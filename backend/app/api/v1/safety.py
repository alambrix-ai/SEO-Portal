"""Spending-agent safety: practice mode, limits, and the stop switches.

Workspace routes (``/safety``): anyone signed in can read the status (the
console's banner needs it); only workspace admins — the roles with full
access to Admin — can change the limits, the workspace practice switch, or
press "Stop all agents" / "Resume".

Platform routes (``/safety/platform``): portal admins only, for the
platform-wide emergency stop and practice mode.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ClientIp, CurrentUserDep, DbSession, PortalAdminDep
from app.core.config import settings
from app.core.rbac import Module
from app.schemas.common import Toast
from app.schemas.safety import (
    CapsRequest,
    PlatformSafetyActionOut,
    PlatformSafetyOut,
    PlatformSwitchOut,
    SafetyActionOut,
    SafetyStatusOut,
    SwitchRequest,
)
from app.services import agent_safety, audit

router = APIRouter(prefix="/safety", tags=["safety"])


def _status(db: DbSession, current: CurrentUserDep) -> SafetyStatusOut:
    return SafetyStatusOut(
        **agent_safety.status(db, current.organization),
        currency_symbol=settings.currency_symbol,
        can_manage=current.can_write(Module.ADMIN),
    )


def _require_admin(current: CurrentUserDep) -> None:
    # Workspace admins: full access to the Admin module.
    current.require_write(Module.ADMIN)


@router.get("", response_model=SafetyStatusOut)
def get_status(current: CurrentUserDep, db: DbSession) -> SafetyStatusOut:
    return _status(db, current)


@router.put("/caps", response_model=SafetyActionOut)
def save_caps(
    payload: CapsRequest, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> SafetyActionOut:
    _require_admin(current)
    agent_safety.save_caps(
        db, org=current.organization, actor=current.user, values=payload.caps, ip_address=ip
    )
    db.commit()
    return SafetyActionOut(
        status=_status(db, current),
        toast=Toast(message="Limits saved", kind="success"),
    )


@router.post("/practice", response_model=SafetyActionOut)
def set_practice(
    payload: SwitchRequest, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> SafetyActionOut:
    _require_admin(current)
    agent_safety.set_workspace_practice(
        db, org=current.organization, actor=current.user, on=payload.on, ip_address=ip
    )
    db.commit()
    message = (
        "Practice mode is on: spending agents plan changes but don't send them"
        if payload.on
        else "Practice mode is off for the workspace. Each agent's own setting applies"
    )
    return SafetyActionOut(status=_status(db, current), toast=Toast(message=message))


@router.post("/stop", response_model=SafetyActionOut)
def stop_all(current: CurrentUserDep, db: DbSession, ip: ClientIp) -> SafetyActionOut:
    """Pause every agent now. Their next changes wait until Resume."""
    _require_admin(current)
    paused = agent_safety.stop_workspace(
        db, org=current.organization, actor=current.user, ip_address=ip
    )
    db.commit()
    noun = "agent" if paused == 1 else "agents"
    return SafetyActionOut(
        status=_status(db, current),
        toast=Toast(message=f"All agents stopped ({paused} {noun} were running)", kind="warning"),
    )


@router.post("/resume", response_model=SafetyActionOut)
def resume_all(current: CurrentUserDep, db: DbSession, ip: ClientIp) -> SafetyActionOut:
    """Lift the stop and restart the agents that were running before it."""
    _require_admin(current)
    restarted = agent_safety.resume_workspace(
        db, org=current.organization, actor=current.user, ip_address=ip
    )
    db.commit()
    noun = "agent" if restarted == 1 else "agents"
    return SafetyActionOut(
        status=_status(db, current),
        toast=Toast(message=f"Agents resumed ({restarted} {noun} restarted)", kind="success"),
    )


# ── Platform (Portal Admin) ────────────────────────────────────────────────
def _platform(db: DbSession) -> PlatformSafetyOut:
    info = agent_safety.platform_info(db)
    return PlatformSafetyOut(
        emergency_stop=PlatformSwitchOut(**info[agent_safety.EMERGENCY_STOP]),
        practice_mode=PlatformSwitchOut(**info[agent_safety.PLATFORM_PRACTICE]),
    )


@router.get("/platform", response_model=PlatformSafetyOut)
def platform_status(current: PortalAdminDep, db: DbSession) -> PlatformSafetyOut:
    current.require_portal_admin()
    return _platform(db)


def _set_platform(
    name: str, on: bool, current: PortalAdminDep, db: DbSession, ip: str, *, words: str
) -> bool:
    current.require_portal_admin()
    actor = current.user.email or current.user.name
    changed = agent_safety.set_platform_switch(db, name=name, on=on, actor_email=actor)
    if changed:
        audit.record(
            db,
            tenant_id=current.tenant_id,
            actor=actor,
            actor_id=current.user.id,
            action=f"portal switched {'on' if on else 'off'} {words}",
            module="portal",
            ip_address=ip,
            context={"platform_switch": name, "on": on},
        )
    db.commit()
    return changed


@router.post("/platform/emergency-stop", response_model=PlatformSafetyActionOut)
def platform_emergency_stop(
    payload: SwitchRequest, current: PortalAdminDep, db: DbSession, ip: ClientIp
) -> PlatformSafetyActionOut:
    """Stop every agent run in every workspace (or lift the stop)."""
    _set_platform(
        agent_safety.EMERGENCY_STOP, payload.on, current, db, ip,
        words="the emergency stop for every workspace",
    )
    message = (
        "Emergency stop is on: no agent runs or sends changes in any workspace"
        if payload.on
        else "Emergency stop is off: agents run on their schedules again"
    )
    return PlatformSafetyActionOut(
        status=_platform(db),
        toast=Toast(message=message, kind="warning" if payload.on else "success"),
    )


@router.post("/platform/practice", response_model=PlatformSafetyActionOut)
def platform_practice(
    payload: SwitchRequest, current: PortalAdminDep, db: DbSession, ip: ClientIp
) -> PlatformSafetyActionOut:
    """Practice mode for spending agents in every workspace (or lift it)."""
    _set_platform(
        agent_safety.PLATFORM_PRACTICE, payload.on, current, db, ip,
        words="practice mode for spending agents in every workspace",
    )
    message = (
        "Practice mode is on for spending agents in every workspace"
        if payload.on
        else "Platform practice mode is off. Workspace and agent settings apply"
    )
    return PlatformSafetyActionOut(status=_platform(db), toast=Toast(message=message))
