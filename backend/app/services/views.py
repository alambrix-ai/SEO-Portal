"""View-model builders.

Every label the console renders — a status word, a countdown, a relative
timestamp — is produced here rather than in the frontend, so the API is the
single place those rules live and two screens showing the same object cannot
disagree about it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.agents.base.policy import GUARDRAIL_LABELS, autonomy_label
from app.agents.base.registry import get_agent
from app.connectors.base import registry as connector_registry
from app.core.rbac import ROLE_LABELS, Module, Role
from app.db.base import utcnow
from app.models.ads import CHANNEL_CONNECTOR, CHANNEL_LABELS, AdCreative, AudienceCluster
from app.models.ads import BudgetAllocation, FraudEvent
from app.models.agent import AgentRecord, AgentRun, AgentStatus
from app.models.approval import ApprovalItem
from app.models.connector import ConnectorRecord
from app.models.offpage import BacklinkTarget, CompetitorAlert
from app.models.seo import SeoIssue, PAGE_STATUS_LABELS, PageStatus, ReferralSpamEvent, SeoPage
from app.models.workspace import AuditLogEntry, User
from app.schemas.workspace import (
    AgentOut,
    AgentRunOut,
    ApprovalOut,
    AudienceOut,
    AuditEntryOut,
    BacklinkOut,
    BudgetChannelOut,
    CompetitorAlertOut,
    ConnectorOut,
    CreativeOut,
    CredentialFieldOut,
    FraudEventOut,
    QaPairOut,
    ReferralSpamOut,
    SchemaPatchOut,
    SeoIssueOut,
    SeoPageOut,
    TeamMemberOut,
)
from app.services.encryption import Ctx, OrgCipher

# ── Time formatting ────────────────────────────────────────────────────────
def countdown(target: datetime | None, *, continuous: bool = False) -> str:
    """Render ``next_run_at`` the way the agent cards do: ``12m``, ``2h``."""
    if continuous:
        return "ongoing"
    if target is None:
        return "—"
    delta = target - utcnow()
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "due"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def since(moment: datetime | None) -> str:
    """``just now``, ``12m ago``, ``3d ago`` — for a card's last-activity line.

    The counterpart of :func:`countdown`, which looks forward. Both live here
    rather than in the console because two screens showing the same moment
    must not describe it differently, and changing the wording should not mean
    changing it in two languages.
    """
    if moment is None:
        return ""
    seconds = int((utcnow() - moment).total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"


def clock(moment: datetime | None) -> str:
    """``HH:MM`` for the event logs."""
    return moment.strftime("%H:%M") if moment else "--:--"


def stamp(moment: datetime | None) -> str:
    """``Sep 7, 10:12`` for the audit log."""
    return moment.strftime("%b %-d, %H:%M") if moment else ""


def _stamp_safe(moment: datetime | None) -> str:
    # %-d is not portable to Windows, so strip the padding manually.
    if moment is None:
        return ""
    return moment.strftime("%b %d, %H:%M").replace(" 0", " ", 1)


# ── Agents ─────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class GuideEnv:
    """Workspace facts the plain-language agent guide is computed from."""

    #: Connector slugs a run would receive: connected, with complete credentials.
    usable: frozenset[str]
    enabled_connectors: set[str]
    guardrail: str
    #: On-Page SEO Sync has collected at least one page.
    has_pages: bool
    #: ``(agent slug, capability) -> ConnectorPolicy`` — what the plan allows
    #: (app.services.entitlements). None means unrestricted.
    policy: object = None
    #: Practice mode set above the agent: "platform", "workspace" or "".
    practice: str = ""
    #: ``slug -> bool``: verified against a live account
    #: (app.services.verification). None when not looked up.
    verified: object = None


def guide_env(db: Session, tenant_id: str) -> GuideEnv:
    from app.models.workspace import OnboardingState, Organization
    from app.services import portal_features
    from app.services.connectors import build_connector_bundle

    org = db.get(Organization, tenant_id)
    usable = build_connector_bundle(db, org).connected if org is not None else frozenset()
    state = db.execute(
        select(OnboardingState).where(OnboardingState.tenant_id == tenant_id)
    ).scalar_one_or_none()
    has_pages = (
        db.execute(
            select(func.count()).select_from(SeoPage).where(SeoPage.tenant_id == tenant_id)
        ).scalar_one()
        > 0
    )
    from app.services import entitlements

    return GuideEnv(
        usable=usable,
        # Unverified types hidden in Portal Admin are not offered as options
        # (one this workspace already connected still is).
        enabled_connectors=set(portal_features.offered_connector_slugs(db, connected=usable)),
        guardrail=state.guardrail if state else "hybrid",
        has_pages=has_pages,
        policy=entitlements.lookup(db, org),
        **_safety_env(db, org),
    )


def _safety_env(db: Session, org) -> dict:  # noqa: ANN001
    """Practice mode above the agent, and the verification lookup."""
    from app.services import agent_safety, verification

    if org is None:
        return {}
    if agent_safety.platform_switch(db, agent_safety.PLATFORM_PRACTICE):
        practice = "platform"
    elif agent_safety.workspace_practice_on(org):
        practice = "workspace"
    else:
        practice = ""
    seen: dict[str, bool] = {}

    def verified(slug: str) -> bool:
        if slug not in seen:
            try:
                seen[slug] = bool(verification.is_verified(db, slug))
            except Exception:  # noqa: BLE001 - unknown means unproven
                seen[slug] = False
        return seen[slug]

    return {"practice": practice, "verified": verified}


def _need_options(options) -> list:  # noqa: ANN001
    from app.schemas.workspace import NeedOptionOut

    return [
        NeedOptionOut(
            slug=o.slug,
            name=o.name,
            connected=o.connected,
            locked=getattr(o, "locked", False),
            locked_reason=getattr(o, "locked_reason", ""),
        )
        for o in options
    ]


def _need_uses(uses) -> list:  # noqa: ANN001
    from app.schemas.workspace import NeedUseOut

    return [
        NeedUseOut(
            capability=u.capability,
            mode=u.mode,
            question=u.question,
            primary=u.primary,
            control=u.control,
            automatic_option=u.automatic_option,
            chosen=list(u.chosen),
            selected=list(u.selected),
            stale=u.stale,
            using=_need_options(u.using),
            options=_need_options(u.options),
            statement=u.statement,
            automatic_statement=u.automatic_statement,
            template=u.template,
            note=u.note,
        )
        for u in uses
    ]


def _guide_fields(record: AgentRecord, env: GuideEnv | None) -> dict:
    """Summary, effect, prerequisites and approval sentence for one agent."""
    from app.agents.base.plain import guide_for
    from app.agents.base.policy import resolve_guardrail
    from app.schemas.workspace import AgentNeedOut, NeedOptionOut
    from app.services import agent_requirements as req

    agent = get_agent(record.slug)
    guide = guide_for(record.slug)
    if agent is None or guide is None:
        return {}
    fields: dict = {
        "summary": guide.summary,
        "effect": guide.effect,
        "makes_changes": guide.makes_changes,
        "scope_label": guide.scope_label,
        "scope_help": guide.scope_help,
        "uses_scope": guide.uses_scope,
        "settings_fields": [
            {
                "key": spec.key,
                "label": spec.label,
                "help": spec.help,
                "kind": spec.kind,
                "default": spec.default,
                "value": str((record.settings or {}).get(spec.key, "") or ""),
            }
            for spec in guide.settings
        ],
    }
    if env is None:
        return fields

    from app.agents.base.choice import saved_choices

    choices = saved_choices(record.settings)
    described: set[str] = set()
    needs: list[AgentNeedOut] = [
        AgentNeedOut(
            key=need.key,
            label=need.label,
            satisfied=need.satisfied,
            note=need.note,
            options=_need_options(need.options),
            uses=_need_uses(need.uses),
        )
        for need in req.needs_for(
            agent,
            usable=env.usable,
            enabled=env.enabled_connectors,
            llm_choice=record.llm_connector or "",
            choices=choices,
            described=described,
            policy=env.policy,
            settings=record.settings,
        )
    ]
    if guide.uses_data_from:
        source = get_agent(guide.uses_data_from)
        name = source.spec.name if source else guide.uses_data_from
        needs.append(
            AgentNeedOut(
                key=f"data:{guide.uses_data_from}",
                kind="agent",
                label=f"Pages collected by {name}",
                satisfied=env.has_pages,
                note=f"Set up and run {name} first. This agent works on the pages it collects.",
                options=[
                    NeedOptionOut(slug=guide.uses_data_from, name=name, connected=env.has_pages)
                ],
            )
        )
    if guide.scope_required_setting:
        filled = bool((record.scope or "").strip()) or bool(
            (record.settings or {}).get(guide.scope_required_setting)
        )
        needs.append(
            AgentNeedOut(
                key="scope",
                kind="setting",
                label=guide.scope_label.replace(" (required)", ""),
                satisfied=filled,
                note="Enter it in this agent's settings.",
            )
        )
    for extra in guide.optional:
        options = req.options_for(
            extra.capability, usable=env.usable, enabled=env.enabled_connectors
        )
        needs.append(
            AgentNeedOut(
                key=extra.capability.value,
                label=req.plain(extra.capability),
                satisfied=any(o.connected for o in options),
                optional=True,
                note=extra.note,
                options=_need_options(options),
                uses=_need_uses(
                    req.uses_for(
                        agent,
                        (extra.capability,),
                        usable=env.usable,
                        enabled=env.enabled_connectors,
                        choices=choices,
                        described=described,
                        policy=env.policy,
                        settings=record.settings,
                    )
                ),
            )
        )
    fields["needs"] = needs
    fields["ready"] = all(n.satisfied for n in needs if not n.optional)
    rail = resolve_guardrail(env.guardrail)
    for key, autonomous in (
        ("approval_behaviour", record.autonomy),
        ("approval_if_auto", True),
        ("approval_if_review", False),
    ):
        fields[key] = req.approval_behaviour(
            agent, guardrail=rail, autonomous=autonomous, makes_changes=guide.makes_changes
        )
    fields["guardrail"] = rail.value
    return fields


def strict_model_on(record: AgentRecord) -> bool:
    """The agent's "Always use the model I picked" switch, as the runner reads it."""
    from app.orchestration.runner import STRICT_MODEL_SETTING

    return (getattr(record, "settings", None) or {}).get(STRICT_MODEL_SETTING) is True


