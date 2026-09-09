"""Onboarding wizard — the site, the ad platforms, and the guardrail.

Every step maps onto real state. The domain and the content system are
written to the organisation; the guardrail is what the agent runner reads on
every single action.

Step two used to be three checkboxes writing to ``ad_accounts``, which the
docstring above this one claimed "pre-select connectors". They did not: no
agent, service or report ever read that field, and the three keys covered
half the six ad connectors that exist. It reports the connector records
instead, so there is one source of truth and nothing to keep in sync.
"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.policy import GUARDRAIL_LABELS, Guardrail, resolve_guardrail
from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.exceptions import InvalidInputError
from app.core.rbac import Module
from app.db.base import utcnow
from app.models.agent import AgentRecord, AgentStatus
from app.models.workspace import OnboardingState
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import OnboardingOut, OnboardingUpdateRequest
from app.services import audit, views

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

def _state(db: DbSession, tenant_id: str) -> OnboardingState:
    state = db.execute(
        select(OnboardingState).where(OnboardingState.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if state is None:
        # A workspace provisioned before this table existed; create it lazily.
        from app.services.provisioning import install_onboarding
        from app.models.workspace import Organization

        org = db.get(Organization, tenant_id)
        state = install_onboarding(db, org)  # type: ignore[arg-type]
    return state


def _out(state: OnboardingState, db: Session | None = None) -> OnboardingOut:
    guardrail = resolve_guardrail(state.guardrail)
    return OnboardingOut(
        step=state.step,
        domain=state.domain,
        cms=state.cms,
        guardrail=guardrail.value,
        guardrail_label=GUARDRAIL_LABELS[guardrail.value],
        completed=state.completed,
        cms_options=views.cms_options(),
        guardrail_options=views.guardrail_options(),
        ad_platforms=(
            views.ad_platform_options(db, tenant_id=state.tenant_id) if db else []
        ),
    )


@router.get("", response_model=OnboardingOut)
def get_state(current: CurrentUserDep, db: DbSession) -> OnboardingOut:
    current.require_view(Module.ONBOARDING)
    return _out(_state(db, current.tenant_id), db)


@router.put("", response_model=OnboardingOut)
def update_state(
    payload: OnboardingUpdateRequest, current: CurrentUserDep, db: DbSession
) -> OnboardingOut:
    """Save wizard progress. Each field is optional so steps save independently."""
    current.require_write(Module.ONBOARDING)
    state = _state(db, current.tenant_id)

    if payload.step is not None:
        state.step = payload.step

    if payload.domain is not None:
        cleaned = payload.domain.strip().lower()
        for prefix in ("https://", "http://", "www."):
            cleaned = cleaned.removeprefix(prefix)
        cleaned = cleaned.rstrip("/")
        state.domain = cleaned
        # The domain is what every agent treats as "this customer's site", so
        # it lives on the organisation too.
        current.organization.primary_domain = cleaned

    if payload.cms is not None:
        # Matched on the display name, which is what the console sends and
        # what is stored. The options now carry a slug alongside it so the
        # wizard can show each connector's own mark.
        names = [option["name"] for option in views.cms_options()]
        if payload.cms not in names:
            raise InvalidInputError("Choose one of: " + ", ".join(names))
        state.cms = payload.cms

    if payload.guardrail is not None:
        if payload.guardrail not in GUARDRAIL_LABELS:
            raise InvalidInputError("Choose one of: " + ", ".join(GUARDRAIL_LABELS))
        state.guardrail = payload.guardrail

    db.commit()
    return _out(state, db)


@router.post("/complete", response_model=ActionResult)
def complete(
    current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """Finish the wizard and start the fleet — the "Launch workspace" button."""
    current.require_write(Module.ONBOARDING)
    state = _state(db, current.tenant_id)

    if not state.domain:
        raise InvalidInputError("Enter your website domain")

    guardrail = resolve_guardrail(state.guardrail)
    state.completed = True
    state.completed_at = utcnow()
    state.step = 3

    # The guardrail choice is applied to the fleet immediately: under full
    # autonomy every agent acts, otherwise they draft and queue.
    autonomous = guardrail is not Guardrail.HUMAN
    current.organization.global_autonomy = autonomous

    now = utcnow()
    records = db.execute(
        select(AgentRecord).where(AgentRecord.tenant_id == current.tenant_id)
    ).scalars()
    started = 0
    unconfigured = 0
    for record in records:
        record.autonomy = autonomous
        # This is the moment the fleet goes live: agents are installed paused
        # so that nothing acts on a customer's site before a person asks for
        # it, and this button is that request.
        #
        # It starts an agent only if that agent has been configured — the same
        # rule the AI Agents screen enforces. Launching cannot be a way to
        # start eleven agents nobody has looked at, or the configuration gate
        # would exist everywhere except the one button that starts everything.
        #
        # And only agents that have never run: one someone paused deliberately
        # has a reason to be paused, and revisiting onboarding is not a reason
        # to override it.
        if record.status == AgentStatus.PAUSED.value and record.last_run_at is None:
            if record.configured:
                record.status = AgentStatus.RUNNING.value
                record.metric_label = "Waiting for first run"
            else:
                unconfigured += 1
        if record.is_running:
            # Due now, so the workspace has real activity within a tick rather
            # than after the first scheduled interval.
            record.next_run_at = now
            started += 1

    audit.record_user_action(
        db,
        user=current.user,
        action=(
            f"launched the workspace for {state.domain} with the "
            f"{guardrail.value} guardrail"
        ),
        module="onboarding",
        ip_address=ip,
        context={
            "agents_started": started,
            "agents_unconfigured": unconfigured,
            "cms": state.cms,
        },
    )
    db.commit()

    # Reported as it actually happened. A fresh workspace has nothing
    # configured yet, so this usually starts nothing — and saying "agents are
    # live" then would be the kind of confident lie that costs an afternoon.
    if started and unconfigured:
        message = (
            f"Workspace ready — {started} agent{'s' if started != 1 else ''} live, "
            f"{unconfigured} still to configure on the AI Agents screen"
        )
        kind = "success"
    elif started:
        message = f"Workspace ready — {started} agents are live"
        kind = "success"
    else:
        message = (
            "Workspace ready. Nothing is running yet: configure an agent on the "
            "AI Agents screen, then start it."
        )
        kind = "info"

    return ActionResult(toast=Toast(message=message, kind=kind))
