"""Ads & Programmatic workspace — budgets, creatives, audiences, fraud log."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import desc, select

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.exceptions import InvalidInputError, NotFoundError
from app.core.rbac import Module
from app.models.ads import (
    AdCreative,
    AudienceCluster,
    BudgetAllocation,
    FraudEvent,
)
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import AdsWorkspaceOut, BudgetUpdateRequest
from app.services import audit, budgets, connectors as connector_service, views

router = APIRouter(prefix="/ads", tags=["ads"])


def _budget_rows(db: DbSession, tenant_id: str) -> list[BudgetAllocation]:
    return list(
        db.execute(
            select(BudgetAllocation)
            .where(BudgetAllocation.tenant_id == tenant_id)
            .order_by(desc(BudgetAllocation.percent))
        ).scalars()
    )


@router.get("", response_model=AdsWorkspaceOut)
def workspace(current: CurrentUserDep, db: DbSession) -> AdsWorkspaceOut:
    current.require_view(Module.ADS)
    can_write = current.can_write(Module.ADS)

    rows = _budget_rows(db, current.tenant_id)
    connected = {
        record.slug
        for record in connector_service.list_records(db, tenant_id=current.tenant_id)
        if record.connected
    }

    creatives = db.execute(
        select(AdCreative)
        .where(AdCreative.tenant_id == current.tenant_id)
        .order_by(desc(AdCreative.created_at))
        .limit(12)
    ).scalars()

    audiences = db.execute(
        select(AudienceCluster)
        .where(AudienceCluster.tenant_id == current.tenant_id)
        .order_by(desc(AudienceCluster.size))
        .limit(8)
    ).scalars()

    fraud = db.execute(
        select(FraudEvent)
        .where(FraudEvent.tenant_id == current.tenant_id)
        .order_by(desc(FraudEvent.at))
        .limit(25)
    ).scalars()

    spend = sum(row.spend for row in rows)
    conversions = sum(row.conversions for row in rows)

    return AdsWorkspaceOut(
        budgets=[views.budget_out(row, connected_slugs=frozenset(connected)) for row in rows],
        budget_total=sum(row.percent for row in rows),
        creatives=[views.creative_out(c, current.cipher) for c in creatives],
        audiences=[views.audience_out(a) for a in audiences],
        fraud_log=[views.fraud_out(f) for f in fraud],
        blended_cac=round(spend / conversions, 2) if conversions else 0.0,
        read_only=not can_write,
    )


@router.put("/budget", response_model=AdsWorkspaceOut)
def update_budget(
    payload: BudgetUpdateRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> AdsWorkspaceOut:
    """Set one channel's share by hand, rebalancing the rest to keep 100%.

    A manual change also **locks** the channel, so the predictive engine will
    not quietly undo it on its next pass. Unlocking is a separate, deliberate
    action.

    The other unlocked channels absorb the difference — see
    :mod:`app.services.budgets`. Setting one share without touching the others
    is what allowed a workspace to end up reporting a 129% allocation.
    """
    current.require_write(Module.ADS)
    rows = {r.channel: r.percent for r in budgets.load_split(db, tenant_id=current.tenant_id)}
    previous = rows.get(payload.channel)
    if previous is None:
        raise NotFoundError(f"Channel {payload.channel!r} is not configured")

    updated = budgets.set_share(
        db, tenant_id=current.tenant_id, channel=payload.channel, percent=payload.percent
    )
    absorbed = [
        f"{r.channel} {rows[r.channel]}%->{r.percent}%"
        for r in updated
        if r.channel != payload.channel and rows[r.channel] != r.percent
    ]

    audit.record_user_action(
        db,
        user=current.user,
        action=(
            f"set {payload.channel} budget share to {payload.percent}% "
            f"(was {previous}%) and locked it"
            + (f"; rebalanced {', '.join(absorbed)}" if absorbed else "")
        ),
        module="ads",
        ip_address=ip,
    )
    db.commit()
    return workspace(current, db)


@router.post("/budget/{channel}/unlock", response_model=ActionResult)
def unlock_channel(
    channel: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """Hand a channel back to the predictive engine."""
    current.require_write(Module.ADS)
    row = db.execute(
        select(BudgetAllocation).where(
            BudgetAllocation.tenant_id == current.tenant_id,
            BudgetAllocation.channel == channel,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Channel {channel!r} is not configured")

    row.locked = False
    audit.record_user_action(
        db,
        user=current.user,
        action=f"unlocked {channel} for automatic reallocation",
        module="ads",
        ip_address=ip,
    )
    db.commit()
    return ActionResult(toast=Toast(message=f"{channel} unlocked"))


@router.post("/budget/rebalance", response_model=ActionResult)
def rebalance(current: CurrentUserDep, db: DbSession, ip: ClientIp) -> ActionResult:
    """Run the predictive budget engine now — the "Rebalance to lowest CAC" button."""
    current.require_write(Module.ADS)

    from app.orchestration.runner import run_agent_by_slug

    outcome = run_agent_by_slug(
        db,
        tenant_id=current.tenant_id,
        slug="predictive_budget_engine",
        trigger="manual",
        triggered_by=current.user.name,
    )
    audit.record_user_action(
        db,
        user=current.user,
        action="triggered a budget rebalance",
        module="ads",
        ip_address=ip,
        context={"run": outcome.status, "summary": outcome.summary},
    )
    db.commit()

    if outcome.run.actions_taken:
        message = "Budget rebalanced toward lowest CAC channels"
    elif outcome.run.actions_queued:
        message = "Reallocation queued for approval"
    else:
        message = outcome.summary or "No reallocation was needed"

    return ActionResult(toast=Toast(message=message))


@router.post("/creatives/generate", response_model=ActionResult)
def generate_creatives(
    current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """Run the DCO generator on demand."""
    current.require_write(Module.ADS)

    from app.orchestration.runner import run_agent_by_slug

    outcome = run_agent_by_slug(
        db,
        tenant_id=current.tenant_id,
        slug="dynamic_creative_optimizer",
        trigger="manual",
        triggered_by=current.user.name,
    )
    audit.record_user_action(
        db,
        user=current.user,
        action="generated new ad creative variants",
        module="ads",
        ip_address=ip,
        context={"run": outcome.status},
    )
    db.commit()
    return ActionResult(
        ok=outcome.status != "failed",
        toast=Toast(message=f"Dynamic Creative Optimizer: {outcome.summary}"),
    )
