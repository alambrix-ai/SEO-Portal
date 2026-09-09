"""What the agents must not ask a language model to know.

Each of these was a real defect where a prompt asked for something the model
cannot know, and the answer then travelled into a decision or onto a
customer's website as though it were research.

* **A sector.** Three agents used ``ctx.setting("industry", "automotive
  retail")``. ``ctx.setting`` reads the *agent record's* settings, and
  nothing in the platform ever wrote ``industry`` — so the default was the
  value, for every customer. Every rewrite, Q&A pair and outreach target was
  produced for a car dealership.

* **A domain authority score.** A proprietary crawl metric, requested from a
  model, then used by ``is_worth_pursuing`` to decide which opportunities a
  person ever saw.

* **A stranger's email address.** Requested, stored, and used by the outreach
  agent as the recipient — so a plausible invention became a real pitch sent
  under the customer's name.

* **A discovery mechanism.** ``discovered_via`` defaulted to "topical crawl",
  describing a crawl that never ran.
"""
from __future__ import annotations

import pathlib
import re

import pytest

AGENTS = pathlib.Path(__file__).resolve().parents[1] / "app" / "agents"


def _sources() -> list[tuple[str, str]]:
    return [
        (path.parent.name, path.read_text(encoding="utf-8"))
        for path in sorted(AGENTS.glob("*/*.py"))
        if path.name in ("agent.py", "prompts.py")
    ]


def test_no_agent_assumes_the_customers_sector():
    """A default vertical is not a fallback, it is a claim about the client.

    Matched on the substance rather than the literal old string: any
    hard-coded sector supplied to a prompt is the same mistake.
    """
    verticals = re.compile(
        r"""["'](automotive|automotive retail|dealership|car dealer|retail|"""
        r"""ecommerce|e-commerce|saas|healthcare|real estate|hospitality)["']""",
        re.I,
    )
    offenders = [
        (slug, match.group(0))
        for slug, source in _sources()
        for match in verticals.finditer(source)
        # A sector named in a list of examples is fine; one used as a
        # fallback value is not.
        if "setting(" in source[max(0, match.start() - 80) : match.start()]
        or "or " in source[max(0, match.start() - 6) : match.start()]
    ]
    assert offenders == [], offenders


def test_the_industry_comes_from_the_workspace_or_from_nowhere():
    """``ctx.industry`` returns empty when nobody has said, and the prompts
    ask the model to infer the sector from the page in front of it."""
    from app.agents.base.context import AgentContext

    assert isinstance(AgentContext.industry, property)

    # And no agent reaches for a default any more.
    for slug, source in _sources():
        assert 'setting("industry"' not in source, slug


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("authority", "a proprietary crawl metric a model cannot measure"),
        ("contact_email", "a stranger's address, which the outreach agent sends to"),
    ],
)
def test_the_link_prospector_does_not_ask_for_what_it_cannot_know(
    field: str, reason: str
):
    from app.agents.backlink_node_discovery import prompts

    assert field not in prompts.SCHEMA_HINT, f"{field}: {reason}"


def test_an_unmeasured_authority_does_not_gate_which_targets_are_shown():
    """Relevance is a judgement a model can make. Authority is a measurement,
    and gating on an invented one hid real opportunities and admitted
    invented ones by a number nobody checked."""
    source = (AGENTS / "backlink_node_discovery" / "agent.py").read_text(
        encoding="utf-8"
    )
    assert "min_authority" not in source
    assert "min_relevance" in source


def test_a_pitch_with_no_address_says_it_was_not_sent():
    """With no recipient the pitch cannot reach the publication. It used to
    arrive as a message starting "Hello," with no addressee, which reads as
    delivered."""
    from app.agents.digital_pr_outreach import prompts

    draft = prompts.render_email(
        body="Pitch text.", recipient="", brand="Acme", domain="example.com"
    )
    assert "not sent" in draft
    assert "example.com" in draft

    sent = prompts.render_email(
        body="Pitch text.", recipient="editor@example.com", brand="Acme"
    )
    assert "not sent" not in sent
    assert sent.startswith("Hello Editor,")


def test_the_answer_engine_agent_emits_an_actual_faq():
    """Its whole purpose is retrieval, and it was emitting loose Question
    items with no FAQPage container — which is what FAQ rich results
    require, and what a parser needs to see them as one FAQ."""
    from app.agents.aeo_qa_injector import prompts

    block = prompts.render_qa_block("How much?", "It starts at X.")
    page = prompts.merge_qa_section("Body copy.", [block])

    assert "schema.org/FAQPage" in page
    assert 'itemprop="mainEntity"' in page
    assert "schema.org/Question" in page
    assert "schema.org/Answer" in page
    # Re-injection replaces its own section rather than appending a second.
    assert prompts.merge_qa_section(page, [block]).count("FAQPage") == 1


def test_the_rewriter_forbids_inventing_the_facts_it_would_need():
    """It edits copy on a live site, so what it refuses to do matters as much
    as what it produces."""
    from app.agents.on_page_seo_sync import prompts

    system = prompts.SYSTEM.lower()
    for forbidden in ("never invent", "statistic", "price", "testimonial"):
        assert forbidden in system, forbidden
    # And it is told to say when a gap cannot be closed honestly, rather
    # than filling it with something plausible.
    assert "leave that gap open" in system


# ── The link-farm check ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "domain",
    [
        # Every one of these was missed. The tokens were unhyphenated runs
        # matched as plain substrings, so the check caught "buylinks.com",
        # which nobody registers, and not the form everybody does.
        "buy-links-cheap.com",
        "seo-links.net",
        "cheap-backlinks.io",
        "guest-post-service.com",
        "link-farm.ru",
        "dofollow-links.biz",
        "pbn.example.com",
        "example-pbn.net",
    ],
)
def test_a_link_seller_is_recognised_however_it_spells_itself(domain: str):
    from app.agents.backlink_node_discovery import scoring

    assert scoring.looks_like_link_farm(domain) is True


@pytest.mark.parametrize(
    "domain",
    [
        "techcrunch.com",
        "searchengineland.com",
        "smashingmagazine.com",
        "linkedin.com",
        "buildingdesign.co.uk",
        # Contains "pbn" once the separators are stripped, which is why the
        # short tokens are matched against the domain's labels instead.
        # Refusing a real publication silently withholds an opportunity from
        # the customer, so a false positive here is the worse error.
        "topbnb.com",
    ],
)
def test_a_real_publication_is_never_mistaken_for_one(domain: str):
    from app.agents.backlink_node_discovery import scoring

    assert scoring.looks_like_link_farm(domain) is False


def test_the_creative_writer_is_told_the_limits_it_will_be_judged_against():
    """check_copy rejects copy over the platform maxima. A prompt that keeps
    them secret has the model write copy the validator throws away, and the
    operator sees fewer variants with no idea why."""
    from app.agents.dynamic_creative_optimizer import prompts, specs

    product = specs.Product(name="Thing", url="https://example.test/thing")
    text = prompts.creative_variants(
        product=product,
        platforms=["Google Ads", "Meta Ads"],
        brand="Acme",
        segments=["In-market"],
        page_url="https://example.test/thing",
    )
    for name in ("Google Ads", "Meta Ads"):
        spec = specs.PLATFORMS[name]
        assert str(spec.headline_max) in text, name
        assert str(spec.body_max) in text, name
        # And the sizes it is allowed to pick from.
        assert spec.dimensions[0] in text, name