def agent_out(
    record: AgentRecord,
    *,
    can_write: bool,
    run: AgentRun | None = None,
    env: GuideEnv | None = None,
) -> AgentOut:
    """One agent card.

    ``run`` is the most recent run row, when the caller has it. Passed in
    rather than queried here so a list of twelve agents is two queries and
    not twenty-five.
    """
    agent = get_agent(record.slug)
    continuous = bool(agent and agent.spec.continuous)
    requires_llm = bool(agent and agent.spec.requires_llm)
    config_summary = ""
    if record.configured:
        parts = [
            f"Configured: {record.schedule}",
            f"notify {record.notify_channel}",
            f"max {record.max_actions_per_day}/day",
        ]
        if requires_llm:
            from app.connectors.base import registry as connector_registry
            from app.llm.from_connector import effective_llm

            using = (
                effective_llm(record.llm_connector, env.usable)[0]
                if env is not None
                else (record.llm_connector or "")
            )
            if using:
                spec = connector_registry.get_spec(using)
                parts.insert(1, f"model {spec.name if spec else using}")
        config_summary = " · ".join(parts)

    return AgentOut(
        id=str(record.id or ""),
        slug=record.slug,
        name=record.name,
        category=record.category,
        description=record.description,
        status=record.status,
        status_label=_agent_status_label(record.status),
        autonomy=record.autonomy,
        autonomy_label=autonomy_label(record.autonomy),
        metric_label=record.metric_label,
        next_run=countdown(record.next_run_at, continuous=continuous)
        if record.status == AgentStatus.RUNNING.value
        else "—",
        schedule=record.schedule,
        scope=record.scope,
        scope_placeholder=agent.spec.scope_placeholder if agent else "",
        group=AGENT_GROUPS.get(agent_rank(record), "idle"),
        **_run_progress(run),
        notify_channel=record.notify_channel,
        llm_connector=record.llm_connector or "",
        requires_llm=requires_llm,
        max_actions_per_day=record.max_actions_per_day,
        configured=record.configured,
        config_summary=config_summary,
        last_error=record.last_error,
        actions_today=record.actions_today,
        read_only=not can_write,
        strict_model=requires_llm and strict_model_on(record),
        **_safety_fields(record, env),
        **_guide_fields(record, env),
    )


# ── Runs ───────────────────────────────────────────────────────────────────
RUN_STATE_LABELS: dict[str, str] = {
    "running": "Running",
    "interrupted": "Interrupted",
    "succeeded": "Completed",
    "skipped": "Did not go ahead",
    "failed": "Failed",
}


