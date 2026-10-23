"""Response models for the console's screens.

These are view models: they carry the labels and flags the design renders, so
the frontend never re-derives a status label or an access rule.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictBool, StrictStr, StringConstraints

from app.core.rbac import Role
from app.schemas.common import ActionResult, ApiModel


# ── Agents ─────────────────────────────────────────────────────────────────
class NeedOptionOut(ApiModel):
    slug: str
    name: str
    connected: bool
    #: The workspace's plan does not include it for this agent; show it
    #: disabled with ``locked_reason`` (e.g. "Available on Pro").
    locked: bool = False
    locked_reason: str = ""


class NeedUseOut(ApiModel):
    """Which connector(s) the agent uses for one capability, and the control to change it.

    ``mode``: ``one`` (a single connector — or, for a multi ``one``, every
    selected website), ``all`` (every connected one, or the selected subset),
    ``page_source`` (pages from the selected websites, or all of them).
    ``control`` is ``single`` (a select), ``multi`` (checkboxes) or "" (the
    selection is made on another need). The answer goes back as
    ``AgentConfigRequest.connectors[capability]``. ``statement`` is the plain
    answer for the saved configuration, resolved by the rules the run uses.
    """

    capability: str
    mode: str
    question: str = ""
    primary: bool = True
    control: str = ""
    #: single: offer "Automatic" (the agent tries each connected one in turn).
    automatic_option: bool = False
    #: The saved selection.
    chosen: list[str] = Field(default_factory=list)
    #: What the control shows pre-filled: what the run uses now.
    selected: list[str] = Field(default_factory=list)
    #: Part of the saved selection is no longer connected.
    stale: bool = False
    using: list[NeedOptionOut] = Field(default_factory=list)
    options: list[NeedOptionOut] = Field(default_factory=list)
    statement: str = ""
    automatic_statement: str = ""
    #: The sentence for a selection, with ``{names}`` to fill in.
    template: str = ""
    note: str = ""


class AgentNeedOut(ApiModel):
    """One prerequisite for an agent, and what satisfies it.

    ``kind`` is ``integration`` (any one of ``options`` connected), or
    ``agent`` (another agent must have collected data first — ``options``
    then holds that agent). Derived from the agent's declared capabilities,
    so it is the same rule a run's preflight applies.
    """

    key: str
    kind: str = "integration"
    label: str
    satisfied: bool
    optional: bool = False
    #: For optional needs: what it adds when present.
    note: str = ""
    options: list[NeedOptionOut] = Field(default_factory=list)
    #: Which connector(s) the agent uses for this need (see NeedUseOut).
    uses: list[NeedUseOut] = Field(default_factory=list)


class AgentSettingOut(ApiModel):
    key: str
    label: str
    help: str = ""
    kind: str = "text"
    default: str = ""
    #: The saved value, or "" when the default applies.
    value: str = ""


class AgentOut(ApiModel):
    #: The agent record's id: its settings versions are kept under it
    #: (``GET /history/agent_config?subject=<id>``). Empty only for an
    #: unsaved record.
    id: str
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
    # ── Plain-language guide (for non-technical operators) ───────────────────
    #: What the agent does, in one or two sentences.
    summary: str = ""
    #: What it changes outside this console, or that it changes nothing.
    effect: str = ""
    #: False for agents that only report (they never publish anything).
    makes_changes: bool = True
    #: What happens to its changes under the current policy and switch.
    approval_behaviour: str = ""
    #: The same sentence for each position of the agent's own switch, so a
    #: setup screen can show the consequence of either choice before saving.
    approval_if_auto: str = ""
    approval_if_review: str = ""
    #: Workspace approval policy in force: full | hybrid | human.
    guardrail: str = ""
    #: What the scope box means for this agent, as a field label.
    scope_label: str = ""
    scope_help: str = ""
    #: False when the agent has no use for the scope box; setup hides it.
    uses_scope: bool = True
    #: Agent-specific settings with their saved values.
    settings_fields: list[AgentSettingOut] = Field(default_factory=list)
    #: Prerequisites, each marked satisfied or not.
    needs: list[AgentNeedOut] = Field(default_factory=list)
    #: Every required need is satisfied, so a run will not be skipped for setup.
    ready: bool = False
    # ── Spending-agent safety (app.agents.base.safety) ─────────────────────
    #: True for the agents that can spend money or change an ad account.
    spending: bool = False
    #: The agent's own practice switch.
    practice_mode: bool = False
    #: Practice mode in force for this agent, from wherever it comes:
    #: "platform", "workspace", "agent" or "" when off.
    practice_source: str = ""
    #: One sentence about practice mode as it applies now.
    practice_note: str = ""
    #: "Automatic changes switch on once Google Ads is verified." — empty
    #: when every connector it writes through is verified.
    automatic_note: str = ""
    #: "Always use the model I picked": the run is skipped rather than
    #: written with another connected model (``runner.STRICT_MODEL_SETTING``).
    strict_model: bool = False


class AgentConfigRequest(ApiModel):
    schedule: str = Field(default="Daily")
    scope: str = Field(default="", max_length=255)
    notify_channel: str = Field(default="None")
    # Connector slug (openai, anthropic_claude, …). Required when the agent
    # needs a model; ignored (and stored empty) when it does not.
    llm_connector: str = Field(default="", max_length=64)
    max_actions_per_day: int = Field(default=20, ge=1, le=1000)
    #: Agent-specific settings, keyed as declared in agents/base/plain.py.
    #: Keys not declared for the agent are refused.
    settings: dict[str, str] = Field(default_factory=dict)
    #: Which connector to use where the agent uses one of several:
    #: ``{capability: [connector slug, …]}``, keyed by ``NeedUseOut.capability``
    #: (a single string is accepted as a one-item list). An empty list means
    #: automatic / every connected one. Omitted (null) keeps the saved
    #: selections; an object replaces them, so a capability left out reverts.
    connectors: dict[str, list[str] | str] | None = None
    #: Spending agents only: "Practice mode — plan changes but don't send
    #: them". Omitted keeps the saved value (on, for a new configuration).
    practice_mode: bool | None = None
    #: Agents that write with a model only: "Always use the model I picked
    #: (don't switch automatically)". Omitted keeps the saved value (off).
    strict_model: StrictBool | None = None


class RunStepOut(ApiModel):
    """One entry on a run's timeline, in the agent's own words."""

    label: str
    text: str
    done: int = 0
    total: int = 0
    #: Whole percent where the step has a countable total; otherwise None.
    percent: int | None = None
    at: datetime | None = None
    ended_at: datetime | None = None


class RunEventOut(ApiModel):
    """Something a run did that is recorded in the audit log."""

    at: datetime
    action: str
    #: True when the change was sent for approval rather than applied.
    awaiting_approval: bool = False
    #: True when it was practised (planned and recorded, not sent).
    practice: bool = False
    #: Why it waits: an unverified connector or a workspace limit.
    held: list[str] = Field(default_factory=list)


class AgentRunOut(ApiModel):
    id: str
    agent_slug: str
    agent_name: str = ""
    trigger: str
    triggered_by: str = ""
    status: str
    #: running | interrupted | succeeded | skipped | failed. ``interrupted``
    #: is a run still marked running after the scheduler lease expired — the
    #: process that owned it stopped, so it will never report again.
    state: str = ""
    state_label: str = ""
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int
    #: "2m 14s" — elapsed so far for a running pass, total for a finished one.
    duration_label: str = ""
    actions_taken: int
    actions_queued: int
    summary: str
    error: str = ""
    cost: float = 0.0
    # Where an open pass has got to.
    step: str = ""
    step_percent: int | None = None
    #: When the pass last reported progress.
    last_update_at: datetime | None = None
    timeline: list[RunStepOut] = Field(default_factory=list)
    #: Audit entries this run produced. Only on the single-run endpoint.
    events: list[RunEventOut] = Field(default_factory=list)
    #: Practice mode: changes the run planned and recorded instead of sending.
    actions_practised: int = 0
    #: Where practice mode came from for this run ("" when it was off).
    practice: str = ""


class ResultColumnOut(ApiModel):
    key: str
    label: str
    #: text | status | number | muted
    kind: str = "text"


class ResultRowOut(ApiModel):
    #: Column key -> text. ``<key>_sub`` holds an optional second line.
    cells: dict[str, str] = Field(default_factory=dict)
    tones: dict[str, str] = Field(default_factory=dict)


class ResultMetricOut(ApiModel):
    label: str
    value: str


class AgentResultsOut(ApiModel):
    """What one agent has produced, read from the tables it writes."""

    title: str
    description: str = ""
    metrics: list[ResultMetricOut] = Field(default_factory=list)
    columns: list[ResultColumnOut] = Field(default_factory=list)
    rows: list[ResultRowOut] = Field(default_factory=list)
    #: All rows, of which ``rows`` is the newest slice.
    total: int = 0
    empty: str = ""
    #: The module screen with the full working view.
    link: str = ""
    link_label: str = ""


class AgentActivityOut(ApiModel):
    """What the fleet is doing now, and what it did recently."""

    running: list[AgentRunOut] = Field(default_factory=list)
    recent: list[AgentRunOut] = Field(default_factory=list)


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
    #: The plan does not include it for agents (app.services.entitlements).
    locked: bool = False
    locked_reason: str = ""


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
    default: str = ""
    #: For ``type == "model"``: ``text`` or ``image``.
    model_kind: str = ""


class ConnectLinkOut(ApiModel):
    slug: str
    name: str


class ConnectGuideOut(ApiModel):
    """How the connect screen should present this integration.

    ``mode``: ``sign_in`` (a vendor sign-in button, then a picker),
    ``wordpress`` (type the site address, approve in WordPress), ``key``
    (guided steps to create and paste a key), ``form`` (a few plain fields),
    or ``advanced`` (a developer integration).
    """

    mode: str = "form"
    provider: str = ""
    provider_label: str = ""
    #: Sign-in can be offered: the platform's app credentials are set.
    sign_in_available: bool = False
    button_label: str = ""
    access_summary: str = ""
    choice_noun: str = ""
    #: Connected by signing in — the address that signed in, when known.
    signed_in: bool = False
    signed_in_account: str = ""
    #: Signed in, and still to choose what to connect.
    pending_choice: bool = False
    #: Another connection in this workspace already signed in to this
    #: provider, offered as "Continue as …".
    reuse_account: str = ""
    #: Other connectors this sign-in could also connect.
    also_connect: list[ConnectLinkOut] = Field(default_factory=list)
    #: Only for workspace admins and portal admins.
    admin_note: str = ""
    #: Fields the sign-in path does not show (tokens, platform-supplied
    #: values, and what the picker chooses).
    sign_in_hidden_fields: list[str] = Field(default_factory=list)
    #: When a sign-in that cannot renew itself ends (Meta, LinkedIn), in
    #: one sentence; "Sign in again" once it has.
    sign_in_note: str = ""
    #: The link to the paste-it-yourself route on the sign-in path.
    manual_label: str = ""
    # Key guide.
    steps: list[str] = Field(default_factory=list)
    key_url: str = ""
    key_url_label: str = ""
    key_field: str = ""
    key_pattern: str = ""
    key_hint: str = ""
    # ── Phase 2B ──────────────────────────────────────────────────────────
    #: Asked before the sign-in button is pressed (Shopify store address,
    #: Salesforce sandbox switch).
    sign_in_inputs: list[SignInInputOut] = Field(default_factory=list)
    #: "Use the included access": the platform supplies the key.
    platform_key_available: bool = False
    platform_key_summary: str = ""
    # ── Sign-in not switched on yet ───────────────────────────────────────
    #: The vendor's sign-in is the way to connect, but the platform's app
    #: is not set up: show the button switched off, with ``setup_message``.
    setup_pending: bool = False
    setup_message: str = ""
    #: Anyone may tell this workspace's admins (``POST …/request-sign-in``).
    can_request_setup: bool = False
    #: Portal admins only: where to switch the sign-in on.
    admin_setup_url: str = ""
    admin_setup_label: str = ""
    #: Workspace admins who are not portal admins: who can switch it on.
    admin_hint: str = ""
    #: The connect-with-your-own-app-credentials route, as a secondary link.
    #: Offered to admins, and wherever such a connection already exists.
    developer_label: str = ""


class SignInInputOut(ApiModel):
    key: str
    label: str
    #: text | toggle
    kind: str = "text"
    placeholder: str = ""
    help_text: str = ""
    #: The value used last time, when there is one (a store address).
    value: str = ""


ConnectGuideOut.model_rebuild()


class OAuthStartRequest(ApiModel):
    #: The console path to come back to (validated server-side).
    return_path: str = "/connectors"
    #: WordPress only: the site address.
    site_url: str = Field(default="", max_length=500)
    #: Reuse this workspace's existing sign-in to the same provider.
    reuse: bool = False
    #: Answers to the connector's sign-in inputs (Shopify store address,
    #: Salesforce sandbox switch). Checked server-side.
    inputs: dict[str, str] = Field(default_factory=dict, max_length=10)


class OAuthStartOut(ApiModel):
    #: Send the browser here. Empty when no redirect was needed.
    authorize_url: str = ""
    #: redirect | connected | choose
    status: str = "redirect"


class OAuthChoiceOut(ApiModel):
    id: str
    label: str
    detail: str = ""


class OAuthChooseRequest(ApiModel):
    choice_id: str = Field(min_length=1, max_length=300)


class ConnectorVerificationOut(ApiModel):
    """Whether this connector type has been confirmed against a live account.

    ``label`` is "Beta" when it has not (shown as a small pill, with
    ``tooltip``), and empty when it has: a verified connector shows nothing extra.
    """

    verified: bool = False
    label: str = "Beta"
    tooltip: str = ""


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
    #: What this integration lets agents do, in plain words.
    abilities: list[str] = Field(default_factory=list)
    #: Agents this integration can serve: [{slug, name}].
    used_by: list[NeedOptionOut] = Field(default_factory=list)
    #: How to connect it, for the connect screen.
    connect: ConnectGuideOut = Field(default_factory=ConnectGuideOut)
    #: Live verification: {verified, label}. See app.services.verification.
    verification: ConnectorVerificationOut = Field(default_factory=ConnectorVerificationOut)


class ConnectRequest(ApiModel):
    values: dict[str, str] = Field(default_factory=dict)
    oauth_completed: bool = False


class ModelOptionOut(ApiModel):
    """One model from the vendor's own list."""

    id: str
    label: str
    #: Unix seconds from the vendor, when it publishes one.
    created: int | None = None
    #: ``text``, ``image`` or both (both when the vendor does not say).
    kinds: list[str] = Field(default_factory=list)


