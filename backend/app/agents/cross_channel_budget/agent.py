"""Propose cross-channel budget shifts from blended ROAS, with daily caps."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    ConnectorUse,
    Impact,
    UseMode,
)
from app.agents.predictive_budget_engine.agent import excluded_channels
from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.ads import CHANNEL_CONNECTOR, BudgetAllocation
from app.models.approval import ApprovalItem, ApprovalType
from app.models.budget import CrossChannelBudgetProposal
from app.models.workspace import Organization
from app.services import cross_channel_budget as budget_svc
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

TARGET_KIND = "cross_channel_budget_proposal"
SLUG = "cross_channel_budget"


class CrossChannelBudgetAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Cross-Channel Budget",
        category=AgentCategory.ADS,
        description=(
            "Allocates spend across Google, Meta, TikTok, and LinkedIn by "
            "blended ROAS. Proposals only — daily move caps apply."
        ),
        default_interval=timedelta(hours=6),
        default_schedule="Every 6 hours",
        required_capabilities=(Capability.READ_AD_PERFORMANCE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=4,
        connector_use=(
            ConnectorUse(
                Capability.READ_AD_PERFORMANCE,
                UseMode.ALL,
                question="Which ad platforms feed blended ROAS?",
                group="ad platforms",
            ),
            ConnectorUse(
                Capability.WRITE_AD_BUDGET,
                UseMode.ALL,
                group="ad platforms",
                follows=Capability.READ_AD_PERFORMANCE,
                selected="applies approved share changes on {names}",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        rows = list(
            ctx.db.execute(
                select(BudgetAllocation)
                .where(BudgetAllocation.tenant_id == ctx.tenant_id)
                .order_by(BudgetAllocation.channel)
            ).scalars()
        )
        if len(rows) < 2:
            return AgentResult.skip("Need at least two budget channels")

        excluded = excluded_channels(ctx)
        channels = [
            budget_svc.ChannelRoas(
                channel=row.channel,
                percent=row.percent,
                spend=float(row.spend or 0),
                conversions=int(row.conversions or 0),
                locked=bool(row.locked) or row.channel in excluded,
            )
            for row in rows
        ]
        used_today = self._points_used_today(ctx)
        plan = budget_svc.plan_reallocation(channels, points_used_today=used_today)
        if plan is None:
            return AgentResult.skip(
                f"No ROAS-improving shift within daily cap ({used_today}/{budget_svc.DAILY_MOVE_CAP_POINTS} pts used)"
            )

        proposal = CrossChannelBudgetProposal(
            tenant_id=ctx.tenant_id,
            snapshot_encrypted=ctx.cipher.encrypt_json(
                [
                    {
                        "channel": c.channel,
                        "percent": c.percent,
                        "spend": c.spend,
                        "conversions": c.conversions,
                        "roas": round(c.roas, 4),
                    }
                    for c in channels
                ],
                context=Ctx.BUDGET_SNAPSHOT,
            ),
            current_shares=plan.current_shares,
            proposed_shares=plan.proposed_shares,
            shifts=plan.shifts,
            rationale=plan.rationale[:2000],
            blended_roas_before=str(plan.blended_before),
            blended_roas_after=str(plan.blended_after),
            moved_points=plan.moved_points,
            status="proposed",
            proposed_at=utcnow(),
        )
        ctx.db.add(proposal)
        ctx.db.flush()

        writers = []
        for channel, shift in plan.shifts.items():
            slug = CHANNEL_CONNECTOR.get(channel)
            if shift and slug and ctx.optional_connector(slug) is not None:
                if ctx.uses_connector(slug, Capability.WRITE_AD_BUDGET) and slug not in writers:
                    writers.append(slug)

        action = AgentAction(
            kind="shift_cross_channel_budget",
            title=(
                f"Shift {plan.moved_points} pts toward higher-ROAS channels "
                f"(ROAS {plan.blended_before} → {plan.blended_after})"
            ),
            impact=Impact.HIGH,
            approval_type=ApprovalType.BUDGET_SHIFT.value,
            target_kind=TARGET_KIND,
            target_id=proposal.id,
            payload={
                "proposal_id": proposal.id,
                "proposed_shares": plan.proposed_shares,
                "shifts": plan.shifts,
                "moved_points": plan.moved_points,
                "rationale": plan.rationale,
            },
            apply=_make_applier(proposal.id),
            audit=f"applied cross-channel budget shift ({plan.moved_points} pts)",
            metrics={"budget_points_moved": plan.moved_points},
            writes_via=tuple(writers),
        )
        return AgentResult(
            summary=f"Proposed {plan.moved_points}-point ROAS reallocation",
            actions=[action],
            metric_label=f"ROAS {plan.blended_before}→{plan.blended_after}",
            metrics={
                "budget_points_proposed": plan.moved_points,
                "daily_points_used": used_today,
            },
        )

    def _points_used_today(self, ctx: AgentContext) -> int:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        total = ctx.db.execute(
            select(func.coalesce(func.sum(CrossChannelBudgetProposal.moved_points), 0)).where(
                CrossChannelBudgetProposal.tenant_id == ctx.tenant_id,
                CrossChannelBudgetProposal.proposed_at >= start,
                CrossChannelBudgetProposal.status.in_(("proposed", "applied")),
            )
        ).scalar_one()
        return int(total or 0)


def _make_applier(proposal_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _apply_proposal(ctx.db, ctx.org, proposal_id, ctx=ctx)

    return apply


def _apply_proposal(
    db: Session,
    org: Organization,
    proposal_id: str,
    *,
    ctx: AgentContext | None = None,
    payload: dict | None = None,
) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    proposal = db.get(CrossChannelBudgetProposal, proposal_id)
    if proposal is None or proposal.tenant_id != org.id:
        raise LookupError("Budget proposal no longer exists")
    shares = dict(
        (payload or {}).get("proposed_shares") or proposal.proposed_shares or {}
    )
    if not shares:
        raise ValueError("Proposal has no shares")

    if ctx is None:
        record = db.execute(
            select(AgentRecord).where(
                AgentRecord.tenant_id == org.id,
                AgentRecord.slug == SLUG,
            )
        ).scalar_one_or_none()
        if record is None:
            raise LookupError("Cross-Channel Budget agent is not installed")
        ctx = build_context(db, record, org, trigger="approval")

    rows = {
        row.channel: row
        for row in db.execute(
            select(BudgetAllocation).where(BudgetAllocation.tenant_id == org.id)
        ).scalars()
    }
    excluded = excluded_channels(ctx)
    for channel, percent in shares.items():
        row = rows.get(channel)
        if row is None or row.locked or channel in excluded:
            continue
        slug = CHANNEL_CONNECTOR.get(channel)
        connector = ctx.optional_connector(slug) if slug else None
        if connector is not None and ctx.uses_connector(slug, Capability.WRITE_AD_BUDGET):
            try:
                connector.set_budget_share(percent=int(percent))  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Could not push budget to %s: %s", channel, exc)
                continue
        row.percent = int(percent)
        row.last_shift = int((proposal.shifts or {}).get(channel) or 0)
    proposal.status = "applied"
    db.flush()


@register_applier(TARGET_KIND)
def apply_cross_channel_budget(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    proposal_id = str((payload or {}).get("proposal_id") or item.target_id or "")
    if not proposal_id:
        raise ValueError("Missing proposal id")
    _apply_proposal(db, org, proposal_id, payload=payload or {})
    proposal = db.get(CrossChannelBudgetProposal, proposal_id)
    if proposal is not None:
        proposal.approval_item_id = item.id


@register_rejecter(TARGET_KIND)
def reject_cross_channel_budget(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    proposal_id = str((payload or {}).get("proposal_id") or item.target_id or "")
    proposal = db.get(CrossChannelBudgetProposal, proposal_id) if proposal_id else None
    if proposal is None or proposal.tenant_id != org.id:
        return
    proposal.status = "rejected"
    proposal.approval_item_id = item.id
    db.flush()


AGENT = CrossChannelBudgetAgent()
