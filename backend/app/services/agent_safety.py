"""Spending-agent safety: practice mode, limits, and the stop switches.

Where everything is kept (no new tables or columns, so nothing waits on a
migration):

* **Per agent** — ``AgentRecord.settings["practice_mode"]``.
* **Per workspace** — ``Organization.settings["agent_safety"]``:
  ``practice_mode`` (all spending agents), ``caps`` (the limits) and
  ``stopped`` (who pressed "Stop all agents", when, and which agents were
  running so Resume can restart exactly those).
* **Platform** — two rows in ``portal_feature_flags`` with kind
  ``platform_switch``: ``emergency_stop`` and ``practice_mode``. That table is
  platform-wide and outside row-level security, which is what lets the
  scheduler read it before it touches any workspace. ``enabled`` means the
  switch is ON; a missing row means off.

The pure rules (what an action changes, the limits, the wording) live in
``app.agents.base.safety``; this module loads and saves the settings, builds
the runner's :class:`~app.agents.base.safety.SafetyGate`, and records what a
practice run would have done.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import safety
from app.agents.base.safety import Caps, Ledger, SafetyGate
from app.core.exceptions import ConflictError, InvalidInputError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.agent import AgentRecord, AgentStatus
from app.models.workspace import AuditLogEntry, Organization, User
from app.services import audit
from app.services.approvals import PRACTICE_KEY, WHY_KEY  # noqa: F401 - re-exported

log = get_logger(__name__)

SETTINGS_KEY = "agent_safety"

PLATFORM_KIND = "platform_switch"
EMERGENCY_STOP = "emergency_stop"
PLATFORM_PRACTICE = "practice_mode"
PLATFORM_SWITCHES = (EMERGENCY_STOP, PLATFORM_PRACTICE)

PRACTISED_LABEL = "Practised — not sent"

PLATFORM_STOP_REASON = (
    "Emergency stop is on for the whole platform, so no agent runs or sends "
    "changes until the platform team switches it off."
)
WORKSPACE_STOP_REASON = (
    "All agents in this workspace are stopped. Press Resume on the banner to "
    "start them again."
)
STOPPED_MID_RUN = (
    "Agents were stopped while this run was in progress, so this change waits "
    "for your approval."
)


# ── Workspace settings ─────────────────────────────────────────────────────
def _state(settings: dict | None) -> dict:
    raw = (settings or {}).get(SETTINGS_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _save_state(org: Organization, state: dict) -> None:
    # A new dict, so the JSONB column is seen as changed.
    org.settings = {**(org.settings or {}), SETTINGS_KEY: state}


def caps_for(org: Organization) -> Caps:
    return Caps.from_dict(_state(org.settings).get("caps"))


def workspace_practice_on(org: Organization) -> bool:
    return bool(_state(org.settings).get("practice_mode"))


def stopped_info(org: Organization) -> dict | None:
    stopped = _state(org.settings).get("stopped")
    return dict(stopped) if isinstance(stopped, dict) else None


def _fresh_settings(db: Session, org: Organization) -> dict:
    """The organisation's settings as committed now, not as this session loaded them.

    The stop is pressed in another request; a run already in progress has
    its own copy of the row, and must see the stop on its next action.
    """
    try:
        value = db.execute(
            select(Organization.settings).where(Organization.id == org.id)
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - fall back to what is loaded
        log.exception("Could not re-read the workspace's safety settings")
        value = None
    if value is None:
        return dict(org.settings or {})
    return dict(value) if isinstance(value, dict) else {}


def save_caps(
    db: Session, *, org: Organization, actor: User, values: dict[str, Any], ip_address: str | None = None
) -> Caps:
    """Validate and store the workspace's limits."""
    problems: list[str] = []
    current = caps_for(org).to_dict()
    for key, raw in values.items():
        if key not in current:
            problems.append(f"There is no limit called {key}.")
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            problems.append(f"{safety.CAP_LABELS[key][0]} must be a number.")
            continue
        if number < 0 or number != number:
            problems.append(f"{safety.CAP_LABELS[key][0]} cannot be negative.")
            continue
        if key == "max_budget_change_pct" and number > 100:
            problems.append("The largest budget change cannot be more than 100%.")
            continue
        current[key] = number
    if problems:
        raise InvalidInputError(" ".join(problems))
    caps = Caps.from_dict(current)
    state = _state(org.settings)
    state["caps"] = caps.to_dict()
    _save_state(org, state)
    audit.record_user_action(
        db,
        user=actor,
        action="changed the limits on spending agents",
        module="agents",
        ip_address=ip_address,
        context={"caps": caps.to_dict()},
    )
    db.flush()
    return caps