def _parse_moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{max(seconds, 1)}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def run_state(run: AgentRun) -> str:
    """A run's state, with abandoned passes told apart from live ones.

    Uses the scheduler's claim lease — the same bound ``_already_running``
    uses to unblock Run now — so the console and the server agree on when a
    pass is presumed dead.
    """
    from app.core.config import settings
    from app.models.agent import RunStatus

    if run.status == RunStatus.RUNNING.value:
        lease = timedelta(minutes=settings.scheduler_claim_lease_minutes)
        if run.started_at is not None and utcnow() - run.started_at > lease:
            return "interrupted"
        return "running"
    return run.status


def run_out(run: AgentRun, *, agent_name: str = "", events: list | None = None) -> AgentRunOut:
    from app.schemas.workspace import RunEventOut, RunStepOut

    state = run_state(run)
    timeline = []
    for entry in (run.detail or {}).get("timeline") or []:
        total = int(entry.get("total") or 0)
        done = int(entry.get("done") or 0)
        timeline.append(
            RunStepOut(
                label=str(entry.get("label") or ""),
                text=str(entry.get("text") or entry.get("label") or ""),
                done=done,
                total=total,
                percent=round(done / total * 100) if total > 0 else None,
                at=_parse_moment(entry.get("at")),
                ended_at=_parse_moment(entry.get("ended_at")),
            )
        )
    last_update = None
    if timeline:
        raw = ((run.detail or {}).get("timeline") or [])[-1]
        last_update = _parse_moment(raw.get("updated_at")) or timeline[-1].at
    if state == "running":
        elapsed = int((utcnow() - run.started_at).total_seconds()) if run.started_at else 0
    else:
        elapsed = int((run.duration_ms or 0) / 1000)
    total = run.step_total or 0
    return AgentRunOut(
        id=run.id,
        agent_slug=run.agent_slug,
        agent_name=agent_name,
        trigger=run.trigger,
        triggered_by=run.triggered_by or "",
        status=run.status,
        state=state,
        state_label=RUN_STATE_LABELS.get(state, state.title()),
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms or 0,
        duration_label=_duration(elapsed) if state != "interrupted" else "",
        actions_taken=run.actions_taken or 0,
        actions_queued=run.actions_queued or 0,
        summary=run.summary or "",
        error=run.error or "",
        cost=run.cost or 0.0,
        step=run.step if state == "running" else "",
        step_percent=(
            round((run.step_done or 0) / total * 100) if state == "running" and total > 0 else None
        ),
        last_update_at=last_update,
        timeline=timeline,
        events=[
            RunEventOut(
                at=entry.at,
                action=entry.action,
                awaiting_approval=bool((entry.context or {}).get("awaiting_approval")),
                practice=bool((entry.context or {}).get("practice")),
                held=[str(n) for n in ((entry.context or {}).get("held") or [])],
            )
            for entry in (events or [])
        ],
        actions_practised=int((run.detail or {}).get("practised") or 0),
        practice=str((run.detail or {}).get("practice") or ""),
    )


def run_events(db: Session, *, tenant_id: str, run_id: str) -> list[AuditLogEntry]:
    """Audit entries a run recorded, oldest first."""
    return list(
        db.execute(
            select(AuditLogEntry)
            .where(
                AuditLogEntry.tenant_id == tenant_id,
                AuditLogEntry.context["run_id"].astext == run_id,
            )
            .order_by(AuditLogEntry.at)
            .limit(200)
        ).scalars()
    )


def agent_activity(db: Session, *, tenant_id: str, recent: int = 12) -> dict:
    """Runs open now (live or interrupted), and the most recent finished ones."""
    from app.models.agent import RunStatus
    from app.services import portal_features

    names = {
        r.slug: r.name
        for r in db.execute(select(AgentRecord).where(AgentRecord.tenant_id == tenant_id)).scalars()
    }
    allowed = portal_features.enabled_agent_slugs(db)
    open_runs = db.execute(
        select(AgentRun)
        .where(AgentRun.tenant_id == tenant_id, AgentRun.status == RunStatus.RUNNING.value)
        .order_by(AgentRun.started_at.desc())
        .limit(50)
    ).scalars()
    finished = db.execute(
        select(AgentRun)
        .where(AgentRun.tenant_id == tenant_id, AgentRun.status != RunStatus.RUNNING.value)
        .order_by(AgentRun.started_at.desc())
        .limit(recent * 2)
    ).scalars()
    running_out = [
        run_out(run, agent_name=names.get(run.agent_slug, run.agent_slug))
        for run in open_runs
        if run.agent_slug in allowed
    ]
    # Only live passes are "running"; interrupted ones belong with history.
    live = [r for r in running_out if r.state == "running"]
    interrupted = [r for r in running_out if r.state == "interrupted"]
    recent_out = [
        run_out(run, agent_name=names.get(run.agent_slug, run.agent_slug))
        for run in finished
        if run.agent_slug in allowed
    ]
    merged = sorted(interrupted + recent_out, key=lambda r: r.started_at, reverse=True)[:recent]
    return {"running": live, "recent": merged}


def _agent_status_label(status: str) -> str:
    return {
        AgentStatus.RUNNING.value: "Running",
        AgentStatus.PAUSED.value: "Paused",
        AgentStatus.ERROR.value: "Error",
    }.get(status, status.title())


def agent_rank(record: AgentRecord) -> int:
    """How near the top of the list this agent belongs.

    The catalogue order is the right order for a catalogue and the wrong one
    for a fleet somebody is running: on a workspace with two agents working
    and nine untouched, the two that matter were wherever the alphabet put
    them. So the list is grouped by how much attention the agent wants:

    0. **Errored** — it was working and has stopped. The most urgent thing on
       the screen, and the reason error sorts above running rather than below
       it.
    1. **Running** — doing the work; this is what somebody came to look at.
    2. **Configured but stopped** — set up and ready, one click from live.
    3. **Untouched** — still a catalogue entry.

    Within a group the fleet's own order holds, so the pipeline still reads
    front to back.
    """
    if record.status == AgentStatus.ERROR.value:
        return 0
    if record.status == AgentStatus.RUNNING.value:
        return 1
    return 2 if record.configured else 3


def _run_progress(run: AgentRun | None) -> dict:
    """What a card says about the pass that is open, or the one that ended.

    A pass is started and not awaited now, so a status of "running" on its own
    could mean working or wedged. The step is what the agent last said it was
    doing; the percentage is only offered where the step has a countable
    total, because a bar that invents its own progress is worse than no bar.
    """
    
    if run is None:
        return {}

    busy = run_state(run) == "running"
    total = run.step_total or 0
    percent = round(run.step_done / total * 100) if busy and total > 0 else None
    return {
        "busy": busy,
        "step": run.step if busy else "",
        "step_done": run.step_done if busy else 0,
        "step_total": total if busy else 0,
        "step_percent": percent,
        "busy_for": f"running for {_elapsed(run.started_at)}" if busy else "",
        # The finished pass's own words. Shown when nothing is open, so the
        # card reports what it last achieved rather than only its status.
        "last_run_summary": ""
        if busy
        else (
            "The previous run was interrupted before it finished — it stopped "
            "reporting and will not resume. The next scheduled run starts fresh."
            if run_state(run) == "interrupted"
            else (run.summary or "")
        ),
        "last_run_status": "" if busy else run_state(run),
    }


