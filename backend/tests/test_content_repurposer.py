"""Content repurposing packs and agent wiring."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, new_id
from app.models.approval import ApprovalItem
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.repurpose import ContentRepurposeJob
from app.models.seo import SeoPage
from app.services import content_repurpose as repurpose
from app.services.encryption import Ctx
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


BODY = (
    "<h1>Industrial Widgets Guide</h1>"
    "<p>Industrial widget installation requires careful planning and trained technicians "
    "who understand calibration, compliance, and seasonal capacity across warehouses.</p>"
    "<p>Compare suppliers, evaluate lead times, negotiate contracts, and keep spare parts "
    "ready so facilities stay productive throughout the year.</p>"
)


def test_build_pack_produces_channels() -> None:
    pack = repurpose.build_pack(
        title="Industrial Widgets Guide",
        body_html=BODY,
        site_url="https://example.com/widgets",
    )
    assert "Industrial Widgets" in pack.social_text
    assert pack.email_subject.startswith("New:")
    assert "[HOOK]" in pack.video_script
    assert "example.com" in pack.email_body


def test_short_source_rejected() -> None:
    with pytest.raises(ValueError):
        repurpose.build_pack(title="Hi", body_html="<p>Too short</p>")


def test_untrusted_fence() -> None:
    pack = repurpose.build_pack(title="Industrial Widgets Guide", body_html=BODY)
    block = repurpose.as_untrusted_block(pack)
    assert "UNTRUSTED" in block.upper() or "BEGIN" in block.upper()


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("content_repurposer") is not None
    assert "content_repurpose_jobs" in TENANT_SCOPED_TABLES


@pytest.fixture
def repurpose_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        MarketingAsset.__table__,
        ContentRepurposeJob.__table__,
        ApprovalItem.__table__,
        SeoPage.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_proposes_pack(repurpose_db: Session) -> None:
    from app.agents.content_repurposer.agent import AGENT

    cipher = PlainCipher()
    asset = MarketingAsset(
        id=new_id(),
        tenant_id=TENANT,
        agent_slug="seo_article_writer",
        run_id="",
        kind=AssetKind.ARTICLE,
        channel="wordpress",
        title="Industrial Widgets Guide",
        content_encrypted=cipher.encrypt(
            json.dumps({"body_html": BODY, "url": "https://example.com/widgets"}),
            context=Ctx.ASSET_CONTENT,
        ),
        status=AssetStatus.APPROVED,
        image_ids=[],
        detail={},
    )
    repurpose_db.add(asset)
    repurpose_db.flush()

    ctx = make_ctx(repurpose_db, slug="content_repurposer", domain="example.com")
    result = AGENT.run(ctx)
    repurpose_db.flush()
    assert result.skipped is False
    assert len(result.actions) == 1
    assert result.actions[0].impact.value == "high"
    jobs = list(repurpose_db.execute(select(ContentRepurposeJob)).scalars())
    assert len(jobs) == 1
    drafts = list(
        repurpose_db.execute(
            select(MarketingAsset).where(MarketingAsset.agent_slug == "content_repurposer")
        ).scalars()
    )
    assert len(drafts) == 3
