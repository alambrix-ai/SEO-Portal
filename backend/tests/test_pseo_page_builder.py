"""Programmatic SEO quality gates, sandbox render, and agent wiring."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.connectors.base.connector import Capability
from app.db.base import Base, new_id, utcnow
from app.models.approval import ApprovalItem
from app.models.pseo import PseoDataset, PseoPageJob, PseoTemplate
from app.models.seo import PageStatus, SeoPage
from app.services.encryption import Ctx
from app.services.pseo import quality, render
from tests.test_seo_pipeline_fixes import TENANT, FakeCms, PlainCipher, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def _rich_body(city: str) -> str:
    # >40 unique tokens after stop-word removal
    return (
        f"<h1>{city} industrial widget service guide</h1>"
        f"<p>Comprehensive coverage of industrial widget installation maintenance "
        f"procurement logistics training certification warranty spare parts inventory "
        f"scheduling diagnostics calibration compliance documentation onboarding "
        f"troubleshooting for teams operating near {city} facilities warehouses.</p>"
        f"<p>Compare suppliers evaluate lead times negotiate contracts and plan "
        f"seasonal capacity with practical checklists tailored to {city}.</p>"
    )


def test_thin_content_rejected() -> None:
    result = quality.assess(
        "Too short.",
        title="Hi",
        path="/hi",
        existing_hashes=set(),
        existing_page_tokens=[],
    )
    assert result.ok is False
    assert result.reason.startswith("thin_content")


def test_near_duplicate_hash_rejected() -> None:
    body = _rich_body("Austin")
    digest = quality.content_hash(body)
    result = quality.assess(
        body,
        title="Austin guide",
        path="/austin",
        existing_hashes={digest},
        existing_page_tokens=[],
    )
    assert result.ok is False
    assert "near_duplicate" in result.reason


def test_sandbox_blocks_include() -> None:
    with pytest.raises(ValueError):
        render.render_body("{% include 'evil' %}Hello {{ city }}", {"city": "X"})


def test_render_path_is_relative() -> None:
    path = render.render_path("/locations/{city}", {"city": "San Antonio"})
    assert path.startswith("/")
    assert "://" not in path


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("pseo_page_builder") is not None
    assert "pseo_templates" in TENANT_SCOPED_TABLES
    assert "pseo_page_jobs" in TENANT_SCOPED_TABLES


@pytest.fixture
def pseo_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        SeoPage.__table__,
        PseoTemplate.__table__,
        PseoDataset.__table__,
        PseoPageJob.__table__,
        ApprovalItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_proposes_high_impact_page(pseo_db: Session) -> None:
    from app.agents.pseo_page_builder.agent import AGENT

    cipher = PlainCipher()
    template = PseoTemplate(
        id=new_id(),
        tenant_id=TENANT,
        slug="city_guide",
        name="City guide",
        body_encrypted=cipher.encrypt(
            "<h1>{{ city }} widgets</h1><p>{{ blurb }}</p>",
            context=Ctx.PSEO_TEMPLATE,
        ),
        path_pattern="/locations/{city}",
        status="active",
    )
    # Make blurb long enough to pass the unique-token gate.
    blurb = (
        "industrial widget installation maintenance procurement logistics training "
        "certification warranty spare parts inventory scheduling diagnostics "
        "calibration compliance documentation onboarding troubleshooting suppliers "
        "lead times contracts seasonal capacity checklists warehouses facilities "
        "benchmarking forecasting budgeting negotiation packaging freight customs "
        "inspections audits renewals upgrades retrofits telemetry dashboards"
    )
    dataset = PseoDataset(
        id=new_id(),
        tenant_id=TENANT,
        name="Cities",
        rows_encrypted=cipher.encrypt_json(
            [{"city": "Austin", "blurb": blurb, "title": "Austin widgets"}],
            context=Ctx.PSEO_DATASET,
        ),
        row_count=1,
        status="ready",
    )
    pseo_db.add_all([template, dataset])
    pseo_db.flush()

    class Cms(FakeCms):
        slug = "wordpress"
        name = "WordPress"

        def supports(self, capability: Capability) -> bool:
            return capability in {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
                Capability.CREATE_PAGE,
            }

        def create_page(self, *, title, body_html, slug, meta_description="", status="draft"):  # noqa: ANN001
            return "99", f"https://acme.test/{slug}"

    ctx = make_ctx(
        pseo_db,
        slug="pseo_page_builder",
        connectors={"wordpress": Cms()},
        domain="acme.test",
    )
    # PlainCipher is already on ctx from make_ctx
    result = AGENT.run(ctx)
    pseo_db.flush()
    assert result.skipped is False
    assert len(result.actions) == 1
    assert result.actions[0].impact.value == "high"
    jobs = list(pseo_db.execute(select(PseoPageJob)).scalars())
    assert any(j.status == "proposed" for j in jobs)