def _elapsed(since: datetime | None) -> str:
    """A short duration, for "running for 40s"."""
    from app.db.base import utcnow

    if since is None:
        return ""
    seconds = int((utcnow() - since).total_seconds())
    if seconds < 60:
        return f"{max(seconds, 1)}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"

#: rank -> the group a card is filed under. Named for the state, not for a
#: heading: the console owns the wording, this owns the rule.
AGENT_GROUPS: dict[int, str] = {
    0: "attention",
    1: "running",
    2: "ready",
    3: "idle",
}


def list_agents(db: Session, *, tenant_id: str, can_write: bool) -> list[AgentOut]:
    from app.agents.base.registry import FLEET_ORDER
    from app.services import portal_features

    records = list(
        db.execute(select(AgentRecord).where(AgentRecord.tenant_id == tenant_id)).scalars()
    )
    allowed = portal_features.enabled_agent_slugs(db)
    records = [r for r in records if r.slug in allowed]
    order = {slug: i for i, slug in enumerate(FLEET_ORDER)}
    records.sort(key=lambda r: (agent_rank(r), order.get(r.slug, len(order)), r.name))

    # The newest run for each agent, in one query rather than one per card.
    newest: dict[str, AgentRun] = {}
    for run in db.execute(
        select(AgentRun)
        .where(AgentRun.tenant_id == tenant_id)
        .order_by(AgentRun.started_at.desc())
        .limit(200)
    ).scalars():
        newest.setdefault(run.agent_slug, run)

    env = guide_env(db, tenant_id)
    return [
        agent_out(record, can_write=can_write, run=newest.get(record.slug), env=env)
        for record in records
    ]


# ── Connectors ─────────────────────────────────────────────────────────────
#: rank -> section. A connected integration that is failing is the one row
#: somebody needs today, so it gets its own heading rather than sitting
#: quietly among the healthy ones.
CONNECTOR_GROUPS: dict[int, str] = {0: "attention", 1: "connected", 2: "available"}


def _connector_rank(record: ConnectorRecord) -> int:
    """Imported lazily: services.connectors imports this module for its own
    output shapes, so a module-level import here would be circular."""
    from app.services.connectors import connector_rank

    return connector_rank(record)


@dataclass(slots=True)
class ConnectContext:
    """Workspace facts the connect screen needs beyond one record."""

    #: A workspace admin or a portal admin: sees who can switch a sign-in
    #: on, and the developer route.
    show_admin_notes: bool = False
    #: A portal admin: can switch a sign-in on themselves.
    is_portal_admin: bool = False
    #: provider -> the address already signed in for this workspace.
    accounts: dict[str, str] = field(default_factory=dict)
    #: provider -> [(slug, name)] of connectors not yet connected.
    not_connected: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def connect_guide(record: ConnectorRecord, ctx: ConnectContext | None = None):  # noqa: ANN201
    """How the connect screen presents one integration. See ConnectGuideOut."""
    from app.connectors.base import oauth_providers
    from app.connectors.base.credentials import OAUTH_ACCOUNT_KEY, from_platform_sign_in
    from app.schemas.workspace import ConnectGuideOut, ConnectLinkOut

    ctx = ctx or ConnectContext()
    spec = connector_registry.get_spec(record.slug)
    if spec is None:
        return ConnectGuideOut()

    if spec.oauth is not None:
        provider = oauth_providers.provider_for(spec)
        label = provider.label if provider else ""
        hints = record.credential_hints or {}
        if provider is not None and provider.kind == "wordpress_app_password":
            return ConnectGuideOut(
                mode="wordpress",
                provider=provider.name,
                provider_label=label,
                sign_in_available=True,
                button_label="Continue to WordPress",
                access_summary=spec.oauth.access_summary,
                sign_in_hidden_fields=sorted(spec.oauth.token_fields),
            )
        available = oauth_providers.sign_in_available(spec)
        signed_in = from_platform_sign_in(hints)
        button = spec.oauth.button_label or f"Sign in with {label}"
        admin = _sign_in_admin_extras(spec.oauth.provider, label, ctx) if not available else {}
        if not available and not signed_in and spec.guide is not None:
            # A connector with both a sign-in and a key guide (Bing
            # Webmaster Tools, Mailchimp, Klaviyo): until the platform's app
            # is set up, the guided key steps are the default path.
            guide_out = _key_guide(spec.guide)
            for key, value in admin.items():
                if key != "developer_label":  # the key steps are the route here
                    setattr(guide_out, key, value)
            return guide_out
        # A connection made with the customer's own app credentials keeps
        # its form; everything else waits for the sign-in to be switched on.
        own_app = record.connected and not signed_in
        pending = not available and not own_app
        also = [
            ConnectLinkOut(slug=slug, name=name)
            for slug, name in ctx.not_connected.get(spec.oauth.provider, [])
            if slug != record.slug
        ]
        return ConnectGuideOut(
            sign_in_note=sign_in_note(record, label) if signed_in else "",
            manual_label=(
                "Advanced: connect with your own Google Cloud app"
                if spec.oauth.provider == "google"
                # Phase 2B: a vendor whose manual route is a guided key.
                else "Advanced: use an API key instead"
                if spec.guide is not None
                else f"Advanced: connect with your own {label} app or token"
            ),
            **_phase_2b_sign_in(spec, hints),
            mode="sign_in",
            provider=spec.oauth.provider,
            provider_label=label,
            sign_in_available=available,
            button_label=button,
            access_summary=spec.oauth.access_summary,
            choice_noun=spec.oauth.choice_noun,
            signed_in=signed_in,
            signed_in_account=hints.get(OAUTH_ACCOUNT_KEY, "") if signed_in else "",
            pending_choice=signed_in and not record.connected,
            reuse_account=ctx.accounts.get(spec.oauth.provider, "") if available else "",
            also_connect=also if available and record.connected and signed_in else [],
            sign_in_hidden_fields=sorted(
                spec.oauth.token_fields | spec.oauth.platform_fields | set(spec.oauth.choice_fields)
            ),
            setup_pending=pending,
            setup_message=setup_message(button, label) if pending else "",
            can_request_setup=pending and not ctx.show_admin_notes and not ctx.is_portal_admin,
            **(admin if pending else {}),
        )

    if spec.guide is not None and not spec.advanced:
        guide_out = _key_guide(spec.guide)
        # Phase 2B: a key the platform includes, for a one-click connection.
        if spec.platform_key is not None and spec.platform_key.available:
            guide_out.platform_key_available = True
            guide_out.platform_key_summary = spec.platform_key.summary
        return guide_out
    if spec.guide is not None:
        # Phase 2B: a developer integration with plain steps (Zapier, Adobe).
        guide_out = _key_guide(spec.guide)
        guide_out.mode = "advanced"
        return guide_out
    return ConnectGuideOut(mode="advanced" if spec.advanced else "form")


