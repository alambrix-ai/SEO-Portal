"""AI Agents hub — the fleet, its switches, and manual runs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import ValidationError
from sqlalchemy import desc, select

from app.agents.base.registry import get_agent as lookup_agent
from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.connectors.base import registry as connector_registry
from app.core.config import settings
from app.core.exceptions import ConflictError, InvalidInputError, NotFoundError
from app.core.rbac import Module
from app.models.agent import AgentRecord, AgentRun, AgentStatus
from app.models.history import AuditSnapshot, SnapshotKind
from app.models.workspace import Organization
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import (
    AgentActivityOut,
    AgentResultsOut,
    AgentConfigRequest,
    AgentOptionsOut,
    LlmConnectorOut,
    NotifyChannelOut,
    AgentOut,
    AgentRunOut,
    AutonomyRequest,
)
from app.schemas.safety import PracticeRequest, PracticeSummaryOut
from app.services import audit, connectors as connector_service, history, views
from app.services.history import store as history_store
from app.core.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.agents.base.plain import SettingField

log = get_logger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


def _record(db: DbSession, tenant_id: str, slug: str) -> AgentRecord:
    from app.models.portal import FeatureKind
    from app.services import portal_features

    if not portal_features.is_enabled(db, FeatureKind.AGENT, slug):
        raise NotFoundError(f"Agent {slug!r} is not available in this workspace")

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == tenant_id, AgentRecord.slug == slug
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError(f"Agent {slug!r} is not installed in this workspace")
    return record


# ── Reads ──────────────────────────────────────────────────────────────────
@router.get("", response_model=list[AgentOut])
def list_agents(current: CurrentUserDep, db: DbSession) -> list[AgentOut]:
    current.require_view(Module.AGENTS)
    return views.list_agents(
        db, tenant_id=current.tenant_id, can_write=current.can_write(Module.AGENTS)
    )


def _notify_options(db: DbSession, org) -> list[NotifyChannelOut]:  # noqa: ANN001
    """Every channel, each marked with whether anything can deliver it."""
    from app.orchestration.runner import NOTIFY_CAPABILITIES

    bundle = connector_service.build_connector_bundle(db, org)
    out: list[NotifyChannelOut] = []
    for channel in views.AGENT_NOTIFY_CHANNELS:
        capability = NOTIFY_CAPABILITIES.get(channel)
        if capability is None:
            # "None" always works, and is the honest default.
            out.append(NotifyChannelOut(value=channel))
            continue
        servers = [
            bundle.names.get(slug, slug)
            for slug, instance in bundle.instances.items()
            if instance.supports(capability)
        ]
        if servers:
            out.append(NotifyChannelOut(value=channel))
        else:
            # Named by what would serve it, not by the channel's own label:
            # a webhook or an SMTP relay carries a notification too.
            candidates = ", ".join(
                spec.name
                for spec in connector_registry.all_specs()
                if capability in spec.capabilities
            )
            out.append(
                NotifyChannelOut(
                    value=channel,
                    available=False,
                    reason=(
                        f"Nothing is connected that can send this. Connect one "
                        f"of: {candidates}."
                    ),
                )
            )
    return out


def _llm_options(db: DbSession, org) -> list[LlmConnectorOut]:  # noqa: ANN001
    """AI model connectors agents can write with, and whether each is connected."""
    from app.connectors.base.connector import Capability
    from app.llm.from_connector import supports_agent_llm
    from app.models.portal import FeatureKind
    from app.services import portal_features

    from app.services import entitlements

    bundle = connector_service.build_connector_bundle(db, org)
    out: list[LlmConnectorOut] = []
    # Unverified types hidden in Portal Admin are not offered, unless connected.
    hidden = portal_features.hidden_connector_slugs(db)
    for spec in connector_registry.all_specs():
        if not portal_features.is_enabled(db, FeatureKind.CONNECTOR, spec.slug):
            continue
        if spec.slug in hidden and spec.slug not in bundle.instances:
            continue
        if not supports_agent_llm(spec.slug):
            continue
        if Capability.COMPLETE not in spec.capabilities:
            continue
        connected = spec.slug in bundle.instances
        # Agents share one AI model list; the plan's rule for writing with AI
        # applies to every agent alike, so it is asked without an agent.
        policy = entitlements.connector_policy(db, org, "", Capability.COMPLETE)
        locked = not policy.permits(spec.slug)
        out.append(
            LlmConnectorOut(
                slug=spec.slug,
                name=spec.name,
                available=connected and not locked,
                reason=policy.locked_reason
                if locked
                else ""
                if connected
                else f"Connect {spec.name} under Connectors first.",
                locked=locked,
                locked_reason=policy.locked_reason if locked else "",
            )
        )
    return out


@router.get("/options", response_model=AgentOptionsOut)
def options(current: CurrentUserDep, db: DbSession) -> AgentOptionsOut:
    current.require_view(Module.AGENTS)
    return AgentOptionsOut(
        schedules=views.AGENT_SCHEDULES,
        notify_channels=_notify_options(db, current.organization),
        llm_connectors=_llm_options(db, current.organization),
    )


@router.get("/activity", response_model=AgentActivityOut)
def activity(current: CurrentUserDep, db: DbSession) -> AgentActivityOut:
    """What the fleet is doing right now, and its most recent runs."""
    current.require_view(Module.AGENTS)
    return AgentActivityOut(**views.agent_activity(db, tenant_id=current.tenant_id))


@router.get("/{slug}", response_model=AgentOut)
def get_agent(slug: str, current: CurrentUserDep, db: DbSession) -> AgentOut:
    current.require_view(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)
    return views.agent_out(
        record,
        can_write=current.can_write(Module.AGENTS),
        env=views.guide_env(db, current.tenant_id),
    )


@router.get("/{slug}/runs", response_model=list[AgentRunOut])
def list_runs(
    slug: str,
    current: CurrentUserDep,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[AgentRunOut]:
    """Run history — what the agent did, when, and what it cost."""
    current.require_view(Module.AGENTS)
    runs = db.execute(
        select(AgentRun)
        .where(AgentRun.tenant_id == current.tenant_id, AgentRun.agent_slug == slug)
        .order_by(desc(AgentRun.started_at))
        .limit(limit)
    ).scalars()
    name = _record(db, current.tenant_id, slug).name
    return [views.run_out(run, agent_name=name) for run in runs]


@router.get("/{slug}/results", response_model=AgentResultsOut)
def get_results(slug: str, current: CurrentUserDep, db: DbSession) -> AgentResultsOut:
    """What this agent has produced: its pages, issues, pitches, ads, blocks …"""
    from dataclasses import asdict

    from app.services.agent_results import results_for
    from app.services.encryption import OrgCipher

    current.require_view(Module.AGENTS)
    _record(db, current.tenant_id, slug)
    results = results_for(
        db, slug=slug, tenant_id=current.tenant_id, cipher=OrgCipher.for_org(current.organization)
    )
    if results is None:
        raise NotFoundError("This agent has no results view")
    data = asdict(results)
    data["metrics"] = [{"label": label, "value": value} for label, value in results.metrics]
    return AgentResultsOut(**data)


@router.get("/{slug}/runs/{run_id}", response_model=AgentRunOut)
def get_run(slug: str, run_id: str, current: CurrentUserDep, db: DbSession) -> AgentRunOut:
    """One run: its timeline and every change it applied or proposed."""
    current.require_view(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)
    run = db.execute(
        select(AgentRun).where(
            AgentRun.tenant_id == current.tenant_id,
            AgentRun.agent_slug == slug,
            AgentRun.id == run_id,
        )
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError("That run was not found")
    return views.run_out(
        run,
        agent_name=record.name,
        events=views.run_events(db, tenant_id=current.tenant_id, run_id=run.id),
    )


def _validate_config(
    db: DbSession,
    current: CurrentUserDep,
    slug: str,
    payload: AgentConfigRequest,
    record: AgentRecord | None = None,
) -> tuple[list[str], dict[str, str] | None]:
    """Check a submitted configuration for the caller's workspace (see :func:`_check_config`)."""
    return _check_config(db, current.organization, slug, payload, record)


