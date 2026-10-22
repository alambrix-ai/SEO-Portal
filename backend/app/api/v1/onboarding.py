"""Onboarding wizard — the site, the ad platforms, and the guardrail.

Every step maps onto real state. Domain and content system are optional:
Skip leaves them empty so the workspace can still launch, and they can be
filled in later. When a domain is set, it is written to the organisation.
The guardrail is what the agent runner reads on every single action.

Step two used to be three checkboxes writing to ``ad_accounts``, which the
docstring above this one claimed "pre-select connectors". They did not: no
agent, service or report ever read that field, and the three keys covered
half the six ad connectors that exist. It reports the connector records
instead, so there is one source of truth and nothing to keep in sync.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.policy import (
    GUARDRAIL_LABELS,
    GUARDRAIL_NAMES,
    REVIEW_SETTING_NAME,
    Guardrail,
    resolve_guardrail,
)
from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.exceptions import InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Module
from app.db.base import utcnow
from app.models.agent import AgentRecord, AgentStatus
from app.models.history import AuditSnapshot, SnapshotKind
from app.models.workspace import OnboardingState, Organization
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import (
    OnboardingOut,
    OnboardingUpdateRequest,
    WorkspacePolicyOut,
    WorkspacePolicyRequest,
)
from app.services import audit, history, views
from app.services.history import config as history_config, store as history_store

log = get_logger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

def _state(db: Session, tenant_id: str) -> OnboardingState:
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
    """Save wizard progress. Each field is optional so steps save independently.

    Choosing "Publish automatically" needs ``confirm``, exactly as on
    ``PUT /onboarding/policy``; it is checked before anything is changed.
    """
    current.require_write(Module.ONBOARDING)
    if payload.guardrail == Guardrail.FULL.value and not payload.confirm:
        raise InvalidInputError(
            CONFIRM_PUBLISH_AUTOMATICALLY,
            details={"needs_confirmation": True, "setting": Guardrail.FULL.value},
        )
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
        changed = state.guardrail != payload.guardrail
        state.guardrail = payload.guardrail
        if changed:
            record_policy_version(db, current.organization, actor=current.user)

    db.commit()
    return _out(state, db)


# ── The workspace policy: "Review before publishing" + the agents' switch ──
#: Asked before switching to "Publish automatically" (``confirm`` missing).
CONFIRM_PUBLISH_AUTOMATICALLY = (
    'Switching to "Publish automatically" lets agents publish changes to your '
    "website and ad accounts without asking you. Confirm to continue."
)


def apply_workspace_policy(
    db: Session, org: Organization, state: OnboardingState, *, guardrail: str, autonomous: bool
) -> list[AgentRecord]:
    """Set both halves of the workspace policy. Flushes nothing, commits nothing.

    The agents' own switches follow the workspace switch only when it
    changes, so re-saving the same policy does not undo per-agent choices.
    Returns the agents whose own switch changed.
    """
    state.guardrail = guardrail
    changed: list[AgentRecord] = []
    if bool(org.global_autonomy) != autonomous:
        org.global_autonomy = autonomous
        records = db.execute(
            select(AgentRecord).where(AgentRecord.tenant_id == org.id)
        ).scalars()
        for record in records:
            if bool(record.autonomy) != autonomous:
                record.autonomy = autonomous
                changed.append(record)
    return changed


def record_policy_version(
    db: Session,
    org: Organization,
    *,
    actor: Any,  # noqa: ANN401 - the User who made the change
    summary: str | None = None,
) -> AuditSnapshot | None:
    """Keep the policy now in force as its next version; never fails the save."""
    return history_store.guarded(
        db,
        SnapshotKind.WORKSPACE_POLICY,
        lambda: history.record_version(
            db, org, SnapshotKind.WORKSPACE_POLICY, "", history_config.current_policy(db, org),
            actor=actor, summary=summary,
        ),
    )


def _record_agent_versions(
    db: Session, org: Organization, records: list[AgentRecord], *, actor: Any  # noqa: ANN401
) -> None:
    from app.api.v1.agents import record_config_version

    for record in records:
        record_config_version(db, org, record, actor=actor, summary="Changed with the workspace policy")


@router.put("/policy", response_model=WorkspacePolicyOut)
def set_policy(
    payload: WorkspacePolicyRequest, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> WorkspacePolicyOut:
    """Save "Review before publishing" and the agents' switch in one transaction.

    The two are stored in different places and read separately by the
    runner, so they are written together or not at all: both settings, the
    version and the audit entry share one commit.
    """
    # Each half is guarded by its own module on its own screen.
    current.require_write(Module.ADMIN)
    current.require_write(Module.ONBOARDING)
    current.require_write(Module.AGENTS)
    if payload.guardrail == Guardrail.FULL.value and not payload.confirm:
        raise InvalidInputError(
            CONFIRM_PUBLISH_AUTOMATICALLY,
            details={"needs_confirmation": True, "setting": Guardrail.FULL.value},
        )
    org = current.organization
    state = _state(db, current.tenant_id)
    changed = apply_workspace_policy(
        db, org, state, guardrail=payload.guardrail, autonomous=payload.autonomous
    )
    db.flush()
    version = record_policy_version(db, org, actor=current.user)
    _record_agent_versions(db, org, changed, actor=current.user)
    name = GUARDRAIL_NAMES[payload.guardrail]
    audit.record_user_action(
        db,
        user=current.user,
        action=(
            f'set {REVIEW_SETTING_NAME} to "{name}" with agents '
            + ("acting on their own" if payload.autonomous else "asking first")
        ),
        module=Module.ADMIN.value,
        ip_address=ip,
        context={
            "guardrail": payload.guardrail,
            "autonomous": payload.autonomous,
            "agents_updated": len(changed),
            "version": getattr(version, "version", None),
        },
    )
    db.commit()
    return WorkspacePolicyOut(
        guardrail=payload.guardrail,
        guardrail_label=GUARDRAIL_LABELS[payload.guardrail],
        autonomous=payload.autonomous,
        agents_updated=len(changed),
        version=getattr(version, "version", None),
        toast=Toast(message=f"{REVIEW_SETTING_NAME}: {name}", kind="success"),
    )


#: Shown when a stored policy no longer passes today's rules.
_POLICY_RESTORE_INVALID = "This version can't be restored because its settings are no longer valid."


@history.register_restorer(SnapshotKind.WORKSPACE_POLICY)
def restore_policy(
    db: Session,
    org: Organization,
    subject_id: str,
    document: dict[str, Any],
    *,
    user: Any,  # noqa: ANN401 - the User restoring
) -> dict[str, Any]:
    """Write a stored workspace policy back (``history.restore``).

    ``history.restore`` has already required confirmation when this switches
    the workspace to "Publish automatically". The version is re-validated
    with the same schema as a save; the history service records the new
    version and the audit entry, and the caller commits.
    """
    if subject_id:
        raise NotFoundError("That version was not found")
    try:
        wanted = WorkspacePolicyRequest.model_validate(
            {"guardrail": document.get("guardrail"), "autonomous": document.get("autonomous"), "confirm": True}
        )
    except ValidationError:
        log.info("Workspace policy version not restorable: failed validation")
        raise InvalidInputError(_POLICY_RESTORE_INVALID) from None
    state = _state(db, org.id)
    changed = apply_workspace_policy(
        db, org, state, guardrail=wanted.guardrail, autonomous=wanted.autonomous
    )
    db.flush()
    _record_agent_versions(db, org, changed, actor=user)
    return {"guardrail": wanted.guardrail, "autonomous": wanted.autonomous}


@router.post("/complete", response_model=ActionResult)
def complete(
    current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """Finish the wizard and start the fleet — the "Launch workspace" button.

    Domain and CMS are optional. The wizard offers Skip for them; launching
    must not refuse a workspace that skipped.
    """
    current.require_write(Module.ONBOARDING)
    state = _state(db, current.tenant_id)

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
    switched: list[AgentRecord] = []
    for record in records:
        if bool(record.autonomy) != autonomous:
            switched.append(record)
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
            f"launched the workspace{f' for {state.domain}' if state.domain else ''} "
            f"with the {guardrail.value} guardrail"
        ),
        module="onboarding",
        ip_address=ip,
        context={
            "agents_started": started,
            "agents_unconfigured": unconfigured,
            "cms": state.cms,
        },
    )
    record_policy_version(db, current.organization, actor=current.user)
    _record_agent_versions(db, current.organization, switched, actor=current.user)
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
