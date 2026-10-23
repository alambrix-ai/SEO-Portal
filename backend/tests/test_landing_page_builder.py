"""Landing page drafts and A/B experiment approval gates."""
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
from app.models.landing import AbExperiment, LandingPage
from app.services import landing_pages as lp
from app.services.encryption import Ctx
from tests.test_seo_pipeline_fixes import TENANT, FakeCms, PlainCipher, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_build_landing_body() -> None:
    draft = lp.build_landing_body(
        title="Summer Sale",
        headline="Save on widgets",
        body="A clear value proposition with enough copy for a landing page draft.",
        cta="Shop now",
    )
    assert draft.path.startswith("/lp/")
    assert "Save on widgets" in draft.body_html


def test_experiment_weight_bounds() -> None:
    with pytest.raises(ValueError):
        lp.validate_experiment(name="Test", variant_weight=100)


def test_metric_point_shape() -> None:
    point = lp.record_metric_point(
        experiment_id="abc", variant="control", metric_key="sessions", value=12
    )
    assert point["dimension"] == "ab:abc:control"


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("landing_page_builder") is not None
    assert "landing_pages" in TENANT_SCOPED_TABLES
    assert "ab_experiments" in TENANT_SCOPED_TABLES


@pytest.fixture
def landing_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [LandingPage.__table__, AbExperiment.__table__, ApprovalItem.__table__]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_proposes_publish_and_experiment_start(landing_db: Session) -> None:
    from app.agents.landing_page_builder.agent import AGENT

    cipher = PlainCipher()
    control = LandingPage(
        id=new_id(),
        tenant_id=TENANT,
        title="Control",
        path="/lp/control",
        body_encrypted=cipher.encrypt(
            "<main><h1>Control</h1><p>Body copy here for length.</p></main>",
            context=Ctx.LANDING_BODY,
        ),
        status="draft",
    )
    variant = LandingPage(
        id=new_id(),
        tenant_id=TENANT,
        title="Variant",
        path="/lp/variant",
        body_encrypted=cipher.encrypt(
            "<main><h1>Variant</h1><p>Body copy here for length.</p></main>",
            context=Ctx.LANDING_BODY,
        ),
        status="draft",
    )
    landing_db.add_all([control, variant])
    landing_db.flush()
    experiment = AbExperiment(
        id=new_id(),
        tenant_id=TENANT,
        slug="cta-test",
        name="CTA test",
        hypothesis="Stronger CTA wins",
        control_page_id=control.id,
        variant_page_id=variant.id,
        variant_weight=50,
        metric_key="conversions",
        status="proposed",
        detail={},
    )
    landing_db.add(experiment)
    landing_db.flush()

    class Cms(FakeCms):
        slug = "wordpress"
        name = "WordPress"

        def supports(self, capability: Capability) -> bool:
            return capability in {
                Capability.CREATE_PAGE,
                Capability.WRITE_PAGE,
                Capability.READ_PAGE,
                Capability.LIST_PAGES,
            }

        def create_page(self, *, title, body_html, slug, meta_description="", status="draft"):  # noqa: ANN001
            return "1", f"https://acme.test/{slug}"

    ctx = make_ctx(
        landing_db,
        slug="landing_page_builder",
        connectors={"wordpress": Cms()},
        domain="acme.test",
    )
    result = AGENT.run(ctx)
    landing_db.flush()
    assert result.skipped is False
    kinds = {a.kind for a in result.actions}
    assert "publish_landing_page" in kinds
    assert "start_ab_experiment" in kinds
    assert all(a.impact.value == "high" for a in result.actions)
