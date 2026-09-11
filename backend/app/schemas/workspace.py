"""Response models for the console's screens.

These are view models: they carry the labels and flags the design renders, so
the frontend never re-derives a status label or an access rule.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from app.core.rbac import Role
from app.schemas.common import ApiModel


# ── Agents ─────────────────────────────────────────────────────────────────
class AgentOut(ApiModel):
    slug: str
    name: str
    category: str
    description: str
    status: str
    status_label: str
    autonomy: bool
    autonomy_label: str
    metric_label: str
    next_run: str
    schedule: str
    scope: str
    # What this agent's scope field means, in its own words. Scope is one text
    # box that means something different per agent — a path glob, a list of
    # domains, a set of schema types — so a single generic hint is wrong for
    # eleven of the twelve.
    scope_placeholder: str = ""
    notify_channel: str
    # Connected AI model slug when this agent writes with a model; empty otherwise.
    llm_connector: str = ""
    # True when Configure must ask which model connector to use.
    requires_llm: bool = False
    max_actions_per_day: int
    configured: bool
    config_summary: str = ""
    #: Which section this card belongs in — attention | running | ready | idle.
    #: Derived from the same rule that sorts the list, so the order and the
    #: headings cannot disagree.
    group: str = "idle"
    # ── What is happening right now ────────────────────────────────────────
    # A pass is started and not awaited, so "running" on its own could mean
    # working or wedged. These describe the pass currently open, if any.
    busy: bool = False
    #: "Analysing /pricing (3 of 6)" — the step, in the agent's own words.
    step: str = ""
    step_done: int = 0
    step_total: int = 0
    #: Whole percent, or None where the step has no countable total.
    step_percent: int | None = None
    #: How long the open pass has been going, e.g. "running for 40s".
    busy_for: str = ""
    #: The previous pass's outcome, so a card can show what it last achieved
    #: rather than only what it is doing.
    last_run_summary: str = ""
    last_run_status: str = ""
    # "Saved, but…" — a configuration that is valid and will not do everything
    # the operator probably expects. Returned by the config endpoint only.
    warnings: list[str] = []
    last_error: str = ""
    actions_today: int = 0
    # True when the caller's role cannot change this agent.
    read_only: bool = False


class AgentConfigRequest(ApiModel):
    schedule: str = Field(default="Daily")
    scope: str = Field(default="", max_length=255)
    notify_channel: str = Field(default="None")
    # Connector slug (openai, anthropic_claude, …). Required when the agent
    # needs a model; ignored (and stored empty) when it does not.
    llm_connector: str = Field(default="", max_length=64)
    max_actions_per_day: int = Field(default=20, ge=1, le=1000)


class AgentRunOut(ApiModel):
    id: str
    agent_slug: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int
    actions_taken: int
    actions_queued: int
    summary: str
    error: str = ""
    cost: float = 0.0


class NotifyChannelOut(ApiModel):
    """One notification channel, and whether it can actually deliver."""

    value: str
    #: False when nothing is connected that can carry it. The dialog renders
    #: these unselectable rather than letting somebody choose a channel that
    #: would silently drop every message.
    available: bool = True
    #: What to connect, in the operator's terms. Empty when available.
    reason: str = ""


class LlmConnectorOut(ApiModel):
    """One AI model connector the Configure dialog can offer."""

    slug: str
    name: str
    #: False when this workspace has not connected it yet.
    available: bool = True
    reason: str = ""


class AgentOptionsOut(ApiModel):
    """Choices the Configure dialog offers."""

    schedules: list[str]
    notify_channels: list[NotifyChannelOut]
    llm_connectors: list[LlmConnectorOut] = Field(default_factory=list)


# ── Connectors ─────────────────────────────────────────────────────────────
class CredentialFieldOut(ApiModel):
    key: str
    label: str
    type: str
    placeholder: str
    required: bool
    help_text: str = ""
    is_oauth: bool = False
    oauth_label: str = ""


class ConnectorOut(ApiModel):
    slug: str
    name: str
    category: str
    description: str = ""
    connected: bool
    #: Which section this card belongs in — attention | connected | available.
    group: str = "available"
    status_label: str
    health: str
    last_error: str = ""
    connected_at: datetime | None = None
    last_sync_at: datetime | None = None
    # "Last sync 12m ago" — rendered here so both halves agree on the wording.
    activity_label: str = ""
    # Non-secret values already stored, so the dialog can show what is wired up.
    hints: dict[str, str] = Field(default_factory=dict)
    fields: list[CredentialFieldOut] = Field(default_factory=list)
    #: Plan tiers, versions and scopes this platform needs. Shown before the
    #: operator tries, because a plan limitation is not fixable by retyping.
    requirements: list[str] = Field(default_factory=list)
    docs_url: str = ""
    read_only: bool = False


class ConnectRequest(ApiModel):
    values: dict[str, str] = Field(default_factory=dict)
    oauth_completed: bool = False


# ── SEO ────────────────────────────────────────────────────────────────────
class SeoPageOut(ApiModel):
    id: str
    url: str
    title: str
    gap_score: int
    aeo_pairs: int
    schema_text: str
    status: str
    status_label: str
    missing_topics: list[str] = Field(default_factory=list)
    show_approve: bool = False
    last_synced_at: datetime | None = None


class QaPairOut(ApiModel):
    """One question and answer the AEO injector wrote for a page."""

    question: str
    answer: str
    status: str
    #: True once the pair has been written into the page.
    injected: bool = False


class SchemaPatchOut(ApiModel):
    """One structured-data patch the knowledge-graph agent prepared."""

    schema_type: str
    status: str
    #: The JSON-LD itself, so it can be read before it is published.
    json_ld: dict = Field(default_factory=dict)


class SeoPageDetailOut(SeoPageOut):
    """Adds the decrypted content, for the page a reviewer opens."""

    body: str = ""
    proposed_body: str = ""
    rewrite_rationale: str = ""
    target_keywords: list[str] = Field(default_factory=list)
    # The work two other agents did on this same page. Both were written to
    # the database and shown nowhere, so those agents looked inert.
    qa_pairs: list[QaPairOut] = Field(default_factory=list)
    schema_patches: list[SchemaPatchOut] = Field(default_factory=list)


class ReferralSpamOut(ApiModel):
    time: str
    referrer: str
    category: str
    action: str
    sessions_affected: int = 0


class SeoWorkspaceOut(ApiModel):
    pages: list[SeoPageOut]
    total: int
    referral_spam: list[ReferralSpamOut]
    read_only: bool = False


class SeoIssueOut(ApiModel):
    """One technical finding, with what to do about it."""

    id: str
    url: str
    kind: str
    #: Human wording for the kind, so the console does not keep its own copy.
    kind_label: str
    severity: str
    summary: str
    recommendation: str
    status: str
    #: "open for 6 days" — rendered here, like every other relative time.
    age_label: str
    ignore_note: str = ""


class SeoAuditOut(ApiModel):
    """The Technical SEO screen.

    ``counts`` is what the tiles show; ``by_kind`` is what the grouped list
    shows. Both come from the same rows, so they cannot disagree.
    """

    issues: list[SeoIssueOut]
    open_count: int
    high_count: int
    fixed_this_month: int
    pages_audited: int
    by_kind: list[dict]
    #: Empty when a page-speed source is connected. Set to an explanation
    #: otherwise, because "no speed issues" and "speed was never measured"
    #: must not look the same on screen.
    vitals_unavailable: str = ""
    last_audit_at: datetime | None = None
    read_only: bool = False


class IgnoreIssueRequest(ApiModel):
    #: Required. "Ignored" without a reason becomes "nobody remembers why".
    note: str = Field(min_length=3, max_length=500)


# ── Off-page ───────────────────────────────────────────────────────────────
class BacklinkOut(ApiModel):
    id: str
    domain: str
    authority: int
    placement_type: str
    status: str
    status_label: str
    relevance: float
    show_launch: bool = False


class CompetitorAlertOut(ApiModel):
    id: str
    text: str
    competitor: str
    source_domain: str
    authority: int
    counter_launched: bool
    button_label: str


class OffPageWorkspaceOut(ApiModel):
    backlinks: list[BacklinkOut]
    alerts: list[CompetitorAlertOut]
    read_only: bool = False


# ── Ads ────────────────────────────────────────────────────────────────────
class BudgetChannelOut(ApiModel):
    channel: str
    label: str
    percent: int
    cac: float
    spend: float
    conversions: int
    last_shift: int
    locked: bool
    connected: bool


class CreativeOut(ApiModel):
    id: str
    platform: str
    dimensions: str
    headline: str = ""
    status: str


class FraudEventOut(ApiModel):
    time: str
    source: str
    reason: str
    action: str
    channel: str = ""
    spend_saved: float = 0.0


class AudienceOut(ApiModel):
    id: str
    label: str
    size: int
    cohesion: float
    top_signals: list[str] = Field(default_factory=list)
    activated_channels: list[str] = Field(default_factory=list)
    is_live: bool


class AdsWorkspaceOut(ApiModel):
    budgets: list[BudgetChannelOut]
    budget_total: int
    creatives: list[CreativeOut]
    audiences: list[AudienceOut]
    fraud_log: list[FraudEventOut]
    blended_cac: float
    read_only: bool = False


class BudgetUpdateRequest(ApiModel):
    channel: str
    percent: int = Field(ge=0, le=100)


# ── Approvals ──────────────────────────────────────────────────────────────
class ApprovalOut(ApiModel):
    id: str
    type: str
    title: str
    agent: str
    agent_slug: str
    meta: str
    target_kind: str
    submitted_at: datetime


class ApprovalDetailOut(ApprovalOut):
    """The full pending change, for a reviewer who wants to see it."""

    payload: dict = Field(default_factory=dict)


class DecisionRequest(ApiModel):
    note: str = Field(default="", max_length=1000)


# ── Onboarding ─────────────────────────────────────────────────────────────
class OnboardingOut(ApiModel):
    step: int
    domain: str
    cms: str
    guardrail: str
    guardrail_label: str
    completed: bool
    cms_options: list[dict[str, str]]
    #: The ad platforms and whether each is connected, so the step can show
    #: what is true rather than offer checkboxes nothing reads.
    ad_platforms: list[dict[str, object]] = []
    guardrail_options: list[dict[str, str]]


class OnboardingUpdateRequest(ApiModel):
    step: int | None = Field(default=None, ge=0, le=3)
    domain: str | None = Field(default=None, max_length=255)
    cms: str | None = None
    guardrail: str | None = None


# ── Dashboard & reports ────────────────────────────────────────────────────
class KpiOut(ApiModel):
    organic_sessions: str
    organic_sessions_raw: int
    agents_running: int
    agents_total: int
    connectors_connected: int
    connectors_total: int
    pending_approvals: int


class DashboardOut(ApiModel):
    kpis: KpiOut
    # The agents actually doing something, not the head of the catalogue.
    agents: list[AgentOut]
    # How many there are in total, so the panel can say "showing 6 of 9"
    # rather than implying six is all of them.
    active_agents: int = 0
    # Configured and stopped. Sent because a dashboard that only knows about
    # running agents cannot tell "nothing is set up" from "everything is set
    # up and paused" — and it was giving the first answer to workspaces in
    # the second state, along with their results hidden.
    ready: list[AgentOut] = []
    ready_agents: int = 0
    # The connected integrations. Empty for a role that cannot see them.
    connectors: list[ConnectorOut] = []
    approvals: list[ApprovalOut]
    global_autonomy: bool


class ReportsOut(ApiModel):
    range: str
    organic_sessions: str
    aeo_citations: int
    backlinks_won: int
    blended_cac: float
    ad_spend: str
    fraud_blocked: int
    referral_spam_blocked: int
    pages_optimised: int
    trend: list[int]


# ── Admin ──────────────────────────────────────────────────────────────────
class TeamMemberOut(ApiModel):
    id: str
    name: str
    email: str
    role: Role
    role_label: str
    is_owner: bool
    is_active: bool
    email_verified: bool
    last_login_at: datetime | None = None
    editable: bool = False


class AuditEntryOut(ApiModel):
    time: str
    actor: str
    actor_type: str
    action: str
    module: str = ""


class NotificationsOut(ApiModel):
    """What the header's bell opens: recent workspace activity.

    Filtered to the modules the caller can see, so the panel cannot become a
    way around the access map.
    """

    entries: list[AuditEntryOut]
    # Activity this person has not looked at yet, excluding their own actions.
    # What the bell's dot is for.
    unseen: int = 0
    pending_approvals: int = 0


class BillingOut(ApiModel):
    plan_name: str
    seats_total: int
    seats_used: int
    seats_percent: int
    renews_on: date | None = None


class AdminOut(ApiModel):
    team: list[TeamMemberOut]
    invitations: list[dict]
    audit: list[AuditEntryOut]
    billing: BillingOut
    role_options: list[dict[str, str]]
    read_only: bool = False


class RoleChangeRequest(ApiModel):
    role: Role


class AutonomyRequest(ApiModel):
    autonomous: bool
