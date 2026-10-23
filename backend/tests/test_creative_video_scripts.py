"""Video script drafts, thumbnail formats, and agent proposals."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, new_id
from app.models.approval import ApprovalItem
from app.models.content import MarketingAsset
from app.models.creative import Product
from app.models.video_script import VideoScriptJob
from app.services.creative import video_scripts as scripts
from app.services.creative.formats import FORMATS
from tests.test_seo_pipeline_fixes import TENANT, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_thumbnail_formats_in_catalogue() -> None:
    for fid in scripts.THUMBNAIL_FORMAT_IDS:
        assert fid in FORMATS
        assert FORMATS[fid].kind == "social"


def test_build_draft_from_claims() -> None:
    draft = scripts.build_draft(
        name="Widget Pro",
        description="Industrial widget built for teams that need reliable calibration.",
        claims=["Ships in two days", "Operator-approved materials"],
    )
    assert "Widget Pro" in draft.script
    assert draft.captions
    assert "youtube_thumbnail" in draft.thumbnail_formats
    block = scripts.as_untrusted(draft)
    assert "video_script" in block


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("creative_video_scripts") is not None
    assert "video_script_jobs" in TENANT_SCOPED_TABLES


@pytest.fixture
def video_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        Product.__table__,
        VideoScriptJob.__table__,
        MarketingAsset.__table__,
        ApprovalItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_proposes_script(video_db: Session) -> None:
    from app.agents.creative_video_scripts.agent import AGENT

    product = Product(
        id=new_id(),
        tenant_id=TENANT,
        name="Widget Pro",
        description="Industrial widget built for teams that need reliable calibration.",
        url="https://example.com/widget",
        price_text="From $49",
        approved_claims=["Ships worldwide", "Operator-approved materials"],
        image_file_ids=[],
        disclaimer="",
        is_archived=False,
    )
    video_db.add(product)
    video_db.flush()

    ctx = make_ctx(video_db, slug="creative_video_scripts", domain="example.com")
    result = AGENT.run(ctx)
    video_db.flush()
    assert result.skipped is False
    assert len(result.actions) == 1
    assert result.actions[0].impact.value == "high"
    jobs = list(video_db.execute(select(VideoScriptJob)).scalars())
    assert len(jobs) == 1
    assert jobs[0].status == "proposed"
