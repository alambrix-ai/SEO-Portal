"""Off-Page & PR workspace — discovered targets and competitor alerts."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import desc, select

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.exceptions import ConflictError, NotFoundError
from app.core.rbac import Module
from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import OffPageWorkspaceOut
from app.services import audit, views

router = APIRouter(prefix="/offpage", tags=["off-page"])


@router.get("", response_model=OffPageWorkspaceOut)
def workspace(current: CurrentUserDep, db: DbSession) -> OffPageWorkspaceOut:
    current.require_view(Module.OFFPAGE)
    can_write = current.can_write(Module.OFFPAGE)

    targets = db.execute(
        select(BacklinkTarget)
        .where(BacklinkTarget.tenant_id == current.tenant_id)
        # Best-fit, highest-authority opportunities first.
        .order_by(desc(BacklinkTarget.relevance), desc(BacklinkTarget.authority))
        .limit(100)
    ).scalars()

    alerts = db.execute(
        select(CompetitorAlert)
        .where(CompetitorAlert.tenant_id == current.tenant_id)
        .order_by(CompetitorAlert.counter_launched, desc(CompetitorAlert.detected_at))
        .limit(25)
    ).scalars()

    return OffPageWorkspaceOut(
        backlinks=[views.backlink_out(t, can_write=can_write) for t in targets],
        alerts=[views.alert_out(a) for a in alerts],
        read_only=not can_write,
    )


@router.post("/targets/{target_id}/outreach", response_model=ActionResult)
def launch_outreach(
    target_id: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """Ask the PR agent to draft and queue a pitch to one target.

    Drafting runs immediately so the operator sees a result; whether it is
    *sent* still goes through the guardrail, because contacting a stranger
    under the customer's name is a high-impact action.
    """
    current.require_write(Module.OFFPAGE)
    target = db.get(BacklinkTarget, target_id)
    if target is None or target.tenant_id != current.tenant_id:
        raise NotFoundError("That target is no longer in the list")
    if target.status != BacklinkStatus.DISCOVERED.value:
        raise ConflictError(f"{target.domain} has already been {target.status.lower()}")

    from app.orchestration.runner import run_agent_by_slug

    outcome = run_agent_by_slug(
        db,
        tenant_id=current.tenant_id,
        slug="digital_pr_outreach",
        trigger="manual",
        triggered_by=current.user.name,
    )
    audit.record_user_action(
        db,
        user=current.user,
        action=f"requested outreach to {target.domain}",
        module="offpage",
        ip_address=ip,
        context={"target_id": target.id, "run": outcome.status},
    )
    db.commit()

    return ActionResult(
        toast=Toast(
            message=(
                "Outreach queued for approval"
                if outcome.run.actions_queued
                else f"Digital PR Outreach: {outcome.summary}"
            )
        )
    )


@router.post("/alerts/{alert_id}/counter-pitch", response_model=ActionResult)
def launch_counter_pitch(
    alert_id: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """Answer a competitor's new link with a counter-pitch to the same publisher."""
    current.require_write(Module.OFFPAGE)
    alert = db.get(CompetitorAlert, alert_id)
    if alert is None or alert.tenant_id != current.tenant_id:
        raise NotFoundError("That alert no longer exists")
    if alert.counter_launched:
        raise ConflictError("A counter-pitch has already gone out for this one")

    from app.orchestration.runner import run_agent_by_slug

    outcome = run_agent_by_slug(
        db,
        tenant_id=current.tenant_id,
        slug="competitor_link_monitor",
        trigger="manual",
        triggered_by=current.user.name,
    )
    audit.record_user_action(
        db,
        user=current.user,
        action=f"requested a counter-pitch to {alert.source_domain}",
        module="offpage",
        ip_address=ip,
        context={"alert_id": alert.id, "run": outcome.status},
    )
    db.commit()

    return ActionResult(
        toast=Toast(
            message=(
                "Counter-pitch launched"
                if outcome.run.actions_taken
                else "Counter-pitch queued for approval"
                if outcome.run.actions_queued
                else f"Competitor Link Monitor: {outcome.summary}"
            )
        )
    )
