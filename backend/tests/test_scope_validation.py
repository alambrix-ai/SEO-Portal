"""What the Configure dialog accepts, and what it refuses.

These are checked when the operator presses Save rather than when the agent
next runs. A scope of ``inventory/*`` instead of ``/inventory/*`` matches
nothing, and finding that out six hours later from a run that reported "0
pages" is the difference between a typo and a wasted afternoon.

They reject only what is certainly wrong: a validator that guesses gets in the
way of a legitimate value, and being unable to save a correct configuration is
worse than saving a doubtful one.
"""
from __future__ import annotations

import pytest

from app.agents.base import scope as rules
from app.agents.base.registry import get_agent


# ── Paths ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scope", ["", "/inventory/*", "/a/*, /b/*", "/", "/about"])
def test_valid_paths_are_accepted(scope: str):
    assert rules.paths(scope) is None


def test_a_missing_leading_slash_is_caught_and_the_fix_is_named():
    """The single most likely typo, and the one with no visible symptom."""
    problem = rules.paths("inventory/*")
    assert problem is not None
    assert "/inventory/*" in problem, problem


def test_a_full_url_in_a_path_scope_is_caught():
    problem = rules.paths("https://site.com/inventory")
    assert problem is not None


# ── Domains ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "scope", ["", "competitor.com", "a.co.uk, b.io", "Some Company Name"]
)
def test_valid_domains_and_company_names_are_accepted(scope: str):
    """The link monitor matches on either, so both have to be allowed."""
    assert rules.domains(scope) is None


def test_a_scheme_or_path_in_a_domain_scope_is_caught():
    assert "competitor.com" in (rules.domains("https://competitor.com") or "")
    assert rules.domains("competitor.com/blog") is not None


# ── Fixed vocabularies ─────────────────────────────────────────────────────
def test_any_real_schema_type_is_accepted_whatever_the_vertical():
    """An allow-list here was a decision about who the product is for.

    Ten types quietly meant "this platform is for car dealers": a recipe site,
    a university, a clinic or a job board would have been refused a type it
    legitimately needed. schema.org has hundreds.
    """
    agent = get_agent("knowledge_graph_schema")
    for scope in (
        "Product, FAQPage",
        "Recipe",
        "JobPosting, Course",
        "MedicalClinic",
        "RealEstateListing",
        "SoftwareApplication",
    ):
        assert agent.validate_scope(scope) is None, scope


def test_something_that_cannot_be_a_schema_type_is_still_refused():
    """The shape is checked, so the typo worth catching still is."""
    agent = get_agent("knowledge_graph_schema")
    for scope in ("Product Page", "produt_page", "!!", "ab"):
        problem = agent.validate_scope(scope)
        assert problem is not None, scope
        assert "PascalCase" in problem


def test_a_channel_that_does_not_exist_cannot_be_locked():
    """A lock on a nonexistent channel protects nothing.

    The engine would go on reallocating the channel somebody meant to hold.
    """
    agent = get_agent("predictive_budget_engine")
    assert agent.validate_scope("google, meta") is None
    assert agent.validate_scope("googel") is not None


# ── Free text ──────────────────────────────────────────────────────────────
def test_a_scope_long_enough_to_lose_the_agent_is_refused():
    assert rules.free_text(", ".join(f"topic{i}" for i in range(20)), max_items=8) is not None
    assert rules.free_text("one, two, three", max_items=8) is None


def test_every_agent_accepts_an_empty_scope():
    """Blank means "decide for yourself" everywhere, and must never be an error."""
    from app.agents.base.registry import all_agents

    for agent in all_agents():
        assert agent.validate_scope("") is None, agent.spec.slug


# ── Matching, which is where the damage actually was ───────────────────────
# Validating a scope and *applying* one are different jobs, and only the
# first had tests. Four agents applied it as `url.startswith(scope.rstrip("*"))`
# and that was wrong three ways at once, on values the validator happily
# accepted.
@pytest.mark.parametrize(
    ("scope", "url", "inside"),
    [
        # No scope is the whole site. This has to stay permissive.
        ("", "/anything", True),
        # A section includes its own landing page. "/blog/*" excluding "/blog"
        # dropped the most important page in the section.
        ("/blog/*", "/blog", True),
        ("/blog/*", "/blog/a-post", True),
        ("/blog/*", "/about", False),
        # A bare path is a section too, not a string prefix: /blog must not
        # pull in /blog-archive, which is a different part of the site.
        ("/blog", "/blog", True),
        ("/blog", "/blog/a-post", True),
        ("/blog", "/blog-archive", False),
        # A comma-separated scope validates, so it has to work. As one
        # prefix, "/a/*, /b/*" became the literal "/a/*, /b/" and matched
        # nothing at all — the agent then reported that the source had no
        # pages.
        ("/a/*, /b/*", "/a/page", True),
        ("/a/*, /b/*", "/b/page", True),
        ("/a/*, /b/*", "/c/page", False),
        # A glob in the middle.
        ("/product/*/spec", "/product/x/spec", True),
        ("/product/*/spec", "/product/x/price", False),
        # Trailing slashes on either side must not decide the answer.
        ("/blog/", "/blog", True),
        ("/blog", "/blog/", True),
    ],
)
def test_a_url_is_matched_against_the_scope_the_way_it_reads(
    scope: str, url: str, inside: bool
):
    assert rules.path_matcher(scope)(url) is inside


def test_the_sql_filter_agrees_with_the_python_matcher():
    """The two must not disagree: three agents filter in SQL and one in
    Python, and a difference between them would be invisible."""
    from sqlalchemy import select

    from app.models.seo import SeoPage

    for scope in ("", "/blog", "/blog/*", "/a/*, /b/*"):
        clause = rules.sql_filter(scope, SeoPage.url)
        if not rules.items(scope):
            assert clause is None
            continue
        # Compiles, and mentions the column it filters.
        rendered = str(select(SeoPage.url).where(clause))
        assert "seo_pages.url" in rendered


def test_a_url_with_like_wildcards_in_it_does_not_widen_the_match():
    """A path can legitimately contain _ or %, and LIKE treats both as
    wildcards. Unescaped, /a_b would also match /axb."""
    from sqlalchemy.dialects import postgresql

    from app.models.seo import SeoPage

    clause = rules.sql_filter("/a_b", SeoPage.url)
    rendered = str(clause.compile(dialect=postgresql.dialect()))
    assert "ESCAPE" in rendered


def test_the_scope_examples_do_not_assume_one_industry():
    """The placeholder read "/inventory/*", which is how a scope of
    "inventory" came to be typed into a workspace that sells software."""
    from app.agents.base.registry import agent_slugs

    for slug in agent_slugs():
        placeholder = get_agent(slug).spec.scope_placeholder.lower()
        for word in ("inventory", "lease", "dealer", "vehicle", " ev ", "models/"):
            assert word not in placeholder, f"{slug}: {placeholder!r}"
