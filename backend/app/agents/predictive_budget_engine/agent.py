"""Predictive Budget Engine — moves spend hourly toward the lowest CAC.

Each pass refreshes every connected channel's real CAC from its own reporting
API, computes a bounded inverse-CAC allocation (see ``allocator.py``), and
pushes the new shares back to the platforms.

Two guards make this safe to leave running:

* the allocation is **deterministic and bounded** — no model call decides where
  money goes, and no channel can move more than a few points per pass;
* a reallocation is only proposed when the **projected blended CAC actually
  improves**, so the engine holds still rather than churning the account.

Moving budget is high impact, so under the hybrid guardrail the whole plan
queues as one reviewable item rather than five separate ones.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base import scope as scope_rules
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.predictive_budget_engine import allocator
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AdsConnector
from app.core.logging import get_logger
from app.core.money import money
from app.models.ads import CHANNEL_CONNECTOR, BudgetAllocation
from app.models.approval import ApprovalItem, ApprovalType
from app.models.workspace import Organization
from app.services.approvals import register_applier

log = get_logger(__name__)

# Improvement in projected blended CAC needed to justify moving money.
MIN_CAC_IMPROVEMENT_USD = 0.25


class PredictiveBudgetEngineAgent(BaseAgent):
    spec = AgentSpec(
        slug="predictive_budget_engine",
        name="Predictive Budget Engine",
        category=AgentCategory.ADS,
        description="Shifts spend hourly toward the lowest-CAC channel.",
        default_interval=timedelta(hours=1),
        default_schedule="Hourly",
        required_capabilities=(Capability.READ_AD_PERFORMANCE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=24,
        scope_placeholder="Channels to leave locked, comma separated",
    )

    def validate_scope(self, scope: str) -> str | None:
        """Channels to leave locked, e.g. ``google, meta``.

        Checked against the channels the platform actually allocates across.
        A locked channel that does not exist locks nothing, and the engine
        would go on reallocating the one somebody meant to protect.
        """
        from app.models.ads import AdChannel

        return scope_rules.one_of(
            scope,
            allowed={channel.value for channel in AdChannel},
            label="an ad channel",
        )

    def run(self, ctx: AgentContext) -> AgentResult:
        rows = self._rows(ctx)
        if not rows:
            return AgentResult.skip("No budget channels configured")

        refreshed = self._refresh_performance(ctx, rows)
        states = self._states(ctx, rows)

        current_blend = allocator.blended_cac(states)
        plan = allocator.allocate(states)
        projected = allocator.projected_cac(states, plan.percents)

        if not plan.is_meaningful:
            return AgentResult(
                summary="allocation already optimal — nothing moved",
                metric_label=f"blended CAC {money(current_blend, decimals=2)}",
                detail={"refreshed": refreshed, "blended_cac": current_blend},
            )

        improvement = current_blend - projected if current_blend and projected else 0.0
        if current_blend and projected and improvement < MIN_CAC_IMPROVEMENT_USD:
            # Holding still is a decision, and the right one here: the churn
            # costs learning-phase performance on every platform it touches.
            return AgentResult(
                summary=(
                    f"held allocation — projected CAC {money(projected, decimals=2)} vs "
                    f"{money(current_blend, decimals=2)} is not a material gain"
                ),
                metric_label=f"blended CAC {money(current_blend, decimals=2)}",
                detail={
                    "blended_cac": current_blend,
                    "projected_cac": projected,
                    "shifts": plan.shifts,
                },
            )

        reallocated = self._reallocated_amount(states, plan)
        action = AgentAction(
            kind="rebalance_budget",
            title=(
                f"Shift {plan.moved_points}% of budget toward lower-CAC channels "
                f"(projected CAC {money(projected, decimals=2)})"
            ),
            impact=Impact.HIGH,
            approval_type=ApprovalType.BUDGET_SHIFT.value,
            target_kind="budget_plan",
            payload={
                "percents": plan.percents,
                "shifts": plan.shifts,
                "rationale": plan.rationale,
                "current_blended_cac": current_blend,
                "projected_blended_cac": projected,
            },
            apply=_make_applier(plan.percents, plan.shifts),
            audit=self._audit_line(plan),
        )

        return AgentResult(
            summary=f"proposing a {plan.moved_points}% reallocation",
            actions=[action],
            metric_label=f"{money(reallocated)} reallocated",
            detail={
                "refreshed": refreshed,
                "blended_cac": current_blend,
                "projected_cac": projected,
                "rationale": plan.rationale,
            },
        )

    # ── Inputs ─────────────────────────────────────────────────────────────
    def _rows(self, ctx: AgentContext) -> list[BudgetAllocation]:
        return list(
            ctx.db.execute(
                select(BudgetAllocation)
                .where(BudgetAllocation.tenant_id == ctx.tenant_id)
                .order_by(BudgetAllocation.channel)
            ).scalars()
        )

    def _refresh_performance(
        self, ctx: AgentContext, rows: list[BudgetAllocation]
    ) -> int:
        """Pull each channel's real spend, conversions and CAC."""
        refreshed = 0
        for row in rows:
            slug = CHANNEL_CONNECTOR.get(row.channel)
            connector = ctx.optional_connector(slug) if slug else None
            if connector is None or not connector.supports(Capability.READ_AD_PERFORMANCE):
                continue

            ads: AdsConnector = connector  # type: ignore[assignment]
            try:
                performance = ads.read_performance(days=7)
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Performance read failed for %s: %s", row.channel, exc)
                continue

            row.spend = performance.spend
            row.conversions = performance.conversions
            row.cac = performance.derived_cac
            refreshed += 1

        if refreshed:
            ctx.db.flush()
        return refreshed

    def _states(
        self, ctx: AgentContext, rows: list[BudgetAllocation]
    ) -> list[allocator.ChannelState]:
        # A channel named in the scope field is held at its current share.
        locked_by_scope = {
            part.strip().lower() for part in (ctx.scope or "").split(",") if part.strip()
        }
        return [
            allocator.ChannelState(
                channel=row.channel,
                percent=row.percent,
                cac=row.cac,
                spend=row.spend,
                conversions=row.conversions,
                locked=row.locked or row.channel.lower() in locked_by_scope,
            )
            for row in rows
        ]

    def _reallocated_amount(
        self, states: list[allocator.ChannelState], plan: allocator.Allocation
    ) -> float:
        """Dollars the shift represents, for the agent card's metric line."""
        total_spend = sum(s.spend for s in states)
        if not total_spend:
            return 0.0
        return round(total_spend * (plan.moved_points / 100), 2)

    def _audit_line(self, plan: allocator.Allocation) -> str:
        gains = sorted(
            (c for c, v in plan.shifts.items() if v > 0),
            key=lambda c: plan.shifts[c],
            reverse=True,
        )
        losses = sorted((c for c, v in plan.shifts.items() if v < 0), key=lambda c: plan.shifts[c])
        if gains and losses:
            return (
                f"shifted {abs(plan.shifts[losses[0]])}% spend from "
                f"{losses[0]} to {gains[0]}"
            )
        return f"rebalanced budget by {plan.moved_points}%"