#: The connect-with-your-own-app-credentials route, offered only as a
#: secondary link while a sign-in is not switched on.
DEVELOPER_LABEL = "For developers: connect with your own app credentials"


def setup_message(button_label: str, provider_label: str) -> str:
    """What everyone sees on a sign-in that is not switched on yet."""
    what = button_label if button_label.startswith("Sign in with") else f"“{button_label}”"
    return (
        f"{what} isn't switched on for this workspace yet. Once your administrator "
        "switches it on, you'll connect in one click."
    )


def _sign_in_admin_extras(provider: str, label: str, ctx: ConnectContext) -> dict:
    """Admin-only extras while a provider's sign-in is not set up: portal
    admins get a link to set it up, workspace admins are told who can, and
    both may use their own app credentials instead."""
    if not ctx.show_admin_notes and not ctx.is_portal_admin:
        return {}
    extras: dict = {"developer_label": DEVELOPER_LABEL}
    if ctx.is_portal_admin:
        extras["admin_setup_url"] = f"/portal?tab=signin&provider={provider}"
        extras["admin_setup_label"] = f"Set up Sign in with {label}"
    else:
        extras["admin_hint"] = (
            f"Only admins see this: ask your platform administrator to set up Sign in with "
            f"{label}. After that, everyone here connects in one click."
        )
    return extras


def _sign_in_input_value(item, hints: dict[str, str]) -> str:  # noqa: ANN001
    """The starting value of a sign-in input, from what the connection holds."""
    current = str(hints.get(item.field, "")) if item.field else ""
    if item.kind == "text":
        return current
    if item.value_when_on:
        # A toggle that writes a value (Microsoft Ads: accountEnvironment).
        return "true" if current.strip().lower() == item.value_when_on.lower() else ""
    # Salesforce: the sandbox toggle wrote test.salesforce.com.
    return "true" if "test." in current else ""


def _phase_2b_sign_in(spec, hints: dict[str, str]) -> dict:  # noqa: ANN001
    """Phase 2B extras for the sign-in path: inputs asked before leaving,
    and the key guide offered under Advanced (Mailchimp, Klaviyo)."""
    from app.schemas.workspace import SignInInputOut

    extras: dict = {
        "sign_in_inputs": [
            SignInInputOut(
                key=item.key,
                label=item.label,
                kind=item.kind,
                placeholder=item.placeholder,
                help_text=item.help_text,
                value=_sign_in_input_value(item, hints),
            )
            for item in spec.oauth.sign_in_inputs
        ]
    }
    if spec.guide is not None:
        guide = spec.guide
        extras.update(
            steps=list(guide.steps),
            key_url=guide.key_url,
            key_url_label=guide.key_url_label,
            key_field=guide.key_field,
            key_pattern=guide.key_pattern,
            key_hint=guide.key_hint,
        )
    return extras


def _key_guide(guide, *, admin_note: str = ""):  # noqa: ANN001, ANN202
    from app.schemas.workspace import ConnectGuideOut

    return ConnectGuideOut(
        mode="key",
        steps=list(guide.steps),
        key_url=guide.key_url,
        key_url_label=guide.key_url_label,
        key_field=guide.key_field,
        key_pattern=guide.key_pattern,
        key_hint=guide.key_hint,
        admin_note=admin_note,
    )


#: Days before a non-renewing sign-in ends that the note turns into a warning.
SIGN_IN_WARNING_DAYS = 14


def sign_in_note(record: ConnectorRecord, label: str, *, now: datetime | None = None) -> str:
    """One sentence about a sign-in that cannot renew itself (Meta's
    long-lived token, LinkedIn without refresh), or "" when it renews."""
    from app.connectors.base.credentials import SIGN_IN_EXPIRY_KEY

    raw = (record.credential_hints or {}).get(SIGN_IN_EXPIRY_KEY, "")
    try:
        ends = datetime.fromtimestamp(float(raw), UTC) if raw else None
    except (ValueError, OverflowError, OSError):
        ends = None
    if ends is None:
        return ""
    current = now or datetime.now(UTC)
    day = f"{ends.day} {ends:%B %Y}"
    if ends <= current:
        return (
            f"Your {label} sign-in for {record.name} has expired, so it has stopped "
            "working. Choose Sign in again to reconnect it."
        )
    if ends - current <= timedelta(days=SIGN_IN_WARNING_DAYS):
        return (
            f"Your {label} sign-in ends on {day}. Choose Sign in again before then "
            f"to keep {record.name} working."
        )
    return (
        f"{label} sign-ins last about 60 days and do not renew themselves. This one "
        f"ends on {day}; you will be reminded here before then."
    )


def connector_out(
    record: ConnectorRecord, *, can_write: bool, ctx: ConnectContext | None = None
) -> ConnectorOut:
    spec = connector_registry.get_spec(record.slug)
    from app.connectors.base.llm_fields import without_legacy_autofill

    # An AI connection's old automatic fill-ins show as blank, so saving the
    # form again clears them (see LEGACY_AUTOFILLED).
    hints = without_legacy_autofill(record.slug, dict(record.credential_hints or {}))

    label = "Connected"
    if not record.connected:
        label = "Not connected"
    elif record.health == "failing":
        label = "Needs attention"
    elif record.health == "degraded":
        label = "Degraded"

    return ConnectorOut(
        slug=record.slug,
        name=record.name,
        # The code's declaration wins: a category changed in a release must
        # not stay stale on rows installed before it.
        category=spec.category if spec else record.category,
        description=spec.description if spec else "",
        connected=record.connected,
        # attention | connected | available. Same rule as the sort, so the
        # headings and the order agree by construction.
        group=CONNECTOR_GROUPS.get(_connector_rank(record), "available"),
        status_label=label,
        health=record.health,
        last_error=record.last_error,
        connected_at=record.connected_at,
        last_sync_at=record.last_sync_at,
        # Pre-rendered, because the card shows it and the console does not
        # format times itself. Empty when there is nothing to say, which is
        # what the card checks before showing the line at all.
        activity_label=(
            f"Last sync {since(record.last_sync_at)}"
            if record.last_sync_at
            else f"Connected {since(record.connected_at)}"
            if record.connected_at
            else ""
        ),
        hints=hints,
        fields=[
            CredentialFieldOut(**field.to_public()) for field in (spec.fields if spec else ())
        ],
        docs_url=spec.docs_url if spec else "",
        requirements=list(spec.requirements) if spec else [],
        read_only=not can_write,
        connect=connect_guide(record, ctx),
        verification=_verification_out(record.slug),
        **_connector_guide(record.slug),
    )


