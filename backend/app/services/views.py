"""View-model builders.

Every label the console renders — a status word, a countdown, a relative
timestamp — is produced here rather than in the frontend, so the API is the
single place those rules live and two screens showing the same object cannot
disagree about it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
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
    SeoAuditOut,
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
def agent_out(
    record: AgentRecord, *, can_write: bool, run: AgentRun | None = None
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
        if requires_llm and record.llm_connector:
            from app.connectors.base import registry as connector_registry

            spec = connector_registry.get_spec(record.llm_connector)
            label = spec.name if spec else record.llm_connector
            parts.insert(1, f"model {label}")
        config_summary = " · ".join(parts)

    return AgentOut(
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
    )


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
    from app.models.agent import RunStatus

    if run is None:
        return {}

    busy = run.status == RunStatus.RUNNING.value
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
        "last_run_summary": "" if busy else (run.summary or ""),
        "last_run_status": "" if busy else run.status,
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

    return [
        agent_out(record, can_write=can_write, run=newest.get(record.slug))
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


def connector_out(record: ConnectorRecord, *, can_write: bool) -> ConnectorOut:
    spec = connector_registry.get_spec(record.slug)
    hints = dict(record.credential_hints or {})

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
        category=record.category,
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
    )


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
    return BacklinkOut(
        id=target.id,
        domain=target.domain,
        authority=target.authority,
        placement_type=target.placement_type,
        status=target.status,
        status_label=target.status,
        relevance=round(target.relevance, 2),
        show_launch=target.status == "Discovered" and can_write,
    )


def alert_out(alert: CompetitorAlert) -> CompetitorAlertOut:
    return CompetitorAlertOut(
        id=alert.id,
        text=alert.text,
        competitor=alert.competitor,
        source_domain=alert.source_domain,
        authority=alert.authority,
        counter_launched=alert.counter_launched,
        button_label="Counter-pitch launched" if alert.counter_launched else "Launch counter-pitch",
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
def approval_out(item: ApprovalItem) -> ApprovalOut:
    from app.services.approvals import humanize_age

    return ApprovalOut(
        id=item.id,
        type=item.type,
        title=item.title,
        agent=item.agent_name,
        agent_slug=item.agent_slug,
        meta=item.meta or humanize_age(utcnow() - item.submitted_at),
        target_kind=item.target_kind,
        submitted_at=item.submitted_at,
    )


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
        out["/agents"] = count(
            AgentRecord, AgentRecord.status == AgentStatus.RUNNING.value
        )

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
