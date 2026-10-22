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

from datetime import datetime, timedelta, timezone

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
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.predictive_budget_engine import allocator
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AdsConnector
from app.core.logging import get_logger
from app.core.money import money
from app.db.base import utcnow
from app.models.ads import CHANNEL_CONNECTOR, CHANNEL_LABELS, BudgetAllocation
from app.models.approval import ApprovalItem, ApprovalType
from app.models.workspace import Organization
from app.services.approvals import register_applier

log = get_logger(__name__)

# Relative improvement in modelled blended CAC needed to justify moving
# money. Relative, because a fixed 0.25 meant something very different in
# dollars, rupees and yen.
MIN_CAC_IMPROVEMENT_RATIO = 0.02
# A channel that moved is left alone this long, unless its data changes.
SHIFT_COOLDOWN = timedelta(hours=24)
# A CAC move this large (relative) counts as new data, ending the cooldown.
MATERIAL_CAC_CHANGE = 0.10
# Agent setting holding {channel: {"at": iso time, "cac": CAC when moved}}.
SHIFT_HISTORY_KEY = "shift_history"


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
        connector_use=(
            ConnectorUse(
                Capability.READ_AD_PERFORMANCE,
                UseMode.ALL,
                question="Which ad platforms should this agent manage budgets on?",
                group="ad platforms",
                note=(
                    "A connected platform you leave out keeps its current budget; this "
                    "agent does not move it."
                ),
            ),
            ConnectorUse(
                Capability.WRITE_AD_BUDGET,
                UseMode.ALL,
                group="ad platforms",
                follows=Capability.READ_AD_PERFORMANCE,
                selected="changes budgets on {names}",
                note=(
                    "Platforms listed in “Never change these platforms” also keep their "
                    "current share."
                ),
            ),
        ),
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
        cooling = self._cooling_down(ctx, rows)
        states = self._states(ctx, rows, cooling=cooling)

        # Measured blend, for display only: total spend / total conversions.
        measured_blend = allocator.blended_cac(states)
        plan = allocator.allocate(states)
        # The decision compares like with like: the same model of blended
        # CAC, evaluated at today's shares and at the proposed ones.
        current_blend = allocator.projected_cac(states, allocator.current_shares(states))
        projected = allocator.projected_cac(states, plan.percents)
        shown_blend = measured_blend or current_blend

        if not plan.is_meaningful:
            return AgentResult(
                summary="allocation already optimal — nothing moved"
                + (f" ({len(cooling)} channel(s) held: moved in the last day)" if cooling else ""),
                metric_label=f"blended CAC {money(shown_blend, decimals=2)}",
                detail={
                    "refreshed": refreshed,
                    "blended_cac": measured_blend,
                    "cooling_down": sorted(cooling),
                },
            )

        improvement = (
            (current_blend - projected) / current_blend if current_blend and projected else 0.0
        )
        if current_blend and projected and improvement < MIN_CAC_IMPROVEMENT_RATIO:
            # Holding still is a decision, and the right one here: the churn
            # costs learning-phase performance on every platform it touches.
            return AgentResult(
                summary=(
                    f"held allocation — projected CAC {money(projected, decimals=2)} vs "
                    f"{money(current_blend, decimals=2)} at today's split is under a "
                    f"{MIN_CAC_IMPROVEMENT_RATIO:.0%} gain"
                ),
                metric_label=f"blended CAC {money(shown_blend, decimals=2)}",
                detail={
                    "blended_cac": measured_blend,
                    "current_modelled_cac": current_blend,
                    "projected_cac": projected,
                    "improvement_ratio": round(improvement, 4),
                    "shifts": plan.shifts,
                    "cooling_down": sorted(cooling),
                },
            )

        reallocated = self._reallocated_amount(states, plan)
        daily_moved = self._daily_moved(states, plan)
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
                "measured_blended_cac": measured_blend,
                # Before and after, and the size of the move, for the
                # workspace's limits and the practice record.
                "before": {s.channel: s.percent for s in states},
                "moved_points": plan.moved_points,
                # The daily budget that changes channel: last 7 days' spend
                # / 7 x the points moved. None when no spend is measured.
                "budget_moved_per_day": daily_moved,
            },
            apply=_make_applier(plan.percents, plan.shifts),
            audit=self._audit_line(plan),
            writes_via=self._budget_writers(ctx, plan),
        )

        return AgentResult(
            summary=f"proposing a {plan.moved_points}% reallocation",
            actions=[action],
            # Proposed, not done: under review it waits for a person.
            metric_label=f"{money(reallocated)} reallocation proposed",
            detail={
                "refreshed": refreshed,
                "blended_cac": measured_blend,
                "current_modelled_cac": current_blend,
                "projected_cac": projected,
                "improvement_ratio": round(improvement, 4),
                "rationale": plan.rationale,
                "cooling_down": sorted(cooling),
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
            # Only the ad platforms this agent was told to manage.
            if connector is None or not ctx.uses_connector(slug, Capability.READ_AD_PERFORMANCE):
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

    def _cooling_down(self, ctx: AgentContext, rows: list[BudgetAllocation]) -> set[str]:
        """Channels moved in the last day whose data has not materially changed.

        The engine runs hourly on 7-day rolling figures. Those barely move hour
        to hour, so without this the same verdict was re-applied every hour
        and a channel could be pushed MAX_SHIFT_POINTS further each time —
        8 points an hour on data that had not changed. A channel is therefore
        held for SHIFT_COOLDOWN after it moves, unless its CAC has since moved
        by at least MATERIAL_CAC_CHANGE (new data worth acting on).
        """
        history = _shift_history(ctx)
        now = utcnow()
        held: set[str] = set()
        for row in rows:
            entry = history.get(row.channel)
            if not isinstance(entry, dict):
                continue
            try:
                at = datetime.fromisoformat(str(entry.get("at")))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if now - at >= SHIFT_COOLDOWN:
                continue
            then = float(entry.get("cac") or 0.0)
            changed = then > 0 and abs(row.cac - then) / then >= MATERIAL_CAC_CHANGE
            if not changed:
                held.add(row.channel)
        return held

    def _states(
        self,
        ctx: AgentContext,
        rows: list[BudgetAllocation],
        *,
        cooling: set[str] | frozenset[str] = frozenset(),
    ) -> list[allocator.ChannelState]:
        # A channel named in the scope field is held at its current share, and
        # so is one still cooling down from its last move.
        locked_by_scope = {
            part.strip().lower() for part in (ctx.scope or "").split(",") if part.strip()
        }
        excluded = excluded_channels(ctx)
        return [
            allocator.ChannelState(
                channel=row.channel,
                percent=row.percent,
                cac=row.cac,
                spend=row.spend,
                conversions=row.conversions,
                locked=(
                    row.locked
                    or row.channel.lower() in locked_by_scope
                    or row.channel in cooling
                    or row.channel in excluded
                ),
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

    def _daily_moved(
        self, states: list[allocator.ChannelState], plan: allocator.Allocation
    ) -> float | None:
        """Daily budget the shift moves between channels, in the account currency."""
        total_spend = sum(s.spend for s in states)
        if not total_spend:
            return None
        return round(total_spend / 7 * (plan.moved_points / 100), 2)

    def _budget_writers(self, ctx: AgentContext, plan: allocator.Allocation) -> tuple[str, ...]:
        """Connectors the plan's budget changes are pushed through when applied.

        The same test the applier uses: a channel whose share changes, whose
        platform is connected and is one this agent manages budgets on.
        """
        slugs: list[str] = []
        for channel, shift in plan.shifts.items():
            slug = CHANNEL_CONNECTOR.get(channel)
            if not shift or not slug or slug in slugs:
                continue
            if ctx.optional_connector(slug) is None:
                continue
            if not ctx.uses_connector(slug, Capability.WRITE_AD_BUDGET):
                continue
            slugs.append(slug)
        return tuple(slugs)

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


def excluded_channels(ctx: AgentContext) -> set[str]:
    """Channels whose platform is connected but left out of this agent's selection.

    They are held at their current share and never written to — "a platform
    you leave out keeps its current budget". A channel whose platform is not
    connected is not excluded; it is simply recorded, as before.
    """
    selected_slugs = getattr(ctx, "selected_slugs", None)
    policy_for = getattr(ctx, "policy", None)
    if not callable(selected_slugs) or not callable(policy_for):
        return set()
    selected = set(selected_slugs(Capability.READ_AD_PERFORMANCE))
    policy = policy_for(Capability.READ_AD_PERFORMANCE)
    return {
        channel
        for channel, slug in CHANNEL_CONNECTOR.items()
        if slug
        and ctx.has_connector(slug)
        and ((selected and slug not in selected) or not policy.permits(slug))
    }


# ── Applying ───────────────────────────────────────────────────────────────
def _shift_history(ctx: AgentContext) -> dict:
    history = ctx.setting(SHIFT_HISTORY_KEY, {}) or {}
    return dict(history) if isinstance(history, dict) else {}


def _make_applier(percents: dict[str, int], shifts: dict[str, int]):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        from app.core.exceptions import ConnectorError
        from app.core.user_messages import public_error_message

        rows = {
            row.channel: row
            for row in ctx.db.execute(
                select(BudgetAllocation).where(BudgetAllocation.tenant_id == ctx.tenant_id)
            ).scalars()
        }
        history = _shift_history(ctx)
        now = utcnow().isoformat()
        failures: list[tuple[str, str]] = []

        for channel, percent in percents.items():
            row = rows.get(channel)
            if row is None or row.locked:
                continue

            slug = CHANNEL_CONNECTOR.get(channel)
            if channel in excluded_channels(ctx):
                # Left out of this agent's ad platforms: its budget is not moved.
                continue
            connector = ctx.optional_connector(slug) if slug else None
            if connector is not None and ctx.uses_connector(slug, Capability.WRITE_AD_BUDGET):
                try:
                    connector.set_budget_share(percent=percent)  # type: ignore[union-attr]
                except Exception as exc:  # noqa: BLE001 - collected and raised below
                    ctx.log_warning("Could not push budget to %s: %s", channel, exc)
                    failures.append((channel, public_error_message(exc, fallback="")))
                    # The platform still has the old budget, so the stored
                    # share stays what the platform is actually spending.
                    continue
            # Otherwise the share is recorded locally only, so a channel that
            # is connected later inherits the current allocation.

            row.percent = percent
            row.last_shift = shifts.get(channel, 0)
            if shifts.get(channel, 0):
                history[channel] = {"at": now, "cac": row.cac}

        ctx.set_setting(SHIFT_HISTORY_KEY, history)
        ctx.db.flush()

        if failures:
            # Raised after the successful channels are written, so those are
            # recorded truthfully; raising is what keeps a queued plan pending
            # (and a run's error visible) instead of reporting it as applied.
            names = ", ".join(
                f"{CHANNEL_LABELS.get(channel, channel)}"
                + (f" ({reason})" if reason else "")
                for channel, reason in failures
            )
            raise ConnectorError(
                f"The budget could not be changed on {names}. "
                "The other channels were updated; those shares are unchanged."
            )

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