def _verification_out(slug: str):  # noqa: ANN202
    """The "Beta" label: shown until the connector type passes a live check."""
    from app.schemas.workspace import ConnectorVerificationOut
    from app.services import verification

    return ConnectorVerificationOut(**verification.label_for(slug))


def _connector_guide(slug: str) -> dict:
    """What an integration lets agents do, and which agents it serves."""
    from app.schemas.workspace import NeedOptionOut
    from app.services import agent_requirements as req

    return {
        "abilities": req.connector_abilities(slug),
        "used_by": [
            NeedOptionOut(slug=agent_slug, name=name, connected=False)
            for agent_slug, name in req.agents_using(slug)
        ],
    }


# ── SEO ────────────────────────────────────────────────────────────────────
def seo_page_out(page: SeoPage, *, can_write: bool) -> SeoPageOut:
    return SeoPageOut(
        id=page.id,
        url=page.url,
        title=page.title,
        gap_score=page.gap_score,
        aeo_pairs=page.aeo_pairs,
        schema_text=page.schema_text,
        status=page.status,
        status_label=PAGE_STATUS_LABELS.get(page.status, page.status.title()),
        missing_topics=list(page.missing_topics or []),
        # An approval can only be asked for on a flagged page, and only by
        # someone with write access to the module.
        show_approve=page.status == PageStatus.FLAGGED.value and can_write,
        last_synced_at=page.last_synced_at,
    )


def referral_spam_out(event: ReferralSpamEvent) -> ReferralSpamOut:
    return ReferralSpamOut(
        time=clock(event.at),
        referrer=event.referrer,
        category=event.category,
        action=event.action,
        sessions_affected=event.sessions_affected,
    )


# ── Off-page ───────────────────────────────────────────────────────────────
def backlink_out(target: BacklinkTarget, *, can_write: bool) -> BacklinkOut:
    from app.agents.digital_pr_outreach.prompts import mask_email

    # Decrypted by the column type; personal data, so only editors see it whole.
    email = (target.contact_email or "").strip()
    return BacklinkOut(
        id=target.id,
        domain=target.domain,
        authority=target.authority,
        placement_type=target.placement_type,
        status=target.status,
        status_label=target.status,
        relevance=round(target.relevance, 2),
        show_launch=target.status == "Discovered" and can_write,
        authority_measured=target.authority > 0,
        has_contact=bool(email),
        contact_email_masked=mask_email(email),
        contact_email=(email or None) if can_write else None,
    )


def alert_out(alert: CompetitorAlert) -> CompetitorAlertOut:
    return CompetitorAlertOut(
        id=alert.id,
        text=alert.text,
        competitor=alert.competitor,
        source_domain=alert.source_domain,
        authority=alert.authority,
        counter_launched=alert.counter_launched,
        button_label=(
            "Counter-pitch sent"
            if alert.counter_launched
            # Answered with a draft to the team: the publisher has no address yet.
            else "Draft sent to your team"
            if alert.counter_pitch_id
            else "Launch counter-pitch"
        ),
    )


# ── Ads ────────────────────────────────────────────────────────────────────
def budget_out(row: BudgetAllocation, *, connected_slugs: frozenset[str]) -> BudgetChannelOut:
    slug = CHANNEL_CONNECTOR.get(row.channel, "")
    return BudgetChannelOut(
        channel=row.channel,
        label=CHANNEL_LABELS.get(row.channel, row.channel.title()),
        percent=row.percent,
        cac=round(row.cac, 2),
        spend=round(row.spend, 2),
        conversions=row.conversions,
        last_shift=row.last_shift,
        locked=row.locked,
        connected=slug in connected_slugs,
    )


def creative_out(creative: AdCreative, cipher: OrgCipher) -> CreativeOut:
    return CreativeOut(
        id=creative.id,
        platform=creative.platform,
        dimensions=creative.dimensions,
        headline=cipher.decrypt(creative.headline_encrypted, context=Ctx.CREATIVE_HEADLINE),
        status=creative.status,
    )


def fraud_out(event: FraudEvent) -> FraudEventOut:
    return FraudEventOut(
        time=clock(event.at),
        source=event.source,
        reason=event.reason,
        action=event.action,
        channel=CHANNEL_LABELS.get(event.channel, event.channel),
        spend_saved=round(event.spend_saved, 2),
    )


def audience_out(cluster: AudienceCluster) -> AudienceOut:
    return AudienceOut(
        id=cluster.id,
        label=cluster.label,
        size=cluster.size,
        cohesion=round(cluster.cohesion, 2),
        top_signals=list(cluster.top_signals or []),
        activated_channels=list(cluster.activated_channels or []),
        is_live=cluster.is_live,
    )


# ── Approvals ──────────────────────────────────────────────────────────────
def approval_out(
    item: ApprovalItem,
    payload: dict | None = None,
    *,
    can_request_changes: bool = False,
    can_edit_text: bool = False,
    preview_file_id: str | None = None,
) -> ApprovalOut:
    """One approval card. ``payload`` (decrypted) adds why it waits, or
    what a practised change would have done. The ``can_*`` flags are decided
    by the caller, which knows both the item and the user's role."""
    from app.models.approval import ApprovalStatus
    from app.services.approvals import PRACTICE_KEY, humanize_age, why_waiting

    practice = item.status == ApprovalStatus.PRACTISED.value
    plan = (payload or {}).get(PRACTICE_KEY) if isinstance(payload, dict) else None
    plan = plan if isinstance(plan, dict) else {}
    return ApprovalOut(
        id=item.id,
        type=item.type,
        title=item.title,
        agent=item.agent_name,
        agent_slug=item.agent_slug,
        meta=item.meta or humanize_age(utcnow() - item.submitted_at),
        target_kind=item.target_kind,
        submitted_at=item.submitted_at,
        status=item.status,
        practice=practice,
        would=str(plan.get("would") or "") if practice else "",
        why=[str(n) for n in plan.get("notes") or []] if practice else why_waiting(payload),
        can_request_changes=can_request_changes,
        can_edit_text=can_edit_text,
        preview_file_id=preview_file_id,
    )