def _check_config(
    db: Session,
    org: Organization,
    slug: str,
    payload: AgentConfigRequest,
    record: AgentRecord | None = None,
) -> tuple[list[str], dict[str, str] | None]:
    """Check a configuration; raises on errors.

    Returns the warnings, and the connector choices to store (``None`` when
    the request did not send any, which keeps the saved ones).

    All of it at submit time, which is the point: the dialog used to accept
    anything of the right shape, and the consequences turned up hours later as
    an agent reporting that it did nothing.

    The line between the two matters, and I got it wrong first:

    * An **error** is a value that is wrong — a schedule that is not a
      schedule, a scope that cannot match anything. Saving it would store a
      configuration that can never work, so it is refused.
    * A **warning** is a value that is merely ineffective — a notify channel
      with no connector behind it. The agent still does its work; it just
      cannot tell anyone. Refusing that would block the *first* thing a new
      workspace has to do, since an agent cannot start until it is configured
      and nothing is connected yet. So it saves, and says so.

    Every error is reported at once rather than the first one, because fixing
    four things one round trip at a time is the worst version of this.
    """
    problems: list[str] = []
    warnings: list[str] = []
    choices: dict[str, str] | None = None

    if payload.schedule not in views.AGENT_SCHEDULES:
        problems.append("Schedule must be one of: " + ", ".join(views.AGENT_SCHEDULES))
    if payload.notify_channel not in views.AGENT_NOTIFY_CHANNELS:
        problems.append(
            "Notify channel must be one of: " + ", ".join(views.AGENT_NOTIFY_CHANNELS)
        )

    agent = lookup_agent(slug)
    if agent is not None:
        scope_problem = agent.validate_scope(payload.scope.strip())
        if scope_problem:
            problems.append(scope_problem)

        if agent.spec.requires_llm:
            choice = (payload.llm_connector or "").strip()
            if not choice:
                problems.append(
                    "Choose which AI model this agent should use. Connect one "
                    "under Connectors if none are listed."
                )
            else:
                llm_opts = {
                    option.slug: option for option in _llm_options(db, org)
                }
                option = llm_opts.get(choice)
                if option is None:
                    problems.append(
                        "That AI model cannot be used for writing. Pick one "
                        "from the list."
                    )
                elif not option.available:
                    problems.append(
                        f"{option.name} is not connected yet. "
                        f"{option.reason}"
                    )
        elif payload.strict_model:
            problems.append(
                "This agent doesn't write with an AI model, so \"Always use the model "
                "I picked\" doesn't apply to it. Switch it off."
            )

        if payload.connectors is not None:
            from app.agents.base.choice import saved_choices
            from app.services import agent_requirements, portal_features

            from app.services import entitlements

            choices, choice_problems = agent_requirements.clean_choices(
                agent,
                payload.connectors,
                usable=(usable := connector_service.build_connector_bundle(db, org).connected),
                enabled=set(portal_features.offered_connector_slugs(db, connected=usable)),
                saved=saved_choices(record.settings if record is not None else None),
                policy=entitlements.lookup(db, org),
            )
            problems.extend(choice_problems)

    if problems:
        raise InvalidInputError(
            problems[0]
            if len(problems) == 1
            else " ".join(f"({i + 1}) {p}" for i, p in enumerate(problems))
        )

    # Refused rather than warned about, now that the channel is wired to
    # something: an agent set to notify by Email with no mail connector would
    # do its work and drop every message, which is the quietest possible
    # failure. The dialog does not offer these, so reaching here means an API
    # caller or a form that was open while a connector was removed.
    #
    # Note this is a refusal of the *channel*, never of configuring the agent:
    # None is always available, so nothing about this blocks setup on a
    # workspace where nothing is connected yet.
    unavailable = {
        option.value: option.reason
        for option in _notify_options(db, org)
        if not option.available
    }
    if payload.notify_channel in unavailable:
        raise InvalidInputError(
            f"{payload.notify_channel} notifications cannot be delivered. "
            f"{unavailable[payload.notify_channel]} Or choose None."
        )
    return warnings, choices


