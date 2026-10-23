"""Product feed checks, JSON-LD validation, and agent proposals."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, new_id
from app.models.approval import ApprovalItem
from app.models.creative import Product
from app.models.product_feed import ProductFeedFinding
from app.services import product_feed as feed
from tests.test_seo_pipeline_fixes import TENANT, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_inspect_flags_missing_description() -> None:
    issues = feed.inspect_product(
        name="Widget Pro",
        description="",
        url="https://example.com/widget",
        price_text="From $49",
        approved_claims=["Ships in 2 days", "Made for industrial teams"],
    )
    kinds = {i.kind for i in issues}
    assert "missing_description" in kinds
    desc = next(i for i in issues if i.kind == "missing_description").patch["description"]
    assert len(desc) >= 40


def test_unsafe_url_rejected() -> None:
    issues = feed.inspect_product(
        name="Widget",
        description="A long enough description for merchant quality checks here.",
        url="http://127.0.0.1/product",
        price_text="$1",
        approved_claims=[],
    )
    assert any(i.kind == "unsafe_url" for i in issues)


def test_product_json_ld_has_no_ratings() -> None:
    graph = feed.build_product_json_ld(
        name="Widget Pro",
        description="Industrial widget with operator-approved copy only.",
        url="https://example.com/widget",
        price_text="From $49/month",
    )
    assert graph["@type"] == "Product"
    assert "aggregateRating" not in graph
    assert "review" not in graph


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("product_feed_optimizer") is not None
    assert "product_feed_findings" in TENANT_SCOPED_TABLES


@pytest.fixture
def feed_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [Product.__table__, ProductFeedFinding.__table__, ApprovalItem.__table__]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_proposes_description_fix(feed_db: Session) -> None:
    from app.agents.product_feed_optimizer.agent import AGENT

    product = Product(
        id=new_id(),
        tenant_id=TENANT,
        name="Widget Pro",
        description="",
        url="https://example.com/widget-pro",
        price_text="From $49",
        approved_claims=["Built for industrial teams", "Ships worldwide"],
        image_file_ids=[],
        disclaimer="",
        is_archived=False,
    )
    feed_db.add(product)
    feed_db.flush()

    ctx = make_ctx(feed_db, slug="product_feed_optimizer", domain="example.com")
    result = AGENT.run(ctx)
    feed_db.flush()
    assert result.skipped is False
    assert len(result.actions) >= 1
    assert result.actions[0].impact.value == "high"
    findings = list(feed_db.execute(select(ProductFeedFinding)).scalars())
    assert any(f.kind == "missing_description" and f.status == "proposed" for f in findings)