# ── Spending-agent safety ──────────────────────────────────────────────────
def _safety_fields(record: AgentRecord, env: GuideEnv | None) -> dict:
    """Practice mode and the verified-connector note, for a spending agent."""
    from app.agents.base import safety

    if not safety.is_spending_agent(record.slug):
        return {}
    own = safety.agent_practice_on(record.slug, record.settings)
    source = getattr(env, "practice", None) if env is not None else None
    if source is None:
        source = "agent" if own else ""
    elif source in ("", "agent"):
        # The workspace or platform switch is off: the agent's own decides.
        source = "agent" if own else ""
    if source == "platform":
        note = (
            "Practice mode is on for every workspace, set by the platform team: this "
            "agent plans changes but doesn't send them."
        )
    elif source == "workspace":
        note = (
            "Practice mode is on for all spending agents in this workspace: this agent "
            "plans changes but doesn't send them."
        )
    elif source == "agent":
        note = "This agent plans changes and records them, but doesn't send them."
    else:
        note = "This agent sends the changes it makes to your ad accounts."
    return {
        "spending": True,
        "practice_mode": own,
        "practice_source": source,
        "practice_note": note,
        "automatic_note": _automatic_note(record, env),
    }


def _automatic_note(record: AgentRecord, env: GuideEnv | None) -> str:
    """ "Automatic changes switch on once <connector> is verified." """
    from app.agents.base import safety
    from app.connectors.base import registry as connector_registry

    capability = safety.SPENDING_AGENTS.get(record.slug)
    verified = getattr(env, "verified", None) if env is not None else None
    if capability is None or verified is None:
        return ""
    names = []
    for slug in sorted(env.usable):
        spec = connector_registry.get_spec(slug)
        if spec is None or capability not in spec.capabilities:
            continue
        if not verified(slug):
            names.append(spec.name)
    if names:
        return safety.automatic_note(names)
    if not any(
        (spec := connector_registry.get_spec(slug)) is not None and capability in spec.capabilities
        for slug in env.usable
    ):
        return (
            "Automatic changes switch on once the ad platform it uses is connected and "
            "verified against a live account."
        )
    return ""


# ── Admin ──────────────────────────────────────────────────────────────────
def team_member_out(member: User, *, actor: User, can_write: bool) -> TeamMemberOut:
    role = Role(member.role)
    # The owner's role is fixed, and nobody can demote themselves — the same
    # rules the service layer enforces, surfaced so the select can disable.
    editable = can_write and not member.is_owner and member.id != actor.id
    return TeamMemberOut(
        id=member.id,
        name=member.name,
        email=member.email,
        role=role,
        role_label=ROLE_LABELS[role],
        is_owner=member.is_owner,
        is_active=member.is_active,
        email_verified=member.email_verified,
        last_login_at=member.last_login_at,
        editable=editable,
    )


def audit_out(entry: AuditLogEntry) -> AuditEntryOut:
    return AuditEntryOut(
        time=_stamp_safe(entry.at),
        actor=entry.actor,
        actor_type=entry.actor_type,
        action=entry.action,
        module=entry.module,
    )


# ── Technical SEO ──────────────────────────────────────────────────────────
#: Human wording for each check. Here rather than in the console, so a report
#: and a screen describe the same finding the same way.
ISSUE_LABELS: dict[str, str] = {
    "thin_content": "Thin content",
    "missing_schema": "Missing structured data",
    "broken_internal_link": "Broken internal link",
    "orphan_page": "Orphan page",
    "missing_meta_description": "Missing meta description",
    "missing_h1": "Missing H1",
    "multiple_h1": "Multiple H1s",
    "duplicate_title": "Duplicate title",
    "title_too_long": "Title too long",
    "image_missing_alt": "Image without alt text",
    "missing_canonical": "Missing canonical",
    "slow_lcp_mobile": "Slow LCP on mobile",
    "poor_cls_mobile": "Layout shift on mobile",
    "slow_inp_mobile": "Slow interaction on mobile",
}


def issue_out(issue: SeoIssue) -> SeoIssueOut:
    return SeoIssueOut(
        id=issue.id,
        url=issue.url,
        kind=issue.kind,
        kind_label=ISSUE_LABELS.get(issue.kind, issue.kind.replace("_", " ").capitalize()),
        severity=issue.severity,
        summary=issue.summary,
        recommendation=issue.recommendation,
        status=issue.status,
        age_label=(
            f"open {since(issue.first_seen_at)}"
            if issue.status == "open"
            else f"fixed {since(issue.resolved_at)}"
            if issue.status == "fixed"
            else f"ignored {since(issue.updated_at)}"
        ),
        ignore_note=issue.ignore_note,
    )


# ── Option lists ───────────────────────────────────────────────────────────
def cms_options() -> list[dict[str, str]]:
    """What onboarding offers as "which CMS", from the actual catalogue.

    It used to be a list of five names in this file, which meant adding the
    GitHub and Bitbucket connectors did not make them selectable — and a
    client whose content is in a repository was offered nothing that
    described them. Derived from the registry, so a new connector appears
    here the moment it exists.
    """
    from app.connectors.base import registry

    options = [
        {"slug": spec.slug, "name": spec.name}
        for spec in registry.all_specs()
        if spec.category == "CMS"
    ]
    # "Other" belongs at the end, with no slug: a client may be on something
    # with no connector yet, and the wizard must not imply otherwise.
    return [*options, {"slug": "", "name": "Other / not listed"}]


AGENT_SCHEDULES: list[str] = ["Hourly", "Every 6 hours", "Daily", "Weekly"]
AGENT_NOTIFY_CHANNELS: list[str] = ["Slack", "Email", "None"]


def guardrail_options() -> list[dict[str, str]]:
    """Each guardrail as a name, the full label, and what it means."""
    from app.agents.base.policy import GUARDRAIL_DETAIL, GUARDRAIL_NAMES

    return [
        {
            "value": key,
            "name": GUARDRAIL_NAMES.get(key, label),
            "label": label,
            "detail": GUARDRAIL_DETAIL.get(key, ""),
        }
        for key, label in GUARDRAIL_LABELS.items()
    ]