def set_workspace_practice(
    db: Session, *, org: Organization, actor: User, on: bool, ip_address: str | None = None
) -> None:
    state = _state(org.settings)
    state["practice_mode"] = bool(on)
    _save_state(org, state)
    audit.record_user_action(
        db,
        user=actor,
        action=(
            "switched on practice mode for all spending agents"
            if on
            else "switched off practice mode for all spending agents"
        ),
        module="agents",
        ip_address=ip_address,
    )
    db.flush()


# ── Workspace stop ─────────────────────────────────────────────────────────
def stop_workspace(
    db: Session, *, org: Organization, actor: User, ip_address: str | None = None
) -> int:
    """Pause every agent now, and remember which were running for Resume.

    A run already in progress sees the stop before its next change: each
    change it had not applied yet waits for approval instead. Approved
    content scheduled to publish later is held while the stop is on.
    """
    if stopped_info(org) is not None:
        raise ConflictError("Agents in this workspace are already stopped.")
    records = list(
        db.execute(select(AgentRecord).where(AgentRecord.tenant_id == org.id)).scalars()
    )
    running = [r.slug for r in records if r.status == AgentStatus.RUNNING.value]
    for record in records:
        if record.status == AgentStatus.RUNNING.value:
            record.status = AgentStatus.PAUSED.value
        record.next_run_at = None
    state = _state(org.settings)
    state["stopped"] = {
        "at": utcnow().isoformat(),
        "by": actor.name,
        "by_id": actor.id,
        "resume": running,
    }
    _save_state(org, state)
    audit.record_user_action(
        db,
        user=actor,
        action=f"stopped all agents ({len(running)} were running)",
        module="agents",
        ip_address=ip_address,
        context={"kill_switch": "workspace", "on": True, "paused": running},
    )
    db.flush()
    return len(running)


def resume_workspace(
    db: Session, *, org: Organization, actor: User, ip_address: str | None = None
) -> int:
    """Lift the stop and restart the agents that were running when it was pressed."""
    info = stopped_info(org)
    if info is None:
        raise ConflictError("Agents in this workspace are not stopped.")
    wanted = set(info.get("resume") or [])
    restarted = 0
    now = utcnow()
    for record in db.execute(
        select(AgentRecord).where(AgentRecord.tenant_id == org.id)
    ).scalars():
        if record.slug in wanted and record.configured and record.status == AgentStatus.PAUSED.value:
            record.status = AgentStatus.RUNNING.value
            record.next_run_at = now
            restarted += 1
    state = _state(org.settings)
    state.pop("stopped", None)
    _save_state(org, state)
    audit.record_user_action(
        db,
        user=actor,
        action=f"resumed agents after a stop ({restarted} restarted)",
        module="agents",
        ip_address=ip_address,
        context={"kill_switch": "workspace", "on": False, "restarted": restarted},
    )
    db.flush()
    return restarted


# ── Platform switches ──────────────────────────────────────────────────────
def _platform_row(db: Session, name: str):  # noqa: ANN202 - PortalFeatureFlag | None
    from app.models.portal import PortalFeatureFlag

    return db.execute(
        select(PortalFeatureFlag).where(
            PortalFeatureFlag.kind == PLATFORM_KIND, PortalFeatureFlag.slug == name
        )
    ).scalar_one_or_none()


def platform_switch(db: Session, name: str) -> bool:
    """Whether a platform switch is on. Read fresh every time — never cached."""
    try:
        row = _platform_row(db, name)
    except Exception:  # noqa: BLE001
        log.exception("Could not read the platform switch %s", name)
        # Failing closed for the stop: if it cannot be read, nothing runs.
        return name == EMERGENCY_STOP
    return bool(row is not None and row.enabled)


def platform_info(db: Session) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in PLATFORM_SWITCHES:
        row = _platform_row(db, name)
        out[name] = {
            "on": bool(row is not None and row.enabled),
            "by": (row.updated_by if row is not None else "") or "",
            "at": row.updated_at.isoformat() if row is not None and row.updated_at else "",
        }
    return out


def set_platform_switch(db: Session, *, name: str, on: bool, actor_email: str) -> bool:
    """Turn a platform switch on or off. Returns True when it changed."""
    from app.models.portal import PortalFeatureFlag

    if name not in PLATFORM_SWITCHES:
        raise InvalidInputError(f"Unknown platform switch {name!r}")
    row = _platform_row(db, name)
    if row is None:
        row = PortalFeatureFlag(kind=PLATFORM_KIND, slug=name, enabled=on, updated_by=actor_email)
        db.add(row)
        changed = on
    else:
        changed = bool(row.enabled) != bool(on)
        row.enabled = on
        row.updated_by = actor_email
    db.flush()
    return changed