class ModelListOut(ApiModel):
    listable: bool = True
    models: list[ModelOptionOut] = Field(default_factory=list)
    suggested: str = ""
    source: str = ""
    error: str = ""
    stale: str = ""
    models_url: str = ""


#: A connector credential field name, e.g. ``apiKey`` or ``baseUrl``.
ModelListFieldName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")]


class ModelListRequest(ApiModel):
    """What the connect dialog holds so far. Blank secrets mean "the saved one".

    Bounded: each typed address can lead to an (egress-guarded) outbound call.
    """

    model_config = ConfigDict(extra="forbid")

    values: dict[ModelListFieldName, Annotated[StrictStr, Field(max_length=2048)]] = Field(
        default_factory=dict, max_length=20
    )


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
    #: True when the Technical Fix Applier can propose a gated CMS/git write.
    fixable: bool = False
    #: True when a fix for this finding is already waiting in Approvals.
    pending_fix: bool = False


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
    #: False when ``authority`` is 0, which means no backlink index measured
    #: it — show "not measured", never "0".
    authority_measured: bool = False
    #: Whether a person has entered the publisher's contact address. Without
    #: one, an approved pitch goes to the team as a draft, not to the publisher.
    has_contact: bool = False
    #: ``e***@example.com`` — always safe to show.
    contact_email_masked: str = ""
    #: The full address, only for users who can edit Off-Page (so they can
    #: correct it). ``None`` for read-only users or when none is set.
    contact_email: str | None = None


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
    #: pending | practised (and the decided states, where they are listed).
    status: str = "pending"
    #: Practised in practice mode: recorded with its plan, never sent.
    practice: bool = False
    #: Practice: "apply" (would have gone ahead automatically) or "queue"
    #: (would have waited for approval).
    would: str = ""
    #: Why it waits for a person: an unverified connector or a workspace
    #: limit, in plain words. Empty for the ordinary approval policy.
    why: list[str] = Field(default_factory=list)
    #: "Ask for changes" is available to this user for this item.
    can_request_changes: bool = False
    #: "Edit text" is available to this user for this item.
    can_edit_text: bool = False
    #: The rendered preview (served by ``/files/{id}``), when there is one.
    preview_file_id: str | None = None