def _assert_configured(record: AgentRecord) -> None:
    """Refuse to start an agent nobody has configured.

    Every agent ships with a working default schedule, so this gate is not
    about missing values — it is about a missing decision. These agents rewrite
    pages on a customer's live site, email strangers and move ad spend. "The
    defaults looked fine" is not an account anyone wants to give afterwards,
    so the platform requires that a person has opened the settings, seen the
    cadence and the daily cap, and saved them.

    Enforced here rather than only in the console, because a disabled button
    is a suggestion and this is a rule.
    """
    if not record.configured:
        raise ConflictError(
            f"Configure {record.name} before starting it — set its schedule, "
            "scope, AI model (when needed) and daily action cap, then start it."
        )


# ── Switches ───────────────────────────────────────────────────────────────
@router.post("/{slug}/pause", response_model=ActionResult)
def pause(
    slug: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    current.require_write(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)
    record.status = AgentStatus.PAUSED.value
    record.next_run_at = None
    audit.record_user_action(
        db, user=current.user, action=f"paused {record.name}", module="agents", ip_address=ip
    )
    db.commit()
    return ActionResult(toast=Toast(message=f"{record.name} paused"))


@router.post("/{slug}/resume", response_model=ActionResult)
def resume(
    slug: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    current.require_write(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)
    _assert_configured(record)
    _assert_not_halted(db, current)
    record.status = AgentStatus.RUNNING.value
    record.consecutive_failures = 0
    record.last_error = ""
    # Due immediately, so resuming produces visible work rather than a wait.
    from app.db.base import utcnow

    record.next_run_at = utcnow()
    audit.record_user_action(
        db, user=current.user, action=f"resumed {record.name}", module="agents", ip_address=ip
    )
    db.commit()
    return ActionResult(toast=Toast(message=f"{record.name} resumed", kind="success"))


@router.post("/{slug}/autonomy", response_model=ActionResult)
def set_autonomy(
    slug: str,
    payload: AutonomyRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ActionResult:
    """Flip one agent between autonomous execution and human review."""
    current.require_write(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)
    record.autonomy = payload.autonomous
    verb = "set to autonomous" if payload.autonomous else "set to human-in-the-loop"
    audit.record_user_action(
        db, user=current.user, action=f"{record.name} {verb}", module="agents", ip_address=ip
    )
    record_config_version(db, current.organization, record, actor=current.user)
    db.commit()
    return ActionResult(
        toast=Toast(
            message=(
                f"{record.name} will act independently"
                if payload.autonomous
                else f"{record.name} will queue actions for approval"
            )
        )
    )


@router.post("/autonomy", response_model=ActionResult)
def set_global_autonomy(
    payload: AutonomyRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ActionResult:
    """The header's Autonomous / Human review control — cascades to the fleet."""
    current.require_write(Module.AGENTS)
    current.organization.global_autonomy = payload.autonomous

    records = list(
        db.execute(
            select(AgentRecord).where(AgentRecord.tenant_id == current.tenant_id)
        ).scalars()
    )
    for record in records:
        record.autonomy = payload.autonomous

    message = (
        "All agents set to autonomous mode"
        if payload.autonomous
        else "All agents set to human-in-the-loop"
    )
    audit.record_user_action(
        db, user=current.user, action=message.lower(), module="agents", ip_address=ip
    )
    # The agents' switch is half of the workspace policy, and each agent's
    # own switch moved with it: both are kept as versions.
    from app.api.v1.onboarding import record_policy_version

    record_policy_version(db, current.organization, actor=current.user)
    for record in records:
        record_config_version(db, current.organization, record, actor=current.user)
    db.commit()
    return ActionResult(toast=Toast(message=message))


# ── Practice mode and the stop switches ────────────────────────────────────
def _assert_not_halted(db: DbSession, current: CurrentUserDep) -> None:
    """Refuse to start or run an agent while a stop switch is on."""
    from app.services import agent_safety

    reason = agent_safety.halt_reason(db, current.organization)
    if reason:
        raise ConflictError(reason)


def _apply_practice_choice(record: AgentRecord, choice: bool | None, *, first: bool) -> None:
    """Store a spending agent's practice switch.

    An explicit choice is stored as given. Without one, a first (new)
    configuration starts in practice mode; an existing one keeps what it had.
    """
    from app.agents.base import safety
    from app.services import agent_safety

    if not safety.is_spending_agent(record.slug):
        return
    if choice is None:
        if first:
            agent_safety.default_practice(record)
        return
    record.settings = {**(record.settings or {}), safety.PRACTICE_SETTING: bool(choice)}


@router.post("/{slug}/practice", response_model=AgentOut)
def set_practice(
    slug: str,
    payload: PracticeRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> AgentOut:
    """Switch one spending agent's practice mode on or off."""
    from app.agents.base import safety

    current.require_write(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)
    if not safety.is_spending_agent(slug):
        raise InvalidInputError(
            f"{record.name} does not spend money or change ad accounts, so it has no "
            "practice mode."
        )
    _apply_practice_choice(record, payload.on, first=False)
    audit.record_user_action(
        db,
        user=current.user,
        action=(
            f"switched on practice mode for {record.name}"
            if payload.on
            else f"switched off practice mode for {record.name}"
        ),
        module="agents",
        ip_address=ip,
    )
    record_config_version(db, current.organization, record, actor=current.user)
    db.commit()
    return views.agent_out(
        record,
        can_write=current.can_write(Module.AGENTS),
        env=views.guide_env(db, current.tenant_id),
    )


@router.get("/{slug}/practice-summary", response_model=PracticeSummaryOut)
def practice_summary(slug: str, current: CurrentUserDep, db: DbSession) -> PracticeSummaryOut:
    """What this agent would have changed in practice mode this week."""
    from app.agents.base import safety
    from app.services import agent_safety

    current.require_view(Module.AGENTS)
    _record(db, current.tenant_id, slug)
    if not safety.is_spending_agent(slug):
        raise NotFoundError("This agent has no practice mode")
    return PracticeSummaryOut(
        **agent_safety.practice_summary(db, tenant_id=current.tenant_id, slug=slug)
    )


# ── Configuration ──────────────────────────────────────────────────────────
def _setting_rule(field: SettingField) -> str:
    """What a valid value for ``field`` looks like, in plain words."""
    if field.kind in ("integer", "number"):
        noun = "a whole number" if field.kind == "integer" else "a number"
        if field.minimum is not None and field.maximum is not None:
            return f"{field.label} must be {noun} from {field.minimum:g} to {field.maximum:g}."
        if field.minimum is not None:
            return f"{field.label} must be {noun} of at least {field.minimum:g}."
        if field.maximum is not None:
            return f"{field.label} must be {noun} of at most {field.maximum:g}."
        return f"{field.label} must be {noun}."
    return f"{field.label} must be {field.max_length} characters or fewer."


def _apply_strict_choice(record: AgentRecord, choice: bool | None) -> None:
    """Store "Always use the model I picked"; ``None`` keeps the saved value."""
    from app.orchestration.runner import STRICT_MODEL_SETTING

    if choice is None:
        return
    saved = dict(record.settings or {})
    if choice:
        saved[STRICT_MODEL_SETTING] = True
    else:
        saved.pop(STRICT_MODEL_SETTING, None)
    record.settings = saved


def _apply_config(
    slug: str, record: AgentRecord, payload: AgentConfigRequest, choices: dict[str, str] | None
) -> Any:  # noqa: ANN401 - the agent implementation, or None
    """Write a configuration already checked by :func:`_check_config` onto ``record``.

    Shared by a save and by restoring an earlier version. Returns the agent
    implementation (None when this build lacks it) so the caller can let it
    react. Flushes nothing and commits nothing.
    """
    record.schedule = payload.schedule
    record.scope = payload.scope.strip()
    record.notify_channel = payload.notify_channel
    agent = lookup_agent(slug)
    if agent is not None and agent.spec.requires_llm:
        record.llm_connector = (payload.llm_connector or "").strip()
    else:
        record.llm_connector = ""
    record.max_actions_per_day = payload.max_actions_per_day
    if payload.settings:
        from app.agents.base.plain import guide_for

        declared = {spec.key: spec for spec in (guide_for(slug).settings if guide_for(slug) else ())}
        unknown = sorted(set(payload.settings) - set(declared))
        if unknown:
            raise InvalidInputError(f"{record.name} has no setting called {', '.join(unknown)}")
        saved = dict(record.settings or {})
        for key, raw in payload.settings.items():
            try:
                value = declared[key].clean(raw)
            except ValueError as exc:
                # The message is written from the declared field, never from
                # the exception text.
                log.info("Agent %s setting %s rejected (%s)", slug, key, type(exc).__name__)
                raise InvalidInputError(_setting_rule(declared[key])) from None
            if value:
                saved[key] = value
            else:
                saved.pop(key, None)
        # A new dict, so the JSONB column is seen as changed.
        record.settings = saved
    if choices is not None:
        from app.agents.base.choice import CHOICES_KEY

        saved = dict(record.settings or {})
        if choices:
            saved[CHOICES_KEY] = choices
        else:
            saved.pop(CHOICES_KEY, None)
        # A selection supersedes the older setting it replaces (the social
        # manager's "networks"): one source of truth from this save on.
        for use in agent.spec.connector_use if agent is not None else ():
            if use.replaces_setting and use.capability.value in (payload.connectors or {}):
                saved.pop(use.replaces_setting, None)
        record.settings = saved
    _apply_practice_choice(record, payload.practice_mode, first=not record.configured)
    _apply_strict_choice(record, payload.strict_model if agent is not None and agent.spec.requires_llm else False)
    record.configured = True

    # The new cadence takes effect from now rather than from the old timer.
    from app.orchestration.runner import SCHEDULE_INTERVALS
    from app.db.base import utcnow

    interval = SCHEDULE_INTERVALS.get(payload.schedule)
    if interval and record.status == AgentStatus.RUNNING.value:
        record.next_run_at = utcnow() + interval
    return agent


# ── Versions of an agent's settings (history kind ``agent_config``) ────────
def _versioned_setting_keys(slug: str) -> set[str]:
    """The ``AgentRecord.settings`` keys that are configuration, not agent state.

    Agents also keep working state in ``settings`` (``ctx.set_setting``);
    that is never versioned and never overwritten by a restore.
    """
    from app.agents.base import safety
    from app.agents.base.choice import CHOICES_KEY
    from app.agents.base.plain import guide_for
    from app.orchestration.runner import STRICT_MODEL_SETTING

    guide = guide_for(slug)
    declared = {spec.key for spec in guide.settings} if guide else set()
    return declared | {safety.PRACTICE_SETTING, STRICT_MODEL_SETTING, CHOICES_KEY}


def config_document(record: AgentRecord) -> dict[str, Any]:
    """One agent's configuration as a version document (``history.config.FIELDS``)."""
    stored = record.settings or {}
    return {
        "schedule": record.schedule,
        "scope": record.scope or "",
        "notify_channel": record.notify_channel,
        "llm_connector": record.llm_connector or "",
        "max_actions_per_day": record.max_actions_per_day,
        "autonomy": bool(record.autonomy),
        "settings": {
            key: stored[key] for key in sorted(_versioned_setting_keys(record.slug)) if key in stored
        },
    }


def record_config_version(
    db: Session,
    org: Organization,
    record: AgentRecord,
    *,
    actor: Any,  # noqa: ANN401 - the User who made the change
    summary: str | None = None,
) -> AuditSnapshot | None:
    """Keep this agent's configuration as its next version; never fails the save."""
    return history_store.guarded(
        db,
        SnapshotKind.AGENT_CONFIG,
        lambda: history.record_version(
            db, org, SnapshotKind.AGENT_CONFIG, record.id, config_document(record),
            actor=actor, summary=summary,
        ),
    )


#: Shown when a stored version no longer passes today's rules.
_RESTORE_INVALID = (
    "This version can't be restored because some of its settings are no longer "
    "valid. Open the agent's settings and choose them again."
)


def _request_from_document(record: AgentRecord, document: dict[str, Any]) -> AgentConfigRequest:
    """Rebuild a configuration request from a stored version, re-validated."""
    from app.agents.base import safety
    from app.agents.base.choice import CHOICES_KEY
    from app.agents.base.plain import guide_for
    from app.orchestration.runner import STRICT_MODEL_SETTING

    stored = document.get("settings")
    stored = stored if isinstance(stored, dict) else {}
    guide = guide_for(record.slug)
    # Every declared setting is sent, "" for one the version did not have,
    # so the restore removes values set since.
    settings: dict[str, Any] = {spec.key: stored.get(spec.key, "") for spec in (guide.settings if guide else ())}
    practice = stored.get(safety.PRACTICE_SETTING)
    choices = stored.get(CHOICES_KEY)
    if not isinstance(choices, dict):
        # No selection in the version means "automatic" for an agent that
        # offers one; an agent that offers none has nothing to change.
        agent = lookup_agent(record.slug)
        choices = {} if agent is not None and agent.spec.connector_use else None
    try:
        return AgentConfigRequest.model_validate(
            {
                "schedule": document.get("schedule"),
                "scope": document.get("scope") or "",
                "notify_channel": document.get("notify_channel"),
                "llm_connector": document.get("llm_connector") or "",
                "max_actions_per_day": document.get("max_actions_per_day"),
                "settings": settings,
                "connectors": choices,
                "practice_mode": practice if isinstance(practice, bool) else None,
                "strict_model": stored.get(STRICT_MODEL_SETTING) is True,
            }
        )
    except ValidationError:
        log.info("Agent %s version not restorable: failed validation", record.slug)
        raise InvalidInputError(_RESTORE_INVALID) from None


@history.register_restorer(SnapshotKind.AGENT_CONFIG)
def restore_config(
    db: Session,
    org: Organization,
    subject_id: str,
    document: dict[str, Any],
    *,
    user: Any,  # noqa: ANN401 - the User restoring
) -> dict[str, Any]:
    """Write a stored version of one agent's settings back (``history.restore``).

    The agent is loaded through a tenant-filtered query and the version is
    checked against today's rules, exactly as a save is. The history service
    records the new version and the audit entry, and the caller commits.
    """
    record = db.execute(
        select(AgentRecord).where(AgentRecord.tenant_id == org.id, AgentRecord.id == subject_id)
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError("That agent was not found")
    # Also refuses an agent switched off for this workspace.
    record = _record(db, org.id, record.slug)
    payload = _request_from_document(record, document)
    _, choices = _check_config(db, org, record.slug, payload, record)
    agent = _apply_config(record.slug, record, payload, choices)
    autonomy = document.get("autonomy")
    if isinstance(autonomy, bool):
        record.autonomy = autonomy
    if agent is not None:
        from app.orchestration.runner import build_context

        agent.on_configure(build_context(db, record, org, trigger="config"), payload.model_dump())
    return config_document(record)


@router.put("/{slug}/config", response_model=AgentOut)
def configure(
    slug: str,
    payload: AgentConfigRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> AgentOut:
    current.require_write(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)
    warnings, choices = _validate_config(db, current, slug, payload, record)
    agent = _apply_config(slug, record, payload, choices)

    # Let the agent react to its own configuration change.
    from app.orchestration.runner import build_context

    if agent is not None:
        agent.on_configure(
            build_context(db, record, current.organization, trigger="config"),
            payload.model_dump(),
        )

    audit.record_user_action(
        db,
        user=current.user,
        action=f"reconfigured {record.name} ({payload.schedule})",
        module="agents",
        ip_address=ip,
    )
    record_config_version(db, current.organization, record, actor=current.user)
    db.commit()
    out = views.agent_out(record, can_write=True, env=views.guide_env(db, current.tenant_id))
    out.warnings = warnings
    return out


@router.delete("/{slug}/config", response_model=AgentOut)
def reset_config(
    slug: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> AgentOut:
    """Remove an agent's configuration and stop it.

    The counterpart of saving one. Without this the settings dialog is a
    one-way door: you can change a schedule but never take back the decision
    that let the agent run at all, which makes the configuration gate feel
    like a trap rather than a switch.

    It stops the agent as part of the same action, because leaving it running
    with no configuration would contradict the rule that starting requires
    one — the state would exist but be unreachable by any other path.

    Its run history and anything it has already produced are untouched. This
    resets a setting; it does not erase what the agent did.
    """
    current.require_write(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)

    agent = lookup_agent(slug)
    if agent is None:
        # The record exists but the implementation does not, which means this
        # build is missing the agent rather than the operator doing anything
        # wrong. Guessing its defaults would hide that.
        raise NotFoundError(
            f"Agent {slug!r} has no implementation on this build, so its "
            "configuration cannot be reset."
        )
    spec = agent.spec

    record.configured = False
    record.status = AgentStatus.PAUSED.value
    record.next_run_at = None
    record.scope = ""
    record.schedule = spec.default_schedule
    record.notify_channel = settings.default_notify_channel
    record.llm_connector = ""
    # Which connector it uses goes with the rest of the configuration.
    from app.agents.base.choice import CHOICES_KEY

    if CHOICES_KEY in (record.settings or {}):
        record.settings = {k: v for k, v in (record.settings or {}).items() if k != CHOICES_KEY}
    # So does keeping to that model.
    _apply_strict_choice(record, False)
    record.max_actions_per_day = spec.default_max_actions_per_day
    # The next configuration is a new one, and starts in practice mode.
    _apply_practice_choice(record, True, first=True)
    record.metric_label = "Not started yet"
    record.last_error = ""
    record.consecutive_failures = 0

    audit.record_user_action(
        db,
        user=current.user,
        action=f"removed the configuration for {record.name} and stopped it",
        module="agents",
        ip_address=ip,
    )
    record_config_version(
        db, current.organization, record, actor=current.user, summary="Agent settings removed"
    )
    db.commit()
    return views.agent_out(record, can_write=True, env=views.guide_env(db, current.tenant_id))


# ── Manual run ─────────────────────────────────────────────────────────────
def _run_in_background(tenant_id: str, slug: str, triggered_by: str) -> None:
    """Execute one pass on its own session, outside any request.

    Its own session on purpose: the request's is closed by the time this
    runs, and a run must not depend on a connection whose lifetime is tied to
    a browser that may already be gone.
    """
    from app.db.session import session_scope
    from app.orchestration.runner import run_agent_by_slug

    try:
        with session_scope(tenant_id) as db:
            outcome = run_agent_by_slug(
                db, tenant_id=tenant_id, slug=slug, trigger="manual",
                triggered_by=triggered_by, force=True,
            )
            log.info("manual run of %s: %s — %s", slug, outcome.status, outcome.summary)
    except Exception:  # noqa: BLE001 - nothing is left to report it to
        # Nobody is waiting on this response, so a traceback in the log is the
        # only place it can go. The run row itself records the failure.
        log.exception("Manual run of %s failed outside the request", slug)


def _already_running(db: DbSession, tenant_id: str, slug: str) -> bool:
    """Whether a pass of this agent is in flight.

    ``force=True`` skips the scheduler's claim, so without this a second click
    on Run now starts a second pass: two lots of model spend, and under full
    autonomy every action applied twice. Bounded by the same lease the
    scheduler uses, so a process killed mid-run cannot wedge the button.
    """
    from datetime import timedelta

    from app.db.base import utcnow
    from app.models.agent import AgentRun, RunStatus

    cutoff = utcnow() - timedelta(minutes=settings.scheduler_claim_lease_minutes)
    return (
        db.execute(
            select(AgentRun.id).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.agent_slug == slug,
                AgentRun.status == RunStatus.RUNNING.value,
                AgentRun.started_at > cutoff,
            )
        ).first()
        is not None
    )


@router.post("/{slug}/run", response_model=ActionResult)
def run_now(
    slug: str,
    background: BackgroundTasks,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ActionResult:
    """Start a pass now, even if the agent is paused.

    Started, not awaited. A pass that analyses six pages makes six model
    calls, and holding the request open for that long meant a refresh looked
    like a cancellation and a proxy timeout looked like a failure — for a run
    that was in fact completing on the server either way.

    Still gated on configuration. A manual run is not a preview — it
    publishes, mails and spends exactly as a scheduled run does, so it cannot
    be the way round the requirement to look at the settings first.
    """
    current.require_write(Module.AGENTS)
    record = _record(db, current.tenant_id, slug)
    _assert_configured(record)
    _assert_not_halted(db, current)

    if _already_running(db, current.tenant_id, slug):
        return ActionResult(
            ok=True,
            toast=Toast(
                message=f"{record.name} is already running. It finishes on the "
                "server whether or not this page is open.",
                kind="info",
            ),
        )

    audit.record_user_action(
        db,
        user=current.user,
        action=f"started a manual run of {record.name}",
        module="agents",
        ip_address=ip,
    )
    # Committed before the task is queued: the background session is a
    # different transaction and must be able to see this.
    db.commit()

    background.add_task(
        _run_in_background, current.tenant_id, slug, current.user.name
    )
    return ActionResult(
        ok=True,
        toast=Toast(
            message=f"{record.name} started. It runs on the server — you can "
            "leave this page, and the result appears in its run history.",
            kind="success",
        ),
    )