# ── What the runner, scheduler and API ask ─────────────────────────────────
def halt_reason(db: Session, org: Organization) -> str:
    """Why no agent may run or send anything in this workspace now, or ""."""
    if platform_switch(db, EMERGENCY_STOP):
        return PLATFORM_STOP_REASON
    if _state(_fresh_settings(db, org)).get("stopped"):
        return WORKSPACE_STOP_REASON
    return ""


def workspace_stopped(settings: dict | None) -> bool:
    """Pure check on an organisation's settings, for the scheduler's claim."""
    return bool(_state(settings).get("stopped"))


def practice_source(db: Session, org: Organization, record: AgentRecord) -> str:
    """Where practice mode comes from for this agent, or "" when it is off."""
    if not safety.is_spending_agent(record.slug):
        return ""
    if platform_switch(db, PLATFORM_PRACTICE):
        return "platform"
    if workspace_practice_on(org):
        return "workspace"
    if safety.agent_practice_on(record.slug, record.settings):
        return "agent"
    return ""


def default_practice(record: AgentRecord) -> None:
    """A new configuration of a spending agent starts in practice mode."""
    if not safety.is_spending_agent(record.slug):
        return
    settings = dict(record.settings or {})
    if safety.PRACTICE_SETTING not in settings:
        settings[safety.PRACTICE_SETTING] = True
        record.settings = settings


def today_ledger(db: Session, tenant_id: str) -> Ledger:
    """What spending agents changed automatically today (UTC), from the audit log."""
    midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(AuditLogEntry.context).where(
            AuditLogEntry.tenant_id == tenant_id,
            AuditLogEntry.actor_type == "agent",
            AuditLogEntry.at >= midnight,
            AuditLogEntry.context["auto_applied"].astext == "true",
        )
    ).scalars()
    return Ledger.from_measures((row or {}).get("safety") for row in rows)


def gate_for(db: Session, org: Organization, record: AgentRecord, *, connector_name=None) -> SafetyGate:  # noqa: ANN001
    """The safety rules in force for one run of one agent."""
    from app.core.money import money
    from app.services import verification

    spending = safety.is_spending_agent(record.slug)
    if not spending:
        return SafetyGate(halted=lambda: halt_reason(db, org))
    ledger = today_ledger(db, org.id)
    names = connector_name or _connector_name

    def verified(slug: str) -> bool:
        try:
            return bool(verification.is_verified(db, slug))
        except Exception:  # noqa: BLE001 - unknown means unproven
            log.exception("Verification lookup for %s failed", slug)
            return False

    return SafetyGate(
        practice=practice_source(db, org, record),
        caps=caps_for(org),
        ledger=ledger,
        spending=True,
        is_verified=verified,
        connector_name=names,
        halted=lambda: halt_reason(db, org),
        money=lambda value: money(value),
        practice_ledger=Ledger(
            budget_moved=ledger.budget_moved,
            campaigns=ledger.campaigns,
            placements=ledger.placements,
            creatives=ledger.creatives,
        ),
    )


def _connector_name(slug: str) -> str:
    from app.connectors.base import registry as connector_registry

    spec = connector_registry.get_spec(slug)
    return spec.name if spec is not None else slug


# ── Practice records ───────────────────────────────────────────────────────
def practice_key(action: Any) -> dict:  # noqa: ANN401 - an AgentAction
    """What makes two practised actions "the same change".

    The budget engine re-derives its plan every hour on numbers that barely
    move, and in practice mode nothing changes between runs; counting each
    hour as a new change would claim 24 moves a day where the real engine
    makes one. So a plan is the same when it proposes the same split.
    """
    if action.kind == "rebalance_budget":
        return {"percents": action.payload.get("percents") or {}}
    return dict(action.payload or {})


