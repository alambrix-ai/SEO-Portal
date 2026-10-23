"""Cross-channel ROAS allocation and daily caps."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, new_id
from app.models.ads import BudgetAllocation
from app.models.approval import ApprovalItem
from app.models.budget import CrossChannelBudgetProposal
from app.services import cross_channel_budget as budget
from tests.test_seo_pipeline_fixes import TENANT, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_plan_shifts_toward_higher_roas() -> None:
    channels = [
        budget.ChannelRoas("google", 50, spend=1000, conversions=50),  # ROAS 0.05
        budget.ChannelRoas("meta", 50, spend=1000, conversions=200),  # ROAS 0.20
    ]
    plan = budget.plan_reallocation(channels, points_used_today=0)
    assert plan is not None
    assert plan.proposed_shares["meta"] > plan.current_shares["meta"]
    assert plan.moved_points >= 2


def test_daily_cap_blocks() -> None:
    channels = [
        budget.ChannelRoas("google", 50, spend=1000, conversions=10),
        budget.ChannelRoas("meta", 50, spend=1000, conversions=200),
    ]
    plan = budget.plan_reallocation(
        channels, points_used_today=budget.DAILY_MOVE_CAP_POINTS
    )
    assert plan is None


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("cross_channel_budget") is not None
    assert "cross_channel_budget_proposals" in TENANT_SCOPED_TABLES


@pytest.fixture
def budget_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        BudgetAllocation.__table__,
        CrossChannelBudgetProposal.__table__,
        ApprovalItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_proposes_high_impact_shift(budget_db: Session) -> None:
    from app.agents.cross_channel_budget.agent import AGENT

    budget_db.add_all(
        [
            BudgetAllocation(
                id=new_id(),
                tenant_id=TENANT,
                channel="google",
                percent=60,
                cac=20.0,
                spend=1200,
                conversions=40,
                last_shift=0,
                locked=False,
            ),
            BudgetAllocation(
                id=new_id(),
                tenant_id=TENANT,
                channel="meta",
                percent=40,
                cac=5.0,
                spend=800,
                conversions=200,
                last_shift=0,
                locked=False,
            ),
        ]
    )
    budget_db.flush()

    ctx = make_ctx(budget_db, slug="cross_channel_budget", domain="example.com")
    result = AGENT.run(ctx)
    budget_db.flush()
    assert result.skipped is False
    assert len(result.actions) == 1
    assert result.actions[0].impact.value == "high"
    proposals = list(budget_db.execute(select(CrossChannelBudgetProposal)).scalars())
    assert len(proposals) == 1
    assert proposals[0].status == "proposed"