def ad_platform_options(db: Session, *, tenant_id: str) -> list[dict[str, object]]:
    """The ad platforms, and whether this workspace has connected each one.

    Replaces three hard-coded checkboxes — google, meta, linkedin — that
    wrote to a field nothing in the platform ever read. There are six ad
    connectors; the step showed three, and ticking one changed nothing.
    """
    from app.models.connector import ConnectorRecord

    connected = {
        record.slug
        for record in db.execute(
            select(ConnectorRecord).where(
                ConnectorRecord.tenant_id == tenant_id,
                ConnectorRecord.connected.is_(True),
            )
        ).scalars()
    }
    return [
        {
            "slug": spec.slug,
            "name": spec.name,
            "connected": spec.slug in connected,
        }
        for spec in connector_registry.all_specs()
        if spec.category == "Ad Platforms"
    ]


def role_options() -> list[dict[str, str]]:
    return [{"value": role.value, "label": label} for role, label in ROLE_LABELS.items()]


def module_values() -> list[str]:
    return [module.value for module in Module]


# ── Navigation badges ──────────────────────────────────────────────────────
def nav_counts(db: Session, *, tenant_id: str, role: str) -> dict[str, int]:
    """How much is waiting on each screen, keyed by route.

    Each number answers "how much needs me here", not "how many rows exist".
    A badge showing the size of a table is decoration; one showing what needs
    attention is the reason to open the screen.

    Filtered by the access map, so a role that cannot see a module is not
    told how much is in it — the same rule the notification panel follows.
    """
    from app.core.rbac import Module, access_map
    from app.models.agent import AgentRecord, AgentStatus
    from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert
    from app.models.seo import PageStatus, SeoIssue, SeoPage
    from app.models.workspace import Invitation
    from app.services import approvals as approval_service
    from app.services import connectors as connector_service

    allowed = {m for m, level in access_map(role).items() if level != "none"}

    def count(model, *where) -> int:  # noqa: ANN001
        return db.execute(
            select(func.count()).select_from(model).where(
                model.tenant_id == tenant_id, *where
            )
        ).scalar_one()

    out: dict[str, int] = {}

    if Module.AGENTS.value in allowed:
        # Non-idle fleet: running, errored, or configured-and-ready. Idle
        # catalogue rows stay off the badge so a fresh workspace stays clean,
        # but a workspace with agents set up still shows a number even when
        # none are running yet (same idea as Connectors showing connected).
        from app.services import portal_features

        allowed_slugs = portal_features.enabled_agent_slugs(db)
        out["/agents"] = db.execute(
            select(func.count())
            .select_from(AgentRecord)
            .where(
                AgentRecord.tenant_id == tenant_id,
                AgentRecord.slug.in_(allowed_slugs),
                or_(
                    AgentRecord.status == AgentStatus.RUNNING.value,
                    AgentRecord.status == AgentStatus.ERROR.value,
                    AgentRecord.configured.is_(True),
                ),
            )
        ).scalar_one()

    if Module.CONNECTORS.value in allowed:
        connected, _total = connector_service.counts(db, tenant_id=tenant_id)
        out["/connectors"] = connected

    if Module.APPROVALS.value in allowed:
        out["/approvals"] = approval_service.pending_count(db, tenant_id=tenant_id)

    if Module.SEO.value in allowed:
        # Open findings, which is what the Technical SEO screen is for. Not
        # the number of pages audited: that goes up as things improve.
        # "open" is a plain string column on SeoIssue — open | fixed |
        # ignored — with no enum behind it.
        out["/technical"] = count(SeoIssue, SeoIssue.status == "open")
        # Pages with a rewrite drafted or queued — work the operator can act
        # on, rather than every page ever synced.
        out["/seo"] = count(
            SeoPage,
            SeoPage.status.in_([PageStatus.FLAGGED.value, PageStatus.QUEUED.value]),
        )

    if Module.OFFPAGE.value in allowed:
        # Prospects worth acting on, plus competitor links not yet answered.
        out["/offpage"] = count(
            BacklinkTarget, BacklinkTarget.status == BacklinkStatus.DISCOVERED.value
        ) + count(CompetitorAlert, CompetitorAlert.counter_launched.is_(False))

    if Module.ADMIN.value in allowed:
        # The only thing on Admin that waits for someone. Uses the same
        # clauses the Admin screen lists by: counting "not accepted" alone
        # included a revoked invitation, so the badge said 1 and the screen
        # showed none.
        from app.services.auth import outstanding_invitation_clauses

        out["/admin"] = count(Invitation, *outstanding_invitation_clauses())

    # Zeroes are dropped so the console can render a badge for anything
    # present without checking the value.
    return {route: value for route, value in out.items() if value}


def qa_pairs_for(db: Session, *, page, cipher) -> list[QaPairOut]:  # noqa: ANN001
    """The Q&A pairs the AEO injector wrote for one page, decrypted.

    These rows existed and no screen read them — ``AeoQaPair`` appeared in no
    API module and no view builder. One workspace had 24 of them written and
    nowhere to see a single one, so the agent that produced them looked
    inert.
    """
    from app.models.seo import AeoQaPair

    rows = db.execute(
        select(AeoQaPair)
        .where(AeoQaPair.tenant_id == page.tenant_id, AeoQaPair.page_id == page.id)
        .order_by(AeoQaPair.injected.desc(), AeoQaPair.created_at.asc())
    ).scalars()
    return [
        QaPairOut(
            question=cipher.decrypt(row.question_encrypted, context=Ctx.QA_QUESTION),
            answer=cipher.decrypt(row.answer_encrypted, context=Ctx.QA_ANSWER),
            # AeoQaPair has no status column: injected is the lifecycle.
            status="Injected" if row.injected else "Awaiting injection",
            injected=row.injected,
        )
        for row in rows
    ]


def schema_patches_for(db: Session, *, page, cipher) -> list[SchemaPatchOut]:  # noqa: ANN001
    """The structured-data patches prepared for one page.

    The JSON-LD is included because a reviewer approving structured data on
    their own site has to be able to read it.
    """
    import json

    from app.models.seo import SchemaPatch

    rows = db.execute(
        select(SchemaPatch)
        .where(SchemaPatch.tenant_id == page.tenant_id, SchemaPatch.page_id == page.id)
        .order_by(SchemaPatch.created_at.desc())
    ).scalars()

    out: list[SchemaPatchOut] = []
    for row in rows:
        raw = (
            cipher.decrypt(row.json_ld_encrypted, context=Ctx.SCHEMA_JSONLD)
            if row.json_ld_encrypted
            else ""
        )
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            # Malformed stored JSON is worth showing as empty rather than
            # failing the whole page view.
            parsed = {}
        out.append(
            SchemaPatchOut(
                schema_type=row.schema_type,
                status=row.status,
                json_ld=parsed if isinstance(parsed, dict) else {},
            )
        )
    return out