class ApprovalDetailOut(ApprovalOut):
    """The full pending change, for a reviewer who wants to see it."""

    payload: dict = Field(default_factory=dict)


class DecisionRequest(ApiModel):
    note: str = Field(default="", max_length=1000)


class RequestChangesRequest(ApiModel):
    """What the reviewer wants changed. Untrusted text: it is stored
    encrypted and delimited as reference material before any model sees it."""

    model_config = ConfigDict(extra="forbid")

    feedback: str = Field(min_length=1, max_length=2000)


#: A field name an editor may accept, e.g. ``headline``.
EditFieldName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,39}$")]


class EditTextRequest(ApiModel):
    """Corrected text, by field. Which fields are allowed depends on the item."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[EditFieldName, Annotated[str, Field(max_length=2000)]] = Field(
        min_length=1, max_length=8
    )


class RevisionOut(ActionResult):
    """The result of "Ask for changes" or "Edit text"."""

    #: The pending item now holding the work; ``None`` while a revised
    #: version is still being made.
    approval_id: str | None = None
    #: Revisions used so far for this piece of work.
    revision: int = 0
    #: The most revisions allowed per piece of work.
    max_revisions: int = 0
    #: The background job making the revised version, when one was started
    #: (a creative); ``None`` otherwise.
    job_id: str | None = None


#: The states the approvals history can be narrowed to.
HistoryStatus = Literal["approved", "rejected", "superseded", "expired"]


class ApprovalHistoryItemOut(ApiModel):
    """One item that has left the queue: what it was, who decided, and when."""

    id: str
    type: str
    title: str
    agent: str
    agent_slug: str
    target_kind: str
    #: approved | rejected | superseded | expired
    status: str
    #: The status in plain words ("Replaced by a revised version").
    status_label: str
    submitted_at: datetime
    decided_at: datetime | None = None
    #: The person who decided ("System" for an expiry).
    decided_by: str = ""
    #: The reviewer's note, or why it closed.
    note: str = ""
    #: Revisions used for this piece of work when this version was made.
    revision: int = 0
    #: The earlier version this one replaced, when it is a revision.
    revises_id: str | None = None
    #: The revised version that replaced this one, once it exists.
    replaced_by_id: str | None = None
    replaced_by_status: str | None = None


class ApprovalHistoryOut(ApiModel):
    items: list[ApprovalHistoryItemOut]
    #: Pass back as ``cursor`` for the next page; ``None`` on the last page.
    next_cursor: str | None = None


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
    #: Required to choose "Publish automatically" ("full"), as on
    #: ``PUT /onboarding/policy``.
    confirm: StrictBool = False


#: The stored "Review before publishing" values (``policy.Guardrail``).
GuardrailValue = Literal["human", "hybrid", "full"]


class WorkspacePolicyRequest(ApiModel):
    """The "Review before publishing" choice and the agents' switch, saved together.

    One request so the two can never disagree after a half-finished save.
    """

    model_config = ConfigDict(extra="forbid")

    guardrail: GuardrailValue
    #: Agents act on their own (``Organization.global_autonomy``); a change
    #: is applied to every agent's own switch too.
    autonomous: StrictBool
    #: Required to switch to "Publish automatically" ("full").
    confirm: StrictBool = False


class WorkspacePolicyOut(ActionResult):
    """The workspace policy in force after a save."""

    guardrail: str
    guardrail_label: str
    autonomous: bool
    #: How many agents' own switches were changed with it.
    agents_updated: int = 0
    #: The version this save recorded; ``None`` when nothing changed or the
    #: history could not be written (the save itself still stands).
    version: int | None = None


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