def record_practice(
    db: Session,
    *,
    org: Organization,
    record: AgentRecord,
    action: Any,  # noqa: ANN401 - an AgentAction
    cipher: Any,  # noqa: ANN401 - an OrgCipher
    would: str,
    reason: str,
    notes: list[str],
    measure: dict,
    source: str,
) -> tuple[Any, bool]:
    """Store what a practice run would have done, with the full plan.

    Kept in the approvals table with the status ``practised``: the payload is
    encrypted like any proposal, it shows on the Approvals screen under
    Practice, and nothing can approve it (it is not pending). Returns
    ``(item, created)``; the same change practised again within a day is not
    stored twice.
    """
    from app.models.approval import ApprovalItem, ApprovalStatus
    from app.services.approvals import METRICS_KEY, humanize_age, payload_digest
    from app.services.encryption import Ctx

    now = utcnow()
    digest = payload_digest(practice_key(action))
    stmt = select(ApprovalItem).where(
        ApprovalItem.tenant_id == org.id,
        ApprovalItem.status == ApprovalStatus.PRACTISED.value,
        ApprovalItem.agent_slug == record.slug,
        ApprovalItem.target_kind == action.target_kind,
        ApprovalItem.payload_digest == digest,
        ApprovalItem.submitted_at >= now - timedelta(days=1),
    )
    stmt = stmt.where(
        ApprovalItem.target_id.is_(None)
        if action.target_id is None
        else ApprovalItem.target_id == action.target_id
    )
    existing = db.execute(stmt).scalars().first()
    if existing is not None:
        return existing, False

    stored = {
        **(action.payload or {}),
        **({METRICS_KEY: action.metrics} if action.metrics else {}),
        PRACTICE_KEY: {
            "would": would,
            "reason": reason,
            "notes": notes,
            "measure": measure,
            "source": source,
            "writes_via": list(getattr(action, "writes_via", ()) or ()),
        },
    }
    item = ApprovalItem(
        tenant_id=org.id,
        type=action.approval_type or action.kind,
        title=action.title,
        agent_slug=record.slug,
        agent_name=record.name,
        target_kind=action.target_kind,
        target_id=action.target_id,
        payload_encrypted=cipher.encrypt_json(stored, context=Ctx.APPROVAL_PAYLOAD),
        payload_digest=digest,
        meta=humanize_age(timedelta(0)),
        status=ApprovalStatus.PRACTISED.value,
        submitted_at=now,
        decided_at=now,
        decided_by="Practice mode",
        decision_note=PRACTISED_LABEL,
    )
    db.add(item)
    db.flush()
    return item, True




def practice_summary(db: Session, *, tenant_id: str, slug: str, days: int = 7) -> dict:
    """What this agent would have changed in practice mode over the last ``days``."""
    from app.core.money import money

    since = utcnow() - timedelta(days=days)
    rows = list(
        db.execute(
            select(AuditLogEntry.context).where(
                AuditLogEntry.tenant_id == tenant_id,
                AuditLogEntry.actor_type == "agent",
                AuditLogEntry.at >= since,
                AuditLogEntry.context["practice"].astext == "true",
                AuditLogEntry.context["agent"].astext == slug,
            )
        ).scalars()
    )
    return summarise_practice(rows, days=days, money=money)


def summarise_practice(rows: list[dict], *, days: int = 7, money=None) -> dict:  # noqa: ANN001
    """Pure: fold practice audit contexts into the Results summary."""
    fresh = [r for r in rows if isinstance(r, dict) and not r.get("repeated")]
    automatic = sum(1 for r in fresh if r.get("would") == "apply")
    approval = sum(1 for r in fresh if r.get("would") == "queue")
    moved = 0.0
    priced = False
    for r in fresh:
        value = (r.get("safety") or {}).get("budget_moved")
        if isinstance(value, (int, float)):
            moved += float(value)
            priced = True
    total = len(fresh)
    period = "this week" if days == 7 else f"in the last {days} days"
    noun = "change" if total == 1 else "changes"
    sentence = f"In practice mode this agent would have made {total} {noun} {period}"
    if priced:
        label = money(moved) if money else f"{moved:,.2f}"
        sentence += f" (total budget moved: {label} of daily spend)"
    sentence += "."
    if total:
        sentence += (
            f" {automatic} would have gone ahead automatically and {approval} would have "
            "waited for your approval."
        )
    return {
        "days": days,
        "changes": total,
        "automatic": automatic,
        "needs_approval": approval,
        "budget_moved": round(moved, 2) if priced else None,
        "budget_moved_label": (money(moved) if money else f"{moved:,.2f}") if priced else "",
        "sentence": sentence,
    }


# ── Console status ─────────────────────────────────────────────────────────
def status(db: Session, org: Organization) -> dict:
    """What the console banner and the settings page show."""
    platform = platform_info(db)
    stopped = stopped_info(org)
    return {
        "practice_mode": workspace_practice_on(org),
        "stopped": stopped is not None,
        "stopped_at": (stopped or {}).get("at", ""),
        "stopped_by": (stopped or {}).get("by", ""),
        "platform_stop": platform[EMERGENCY_STOP]["on"],
        "platform_practice": platform[PLATFORM_PRACTICE]["on"],
        "caps": caps_for(org).to_dict(),
        "default_caps": Caps().to_dict(),
        "cap_fields": [
            {"key": key, "label": label, "help": help_text}
            for key, (label, help_text) in safety.CAP_LABELS.items()
        ],
        "spending_agents": sorted(safety.SPENDING_AGENTS),
    }
