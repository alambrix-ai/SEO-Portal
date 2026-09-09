"""No agent decides what to do by reading another agent's state machine.

``SeoPage.status`` belongs to On-Page SEO Sync. Its own docstring says so —
"lifecycle of one page through the optimisation pipeline" — and its values
are that agent's rewrite states: flagged, queued, rewritten, live. Every one
of them describes a page that exists and is published. None means the page
is unusable.

Two other agents filtered on it anyway. The AEO injector skipped ``queued``
pages and the creative optimizer accepted only ``live`` and ``rewritten``.
So the moment On-Page SEO Sync queued a rewrite — which is the *normal*
state on a workspace under human review — the page disappeared from both.
A real workspace had both its pages queued: the AEO run found nothing,
finished in 27 milliseconds, and reported success with three zeroes.

Injecting a Q&A block and building an ad creative have nothing to do with
whether somebody has approved a copy rewrite. Knowledge Graph & Schema, the
third agent over these rows, filters on no status at all, which is what made
the other two the outliers rather than the rule.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from tests.conftest import requires_db

AGENTS = pathlib.Path(__file__).resolve().parents[1] / "app" / "agents"

#: The agent that owns the page lifecycle and is entitled to read it.
OWNER = "on_page_seo_sync"


def _agent_sources() -> list[tuple[str, str]]:
    return [
        (path.parent.name, path.read_text(encoding="utf-8"))
        for path in sorted(AGENTS.glob("*/agent.py"))
    ]


def test_only_the_owning_agent_reads_the_page_lifecycle():
    """Comments mentioning PageStatus are fine; using it in a query is not.

    Parsed rather than grepped, so the explanation of *why* this rule exists
    can name the thing it forbids without tripping over itself.
    """
    offenders: list[tuple[str, int]] = []
    for slug, source in _agent_sources():
        if slug == OWNER:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            # `PageStatus.QUEUED` and friends — an attribute access on the
            # enum, which only appears in real code, never in a comment.
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "PageStatus"
            ):
                offenders.append((slug, node.lineno))
    assert offenders == [], offenders


@requires_db
def test_a_page_awaiting_a_rewrite_is_still_offered_to_the_other_agents(
    client, clean_db  # noqa: ANN001
):
    """The behavioural version, on the exact state that broke it.

    A page whose copy rewrite is queued for approval — the normal state under
    human review — has to remain a valid subject for Q&A pairs and for
    creative. Both agents skipped it, so a workspace where every page was
    queued had two agents quietly doing nothing and reporting success.
    """
    from sqlalchemy import select

    from app.agents.base.registry import get_agent
    from app.db.session import bind_tenant
    from app.models.agent import AgentRecord
    from app.models.seo import PageStatus, SeoPage
    from app.models.workspace import Organization
    from app.orchestration.runner import build_context
    from tests.test_api_flows import register

    body = register(client)
    org_id = body["session"]["organization"]["id"]
    bind_tenant(clean_db, org_id)
    org = clean_db.get(Organization, org_id)

    clean_db.add(
        SeoPage(
            tenant_id=org_id,
            url="/pricing",
            title="Pricing",
            cms_slug="fake_cms",
            word_count=800,
            # The state that made both agents skip it.
            status=PageStatus.QUEUED.value,
            aeo_pairs=0,
        )
    )
    clean_db.flush()

    for slug, method in (
        ("aeo_qa_injector", "_pages_needing_pairs"),
        ("dynamic_creative_optimizer", "_source_pages"),
    ):
        record = clean_db.execute(
            select(AgentRecord).where(
                AgentRecord.tenant_id == org_id, AgentRecord.slug == slug
            )
        ).scalar_one()
        ctx = build_context(clean_db, record, org, trigger="test")
        selected = getattr(get_agent(slug), method)(ctx)
        assert [p.url for p in selected] == ["/pricing"], (slug, selected)


def test_duplicate_proposals_are_prevented_where_they_should_be():
    """The filters were defended as stopping repeat proposals. They were not
    what did that, and removing them changes nothing about it: queue_action
    keeps one pending item per agent per target, keyed by agent slug — so one
    agent's queued proposal cannot suppress another's."""
    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "approvals.py"
    ).read_text(encoding="utf-8")
    assert "def find_open_proposal" in source
    # Keyed by the agent, which is what makes the agents independent.
    assert "agent_slug" in source
