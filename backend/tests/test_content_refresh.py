"""Content refresh decline detection and agent wiring."""
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
from app.models.content_refresh import ContentRefreshCandidate
from app.models.seo import PageStatus, SeoPage
from app.services.content_refresh import decline_signals, path_key
from tests.test_seo_pipeline_fixes import TENANT, make_ctx


def test_decline_signals_flags_click_drop_and_position() -> None:
    rows = [
        {
            "url": "https://acme.test/guide",
            "prior_clicks": 100,
            "recent_clicks": 40,
            "prior_position": 5.0,
            "recent_position": 9.5,
        },
        {
            "url": "https://acme.test/tiny",
            "prior_clicks": 3,
            "recent_clicks": 0,
            "prior_position": 2.0,
            "recent_position": 20.0,
        },
        {
            "url": "https://acme.test/stable",
            "prior_clicks": 50,
            "recent_clicks": 48,
            "prior_position": 4.0,
            "recent_position": 4.2,
        },
    ]
    signals = decline_signals(rows)
    assert [s.url for s in signals] == ["https://acme.test/guide"]
    assert set(signals[0].reason_codes) == {"click_drop", "position_worsened"}


def test_path_key_strips_www_and_scheme() -> None:
    assert path_key("https://www.Acme.test/blog/a/") == "acme.test/blog/a"
    assert path_key("http://acme.test/blog/a") == "acme.test/blog/a"


def test_content_refresh_agent_registered() -> None:
    from app.agents.base.registry import get_agent

    agent = get_agent("content_refresh")
    assert agent is not None
    assert agent.spec.max_impact.value == "high"


def test_refresh_table_under_rls() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    assert "content_refresh_candidates" in TENANT_SCOPED_TABLES


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


@pytest.fixture
def refresh_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        SeoPage.__table__,
        ContentRefreshCandidate.__table__,
        ApprovalItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_content_refresh_proposes_high_impact_action(refresh_db: Session) -> None:
    from app.agents.content_refresh.agent import AGENT

    page = SeoPage(
        id=new_id(),
        tenant_id=TENANT,
        url="https://acme.test/guide",
        title="Guide",
        cms_id="1",
        cms_slug="wordpress",
        body_encrypted="Old body about widgets that ships fast.",
        status=PageStatus.LIVE.value,
        schema_types=[],
        missing_topics=[],
        target_keywords=["widgets"],
    )
    refresh_db.add(page)
    refresh_db.flush()

    class FakeGsc:
        slug = "search_console"
        name = "Search Console"

        def supports(self, capability: Capability) -> bool:
            return capability is Capability.READ_SEARCH_PERFORMANCE

        def read_page_performance_windows(self, *, window_days=28, row_limit=100):  # noqa: ANN001
            return [
                {
                    "url": "https://acme.test/guide",
                    "prior_clicks": 80,
                    "recent_clicks": 20,
                    "prior_impressions": 400,
                    "recent_impressions": 350,
                    "prior_position": 4.0,
                    "recent_position": 11.0,
                    "window_days": window_days,
                }
            ]

    class FakeCms:
        slug = "wordpress"
        name = "WordPress"

        def supports(self, capability: Capability) -> bool:
            return capability in {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
            }

    answer = {
        "rationale": "Clarify the offer and answer pricing questions.",
        "proposed_body": "Refreshed body about widgets with clearer sections.",
        "confidence": 0.8,
    }
    ctx = make_ctx(
        refresh_db,
        slug="content_refresh",
        answer=answer,
        connectors={"search_console": FakeGsc(), "wordpress": FakeCms()},
        domain="acme.test",
    )
    result = AGENT.run(ctx)
    refresh_db.flush()

    assert result.skipped is False
    assert len(result.actions) == 1
    assert result.actions[0].impact.value == "high"
    assert result.actions[0].payload["proposed_body"].startswith("Refreshed")
    rows = list(refresh_db.execute(select(ContentRefreshCandidate)).scalars())
    assert len(rows) == 1
    assert rows[0].status == "proposed"
