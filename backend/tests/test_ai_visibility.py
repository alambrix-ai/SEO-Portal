"""AI visibility attribution and agent behaviour."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.connectors.base.aeo_base import BaseAeoConnector
from app.connectors.base.interfaces import VisibilityProbe
from app.db.base import Base
from app.models.visibility import (
    VisibilityObservation,
    VisibilityQuery,
    VisibilitySettings,
)
from app.services.encryption import Ctx
from app.services.visibility import attribute_sources, normalise_competitor_list
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher, make_ctx


def test_attribute_sources_flags_brand_and_competitors() -> None:
    attr = attribute_sources(
        [
            "https://rival.example/a",
            "https://www.acme.test/pricing",
            "https://other.example/",
            "https://blog.rival.example/x",
        ],
        brand_domain="acme.test",
        competitors=["rival.example", "https://elsewhere.test"],
    )
    assert attr.brand_cited is True
    assert attr.brand_position == 2
    assert "acme.test" in attr.cited_domains
    assert "rival.example" in attr.competitors_cited
    assert "blog.rival.example" in attr.competitors_cited
    assert "elsewhere.test" not in attr.competitors_cited


def test_attribute_sources_no_brand() -> None:
    attr = attribute_sources(
        ["https://rival.example/"],
        brand_domain="acme.test",
        competitors=["rival.example"],
    )
    assert attr.brand_cited is False
    assert attr.brand_position is None
    assert attr.competitors_cited == ["rival.example"]


def test_normalise_competitor_list_dedupes_and_strips() -> None:
    assert normalise_competitor_list(
        ["https://WWW.Rival.Example/path", "rival.example", "", "https://other.test"]
    ) == ["rival.example", "other.test"]


def test_probe_visibility_returns_all_sources() -> None:
    from app.connectors.base.connector import ConnectorSpec

    class Stub(BaseAeoConnector):
        engine = "openai"
        spec = ConnectorSpec(
            slug="stub_openai",
            name="Stub",
            category="AI models",
            description="Test double",
            fields=(),
            capabilities=frozenset(),
        )

        def _ask_with_search(self, query: str) -> list[str]:
            assert query == "best crm"
            return [
                "https://rival.example/",
                "https://acme.test/",
            ]

        def _probe_credentials(self) -> None:
            return None

    probe = Stub().probe_visibility(query="best crm")
    assert isinstance(probe, VisibilityProbe)
    assert probe.engine == "openai"
    assert len(probe.sources) == 2


def test_visibility_agent_is_registered() -> None:
    from app.agents.base.registry import get_agent

    agent = get_agent("ai_visibility_tracker")
    assert agent is not None
    assert agent.spec.slug == "ai_visibility_tracker"
    assert agent.spec.max_impact.value == "low"


def test_visibility_tables_are_under_rls() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    for table in (
        "visibility_queries",
        "visibility_settings",
        "visibility_observations",
    ):
        assert table in TENANT_SCOPED_TABLES


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


@pytest.fixture
def visibility_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        VisibilityQuery.__table__,
        VisibilitySettings.__table__,
        VisibilityObservation.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_visibility_agent_records_observations(visibility_db: Session) -> None:
    from app.agents.ai_visibility_tracker.agent import AGENT
    from app.connectors.base.connector import Capability

    cipher = PlainCipher()
    visibility_db.add(
        VisibilityQuery(
            tenant_id=TENANT,
            question_encrypted=cipher.encrypt(
                "What is the best CRM?", context=Ctx.VISIBILITY_QUESTION
            ),
            label="Best CRM",
            active=True,
            tags=[],
        )
    )
    visibility_db.add(
        VisibilitySettings(
            tenant_id=TENANT,
            competitor_domains=["rival.example"],
        )
    )
    visibility_db.flush()

    class FakeAeo:
        slug = "perplexity"
        name = "Perplexity"
        engine = "perplexity"

        def supports(self, capability: Capability) -> bool:
            return capability is Capability.CHECK_CITATIONS

        def probe_visibility(self, *, query: str) -> VisibilityProbe:
            return VisibilityProbe(
                engine="perplexity",
                query=query,
                sources=[
                    "https://rival.example/crm",
                    "https://acme.test/crm",
                ],
            )

    ctx = make_ctx(
        visibility_db,
        slug="ai_visibility_tracker",
        connectors={"perplexity": FakeAeo()},
        domain="acme.test",
    )
    result = AGENT.run(ctx)
    visibility_db.flush()

    assert result.skipped is False
    rows = list(
        visibility_db.execute(
            select(VisibilityObservation).where(VisibilityObservation.tenant_id == TENANT)
        ).scalars()
    )
    assert len(rows) == 1
    assert rows[0].brand_cited is True
    assert "rival.example" in (rows[0].competitors_cited or [])