# ── Applying ───────────────────────────────────────────────────────────────
def _make_applier(percents: dict[str, int], shifts: dict[str, int]):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        rows = {
            row.channel: row
            for row in ctx.db.execute(
                select(BudgetAllocation).where(BudgetAllocation.tenant_id == ctx.tenant_id)
            ).scalars()
        }

        for channel, percent in percents.items():
            row = rows.get(channel)
            if row is None or row.locked:
                continue
            row.percent = percent
            row.last_shift = shifts.get(channel, 0)

            slug = CHANNEL_CONNECTOR.get(channel)
            connector = ctx.optional_connector(slug) if slug else None
            if connector is None or not connector.supports(Capability.WRITE_AD_BUDGET):
                # The share is recorded locally either way, so a channel that
                # is connected later inherits the current allocation.
                continue
            try:
                connector.set_budget_share(percent=percent)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Could not push budget to %s: %s", channel, exc)

        ctx.db.flush()

    return apply


@register_applier("budget_plan")
def apply_budget_plan(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id,
            AgentRecord.slug == "predictive_budget_engine",
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("Predictive Budget Engine is not installed in this workspace")

    percents = {str(k): int(v) for k, v in (payload.get("percents") or {}).items()}
    shifts = {str(k): int(v) for k, v in (payload.get("shifts") or {}).items()}
    if not percents:
        raise ValueError("The approved plan has no allocation stored")

    ctx = build_context(db, record, org, trigger="approval")
    _make_applier(percents, shifts)(ctx)


AGENT = PredictiveBudgetEngineAgent()
