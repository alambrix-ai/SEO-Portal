"""Internal linking scoring, safe insertion, and agent wiring."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.connectors.base.connector import Capability
from app.db.base import Base, new_id
from app.models.approval import ApprovalItem
from app.models.internal_link import InternalLinkSuggestion
from app.models.seo import PageStatus, SeoPage
from app.services import internal_linking as linking
from tests.test_seo_pipeline_fixes import TENANT, FakeCms, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_rejects_javascript_href() -> None:
    with pytest.raises(ValueError):
        linking.validate_target_href("javascript:alert(1)")


def test_rejects_dangerous_anchor() -> None:
    with pytest.raises(ValueError):
        linking.validate_anchor('<script>x</script>')


def test_density_blocks_overlinked_short_page() -> None:
    body = (
        '<p>One <a href="/a">a</a> two <a href="/b">b</a> '
        'three <a href="/c">c</a> four <a href="/d">d</a>.</p>'
    )
    assert linking.density_ok(body) is False


def test_insert_link_into_html_paragraph() -> None:
    body = "<p>We help teams ship widgets faster than ever before.</p>"
    out = linking.insert_link(body, href="/pricing", anchor="pricing guide")
    assert 'href="/pricing"' in out
    assert "pricing guide" in out


def test_rank_candidates_prefers_related_pages() -> None:
    pages = [
        {
            "id": "1",
            "url": "https://acme.test/widgets",
            "title": "Widget buying guide",
            "body": "<p>Everything about industrial widgets and procurement.</p>",
            "target_keywords": ["industrial widgets"],
        },
        {
            "id": "2",
            "url": "https://acme.test/widget-pricing",
            "title": "Widget pricing",
            "body": "<p>Prices for industrial widgets and bulk orders.</p>",
            "target_keywords": ["widget pricing"],
        },
        {
            "id": "3",
            "url": "https://acme.test/careers",
            "title": "Careers",
            "body": "<p>Join our finance team in London.</p>",
            "target_keywords": ["jobs"],
        },
    ]
    ranked = linking.rank_candidates(pages, domain="acme.test", limit=5)
    assert ranked
    assert ranked[0].source_page_id in {"1", "2"}
    assert ranked[0].target_page_id in {"1", "2"}
    assert ranked[0].source_page_id != ranked[0].target_page_id


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    agent = get_agent("internal_linking")
    assert agent is not None
    assert agent.spec.max_impact.value == "high"
    assert "internal_link_suggestions" in TENANT_SCOPED_TABLES


@pytest.fixture
def link_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        SeoPage.__table__,
        InternalLinkSuggestion.__table__,
        ApprovalItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_proposes_high_impact_link(link_db: Session) -> None:
    from app.agents.internal_linking.agent import AGENT

    pages = [
        SeoPage(
            id=new_id(),
            tenant_id=TENANT,
            url="https://acme.test/widgets",
            title="Widget buying guide",
            cms_id="1",
            cms_slug="wordpress",
            body_encrypted="<p>Everything about industrial widgets and procurement teams.</p>",
            status=PageStatus.LIVE.value,
            schema_types=[],
            missing_topics=[],
            target_keywords=["industrial widgets"],
        ),
        SeoPage(
            id=new_id(),
            tenant_id=TENANT,
            url="https://acme.test/widget-pricing",
            title="Widget pricing",
            cms_id="2",
            cms_slug="wordpress",
            body_encrypted="<p>Prices for industrial widgets and bulk orders for teams.</p>",
            status=PageStatus.LIVE.value,
            schema_types=[],
            missing_topics=[],
            target_keywords=["widget pricing"],
        ),
    ]
    link_db.add_all(pages)
    link_db.flush()

    class Cms(FakeCms):
        def supports(self, capability: Capability) -> bool:
            return capability in {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
            }

    ctx = make_ctx(
        link_db,
        slug="internal_linking",
        connectors={"wordpress": Cms()},
        domain="acme.test",
    )
    result = AGENT.run(ctx)
    link_db.flush()

    assert result.skipped is False
    assert len(result.actions) >= 1
    action = result.actions[0]
    assert action.impact.value == "high"
    assert action.payload["proposed_body"]
    assert "javascript:" not in action.payload["target_url"].lower()
    rows = list(link_db.execute(select(InternalLinkSuggestion)).scalars())
    assert len(rows) >= 1
    assert rows[0].status == "proposed"
