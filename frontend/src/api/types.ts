/**
 * Types mirroring the backend's response models.
 *
 * Kept hand-written rather than generated so the console has one readable
 * contract to program against; the field names match the API exactly.
 */

// ── Roles and access ───────────────────────────────────────────────────────
export type Role = 'admin' | 'manager' | 'seo' | 'ads' | 'approver' | 'client'

export type ModuleKey =
  | 'dashboard'
  | 'onboarding'
  | 'agents'
  | 'seo'
  | 'offpage'
  | 'ads'
  | 'connectors'
  | 'approvals'
  | 'reports'
  | 'admin'
  | 'content'
  | 'brand_kit'

/** What a role may do in one module. */
export type Access = 'full' | 'view' | 'none'

export type AccessMap = Record<ModuleKey, Access>

// ── Session ────────────────────────────────────────────────────────────────
export interface Tokens {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface UserOut {
  id: string
  name: string
  email: string
  role: Role
  role_label: string
  is_owner: boolean
  email_verified: boolean
  last_login_at: string | null
}

export interface OrganizationOut {
  id: string
  name: string
  slug: string
  primary_domain: string
  global_autonomy: boolean
  plan_name: string
  seats_total: number
  seats_used: number
  renews_on: string | null
}

export interface SessionOut {
  user: UserOut
  organization: OrganizationOut
  access: AccessMap
  /** Modules still on after portal feature flags. */
  enabled_modules: string[]
  /** True when the caller's email is on PORTAL_ADMIN_EMAILS. */
  is_portal_admin: boolean
  onboarding_complete: boolean
  pending_approvals: number
  running_agents: number
  total_agents: number
  connected_connectors: number
  total_connectors: number
  /** route -> how much is waiting there. Zeroes are omitted. */
  nav_counts: Record<string, number>
  /** Every workspace this address can open. */
  workspaces: WorkspaceCard[]
}

export interface WorkspaceCard {
  id: string
  name: string
  slug: string
  primary_domain: string
  role: Role
  role_label: string
  onboarding_complete: boolean
  is_current: boolean
  is_owner: boolean
}

export interface AuthResponse {
  tokens: Tokens
  session: SessionOut
}

/**
 * The answer to a code request. Carries no code, and is identical for an
 * address with an account and one without.
 */
export interface CodeChallenge {
  detail: string
  /** Seconds a code stays valid once sent. */
  expires_in: number
  /** Seconds before another code may be requested. */
  resend_in: number
}

export interface SsoProvider {
  id: string
  label: string
  start_url: string
}

export interface RegistrationPolicy {
  public_signup: boolean
  /** Empty when the deployment has no identity provider configured. */
  sso_providers: SsoProvider[]
  /** Always 'email_code' in this build: there are no passwords. */
  auth_method: 'email_code'
  /** How many boxes the code input should draw. */
  code_length: number
  /** Seconds a code stays valid - the screen counts it down. */
  code_expires_in: number
  /** Seconds before "Send another" becomes available. */
  resend_in: number
  work_email_required: boolean
  roles: { value: Role; label: string }[]
}

export interface InvitationPreview {
  organization_name: string
  email: string
  role: Role
  role_label: string
  expires_at: string
}

// ── Shared ─────────────────────────────────────────────────────────────────
export interface Toast {
  message: string
  kind: 'info' | 'success' | 'warning' | 'error'
}

export interface ActionResult {
  ok: boolean
  toast: Toast | null
}

export interface Message {
  detail: string
}

// ── Agents ─────────────────────────────────────────────────────────────────
export interface AgentOut {
  /** The agent record's id; its settings versions are kept under it (history subject). */
  id: string
  slug: string
  name: string
  category: string
  description: string
  status: 'running' | 'paused' | 'error'
  status_label: string
  autonomy: boolean
  autonomy_label: string
  metric_label: string
  next_run: string
  schedule: string
  scope: string
  scope_placeholder: string
  notify_channel: string
  /** Connected AI model connector slug when this agent writes with a model. */
  llm_connector: string
  /** True when Configure must ask which model connector to use. */
  requires_llm: boolean
  max_actions_per_day: number
  configured: boolean
  config_summary: string
  /** "Saved, but…" - valid, and will not do everything you probably expect. */
  warnings: string[]
  last_error: string
  actions_today: number
  read_only: boolean
  /** attention | running | ready | idle - which section this card sits in. */
  group: string
  /** A pass is open right now. */
  busy: boolean
  step: string
  step_done: number
  step_total: number
  /** Null where the step has no countable total; no invented bar. */
  step_percent: number | null
  busy_for: string
  last_run_summary: string
  last_run_status: string
  // ── Plain-language guide, derived on the server from the agent's code ──
  summary: string
  effect: string
  /** False for agents that only report and never publish anything. */
  makes_changes: boolean
  approval_behaviour: string
  approval_if_auto: string
  approval_if_review: string
  /** Workspace approval policy: full | hybrid | human. */
  guardrail: string
  scope_label: string
  scope_help: string
  /** False when the agent has no use for the scope box. */
  uses_scope: boolean
  /** Agent-specific settings, with saved values ("" = default applies). */
  settings_fields: { key: string; label: string; help: string; kind: string; default: string; value: string }[]
  needs: AgentNeed[]
  /** Every required need is met, so a run will not be skipped for setup. */
  ready: boolean
  // ── Spending-agent safety ──
  /** Can spend money or change an ad account (practice mode applies). */
  spending?: boolean
  /** The agent's own practice switch. */
  practice_mode?: boolean
  /** Practice mode in force: platform | workspace | agent | '' (off). */
  practice_source?: string
  practice_note?: string
  /** 'Automatic changes switch on once Google Ads is verified.' */
  automatic_note?: string
  /** "Always use the model I picked": skip a run rather than switch models. */
  strict_model?: boolean
}

export interface NeedOption {
  slug: string
  name: string
  connected: boolean
  /** The workspace's plan does not include it; show it disabled with the reason. */
  locked?: boolean
  locked_reason?: string
}

/**
 * Which connector(s) an agent uses for one need, and the control to change
 * it — resolved on the server by the same rules a run follows.
 *
 * - `one`: a single connector (`control` "single"), or — for a multi `one` —
 *   every ticked website (`control` "multi"); nothing chosen = automatic.
 * - `all`: every connected one, or only the ticked ones.
 * - `page_source`: pages from every website, or only the ticked ones.
 *
 * `control` is "" where the selection is made on another need. The answer
 * goes back in `AgentConfigRequest.connectors[capability]`.
 */
export interface NeedUse {
  /** Key the selection is saved under. */
  capability: string
  mode: 'one' | 'all' | 'page_source'
  question: string
  /** False on a need whose selection is made on another need. */
  primary: boolean
  control: 'single' | 'multi' | ''
  /** single: offer "Automatic" (the agent tries each connected one in turn). */
  automatic_option: boolean
  /** The saved selection. */
  chosen: string[]
  /** What the control shows pre-filled: what the run uses now. */
  selected: string[]
  /** Part of the saved selection is no longer connected. */
  stale: boolean
  /** What a run uses now (in the order it tries them). */
  using: NeedOption[]
  /** Connected options, in catalogue order; locked ones included. */
  options: NeedOption[]
  /** Plain-language answer for the saved configuration. */
  statement: string
  /** What nothing chosen (automatic / every one) does now. */
  automatic_statement: string
  /** A selection in words, with `{names}` to fill in. */
  template: string
  note: string
}

/** One prerequisite for an agent. */
export interface AgentNeed {
  key: string
  /** integration | agent | setting */
  kind: 'integration' | 'agent' | 'setting'
  label: string
  satisfied: boolean
  optional: boolean
  note: string
  options: NeedOption[]
  /** Which connector(s) the agent uses for this need. */
  uses?: NeedUse[]
}

/** One notification channel, and whether anything can actually deliver it.
 *
 * `available` is false when nothing connected can carry it. The dialog
 * renders those unselectable rather than letting somebody pick a channel
 * that would silently drop every message. */
export interface NotifyChannelOut {
  value: string
  available: boolean
  reason: string
}

export interface LlmConnectorOut {
  slug: string
  name: string
  available: boolean
  reason: string
  /** The workspace's plan does not include it; shown disabled with the reason. */
  locked?: boolean
  locked_reason?: string
}

export interface AgentOptions {
  schedules: string[]
  notify_channels: NotifyChannelOut[]
  llm_connectors: LlmConnectorOut[]
}

export interface RunStep {
  label: string
  text: string
  done: number
  total: number
  /** Only where the step has a countable total. */
  percent: number | null
  at: string | null
  ended_at: string | null
}

export interface RunEvent {
  at: string
  action: string
  /** Sent for approval rather than applied. */
  awaiting_approval: boolean
  /** Practised: planned and recorded, not sent. */
  practice?: boolean
  /** Why it waits: unverified connector or a workspace limit. */
  held?: string[]
}

/** running | interrupted | succeeded | skipped | failed */
export type RunState = 'running' | 'interrupted' | 'succeeded' | 'skipped' | 'failed'

export interface AgentRun {
  id: string
  agent_slug: string
  agent_name: string
  trigger: string
  triggered_by: string
  status: string
  state: RunState
  state_label: string
  started_at: string
  finished_at: string | null
  duration_ms: number
  duration_label: string
  actions_taken: number
  actions_queued: number
  summary: string
  error: string
  cost: number
  step: string
  step_percent: number | null
  last_update_at: string | null
  timeline: RunStep[]
  /** Only on the single-run endpoint. */
  events: RunEvent[]
  /** Practice mode: changes planned and recorded instead of sent. */
  actions_practised?: number
  /** Where practice mode came from for this run ('' when off). */
  practice?: string
}

/** What one agent has produced, as a generic table. */
export interface AgentResults {
  title: string
  description: string
  metrics: { label: string; value: string }[]
  columns: { key: string; label: string; kind: 'text' | 'status' | 'number' | 'muted' }[]
  /** cells[key] is the value; cells[`${key}_sub`] an optional second line. */
  rows: { cells: Record<string, string>; tones: Record<string, string> }[]
  total: number
  empty: string
  link: string
  link_label: string
}

export interface AgentActivity {
  running: AgentRun[]
  recent: AgentRun[]
}

export interface AgentConfigRequest {
  schedule: string
  scope: string
  notify_channel: string
  llm_connector: string
  max_actions_per_day: number
  /** Agent-specific settings, by key. */
  settings?: Record<string, string>
  /**
   * Which connectors to use, by capability ([] = automatic / every one).
   * Omit to keep the saved selections; an object replaces them.
   */
  connectors?: Record<string, string[]>
  /** Spending agents: plan changes but don't send them. Omit to keep. */
  practice_mode?: boolean
  /** Agents that write with a model: never switch to another one. Omit to keep. */
  strict_model?: boolean
}

// ── Connectors ─────────────────────────────────────────────────────────────
export interface CredentialField {
  key: string
  label: string
  /** `model`: an AI model picked from the vendor's own live list. */
  type: 'text' | 'password' | 'number' | 'url' | 'oauth' | 'model'
  placeholder: string
  required: boolean
  help_text: string
  is_oauth: boolean
  oauth_label: string
  /** Pre-filled when the workspace has no saved hint yet. */
  default?: string
  /** For `type === 'model'`: which models to offer, `text` or `image`. */
  model_kind?: string
}

/** One model from the vendor's own list (GET/POST /connectors/{slug}/models). */
export interface ModelOption {
  id: string
  label: string
  /** Unix seconds from the vendor, when it publishes one. */
  created: number | null
  /** `text`, `image`, or both when the vendor does not say. */
  kinds: string[]
}

export interface ModelList {
  /** False when the vendor publishes no list: the person types a name. */
  listable: boolean
  models: ModelOption[]
  /** The vendor's newest text model by its own timestamp, or ''. */
  suggested: string
  source: string
  /** Plain words when the list could not be read. */
  error: string
  /** Set when the saved model is no longer offered. */
  stale: string
  models_url: string
}

/** How the connect screen presents an integration (from the API). */
export interface ConnectGuide {
  /** sign_in | wordpress | key | form | advanced */
  mode: 'sign_in' | 'wordpress' | 'key' | 'form' | 'advanced'
  provider: string
  provider_label: string
  sign_in_available: boolean
  button_label: string
  access_summary: string
  choice_noun: string
  signed_in: boolean
  signed_in_account: string
  pending_choice: boolean
  reuse_account: string
  also_connect: { slug: string; name: string }[]
  /** Only sent to workspace admins and portal admins. */
  admin_note: string
  sign_in_hidden_fields: string[]
  /** When a sign-in that cannot renew itself ends (Meta, LinkedIn). */
  sign_in_note?: string
  /** The link to the paste-it-yourself route on the sign-in path. */
  manual_label?: string
  steps: string[]
  key_url: string
  key_url_label: string
  key_field: string
  key_pattern: string
  key_hint: string
  /** Phase 2B: asked before the sign-in button (Shopify store, Salesforce sandbox). */
  sign_in_inputs?: SignInInput[]
  /** Phase 2B: the platform includes a key; offer "Use the included access". */
  platform_key_available?: boolean
  platform_key_summary?: string
  /** Sign-in is the way to connect, but it is not switched on yet: a switched-off button and this sentence. */
  setup_pending?: boolean
  setup_message?: string
  /** "Notify my admin" (members only). */
  can_request_setup?: boolean
  /** Portal admins: where to switch the sign-in on. */
  admin_setup_url?: string
  admin_setup_label?: string
  /** Workspace admins: who can switch it on. */
  admin_hint?: string
  /** The own-app-credentials route, offered as a secondary link (admins). */
  developer_label?: string
}

export interface SignInInput {
  key: string
  label: string
  /** text | toggle */
  kind: string
  placeholder: string
  help_text: string
  /** The value used last time, when there is one. */
  value: string
}

export interface OAuthStartOut {
  authorize_url: string
  /** redirect | connected | choose */
  status: 'redirect' | 'connected' | 'choose'
}

export interface OAuthChoice {
  id: string
  label: string
  detail: string
}

export interface SignInField {
  key: string
  label: string
  secret: boolean
  multiline: boolean
  /** Empty when the provider always needs it; otherwise the one integration that does. */
  needed_for: string
  help: string
  placeholder: string
  is_set: boolean
  /** portal (saved here) | server (environment) | '' */
  source: '' | 'portal' | 'server'
  /** The value itself for non-secrets; "Set · ends in 1234" for secrets. Never the secret. */
  preview: string
}

export interface SignInProvider {
  name: string
  label: string
  configured: boolean
  redirect_uri: string
  settings_keys: string[]
  missing: string[]
  connectors: {
    slug: string
    name: string
    available: boolean
    missing: string[]
    needs: string[]
    scopes: string[]
  }[]
  console_url: string
  note: string
  status: 'ready' | 'partial' | 'not_set_up' | 'built_in'
  status_detail: string
  console_label: string
  steps: string[]
  permissions: string[]
  permissions_where: string
  review_note: string
  fields: SignInField[]
  saved_here: boolean
  updated_by: string
  updated_at: string
  checkable: boolean
}

export interface SignInCheck {
  /** ok | failed | unchecked */
  status: 'ok' | 'failed' | 'unchecked'
  message: string
}

export interface SignInProviderSaved {
  provider: SignInProvider
  check: SignInCheck
}

export interface ConnectorOut {
  slug: string
  name: string
  category: string
  description: string
  connected: boolean
  status_label: string
  health: 'ok' | 'degraded' | 'failing' | 'unknown'
  last_error: string
  connected_at: string | null
  last_sync_at: string | null
  /** "Last sync 12m ago" - rendered by the API, empty when there is none. */
  activity_label: string
  hints: Record<string, string>
  fields: CredentialField[]
  /** Plan tiers, versions and scopes this platform needs, from the API. */
  requirements: string[]
  docs_url: string
  read_only: boolean
  /** attention | connected | available. */
  group: string
  /** What this integration lets agents do, in plain words. */
  abilities: string[]
  /** Agents this integration can serve. */
  used_by: NeedOption[]
  /** How to connect it. */
  connect: ConnectGuide
  /** Live verification: label is "Beta" until the type passes a live check, empty once it has. */
  verification?: ConnectorVerification
}

export interface ConnectorVerification {
  verified: boolean
  label: string
  tooltip: string
}

// ── Portal Admin → Verification ────────────────────────────────────────────
export type VerificationEnvironment = 'live' | 'test_account' | 'sandbox'

export interface VerificationStep {
  key: string
  label: string
  kind: 'read' | 'write'
  ok: boolean
  skipped: boolean
  detail: string
  evidence: Record<string, unknown>
  seconds: number
}

export interface VerificationRun {
  ok: boolean
  environment: VerificationEnvironment
  environment_label: string
  started_at: string | null
  finished_at: string | null
  run_by: string
  workspace_id: string | null
  workspace_name: string
  note: string
  steps: VerificationStep[]
}

export interface VerificationRow {
  slug: string
  name: string
  category: string
  has_steps: boolean
  verified: boolean
  state: 'verified' | 'failed' | 'expired' | 'never' | 'no_check'
  summary: string
  environment: string
  environment_label: string
  last_run_at: string | null
  last_ok_at: string | null
  reason: string
  running: boolean
  run_error: string
  latest: VerificationRun | null
}

export interface VerificationList {
  hide_unverified_connectors: boolean
  verified_for_days: number
  storage_ready: boolean
  items: VerificationRow[]
}

export interface VerificationWorkspace {
  id: string
  name: string
  slug: string
  environment: VerificationEnvironment
  environment_label: string
}

export interface VerificationRunStarted {
  status: string
  toast: Toast | null
}

export interface VerificationSettings {
  hide_unverified_connectors: boolean
  toast: Toast | null
}

// ── SEO ────────────────────────────────────────────────────────────────────
export interface SeoIssueOut {
  id: string
  url: string
  kind: string
  /** Human wording, from the API - the console keeps no second copy. */
  kind_label: string
  severity: 'high' | 'medium' | 'low'
  summary: string
  /** What to do about it. The difference between a report and a service. */
  recommendation: string
  status: 'open' | 'fixed' | 'ignored'
  age_label: string
  ignore_note: string
  /** True when Technical Fix Applier can propose a gated CMS/git write. */
  fixable?: boolean
  /** True when a fix for this finding is already waiting in Approvals. */
  pending_fix?: boolean
}

export interface SeoAuditOut {
  issues: SeoIssueOut[]
  open_count: number
  high_count: number
  fixed_this_month: number
  pages_audited: number
  by_kind: { kind: string; label: string; count: number }[]
  /** Set when no page-speed source is connected, so "no speed issues" and
   *  "speed was never measured" cannot look the same on screen. */
  vitals_unavailable: string
  last_audit_at: string | null
  read_only: boolean
}

export interface SeoPageOut {
  id: string
  url: string
  title: string
  gap_score: number
  aeo_pairs: number
  schema_text: string
  status: 'flagged' | 'queued' | 'rewritten' | 'live'
  status_label: string
  missing_topics: string[]
  show_approve: boolean
  last_synced_at: string | null
}

export interface QaPairOut {
  question: string
  answer: string
  status: string
  injected: boolean
}

export interface SchemaPatchOut {
  schema_type: string
  status: string
  json_ld: Record<string, unknown>
}

export interface SeoPageDetail extends SeoPageOut {
  body: string
  proposed_body: string
  rewrite_rationale: string
  target_keywords: string[]
  /** The AEO injector's pairs for this page - previously stored and shown nowhere. */
  qa_pairs: QaPairOut[]
  schema_patches: SchemaPatchOut[]
}

export interface ReferralSpamOut {
  time: string
  referrer: string
  category: string
  action: string
  sessions_affected: number
}

export interface SeoWorkspace {
  pages: SeoPageOut[]
  total: number
  referral_spam: ReferralSpamOut[]
  read_only: boolean
}

// ── AI visibility ──────────────────────────────────────────────────────────
export interface VisibilityQueryOut {
  id: string
  question: string
  label: string
  active: boolean
  tags: string[]
  created_at: string
}

export interface VisibilityObservationOut {
  id: string
  query_id: string | null
  question: string
  engine: string
  brand_cited: boolean
  brand_position: number | null
  cited_domains: string[]
  competitors_cited: string[]
  answer_excerpt: string
  observed_at: string
}

export interface VisibilityEngineShare {
  engine: string
  probes: number
  brand_citations: number
  share: number
}

export interface VisibilityCompetitorShare {
  domain: string
  sightings: number
}

export interface VisibilitySummaryOut {
  probes: number
  brand_citations: number
  brand_share: number
  by_engine: VisibilityEngineShare[]
  top_competitors: VisibilityCompetitorShare[]
  read_only: boolean
}

export interface VisibilityWorkspace {
  queries: VisibilityQueryOut[]
  competitors: string[]
  recent: VisibilityObservationOut[]
  summary: VisibilitySummaryOut
  read_only: boolean
}

// ── Off-page ───────────────────────────────────────────────────────────────
export interface BacklinkOut {
  id: string
  domain: string
  authority: number
  placement_type: string
  status: string
  status_label: string
  relevance: number
  show_launch: boolean
  /** False when authority was never measured (no backlink index connected). */
  authority_measured: boolean
  has_contact: boolean
  /** e.g. j***@editor.com — safe to show to everyone. */
  contact_email_masked: string
  /** Full address, only for roles that can edit Off-Page. */
  contact_email: string | null
}

export interface CompetitorAlertOut {
  id: string
  text: string
  competitor: string
  source_domain: string
  authority: number
  counter_launched: boolean
  button_label: string
}

export interface OffPageWorkspace {
  backlinks: BacklinkOut[]
  alerts: CompetitorAlertOut[]
  read_only: boolean
}

// ── Ads ────────────────────────────────────────────────────────────────────
export interface BudgetChannelOut {
  channel: string
  label: string
  percent: number
  cac: number
  spend: number
  conversions: number
  last_shift: number
  locked: boolean
  connected: boolean
}

export interface CreativeOut {
  id: string
  platform: string
  dimensions: string
  headline: string
  status: string
}

export interface FraudEventOut {
  time: string
  source: string
  reason: string
  action: string
  channel: string
  spend_saved: number
}

export interface AudienceOut {
  id: string
  label: string
  size: number
  cohesion: number
  top_signals: string[]
  activated_channels: string[]
  is_live: boolean
}

export interface AdsWorkspace {
  budgets: BudgetChannelOut[]
  budget_total: number
  creatives: CreativeOut[]
  audiences: AudienceOut[]
  fraud_log: FraudEventOut[]
  blended_cac: number
  read_only: boolean
}

// ── Approvals ──────────────────────────────────────────────────────────────
export interface ApprovalOut {
  id: string
  type: string
  title: string
  agent: string
  agent_slug: string
  meta: string
  target_kind: string
  submitted_at: string
  /** pending | practised | superseded (and the decided states, where listed) */
  status?: string
  /** Practised in practice mode: recorded, never sent. */
  practice?: boolean
  /** Practice: 'apply' (would have gone ahead) or 'queue' (would have waited). */
  would?: string
  /** Why it waits for a person: unverified connector or a workspace limit. */
  why?: string[]
  /** This person may send it back with feedback for another version. */
  can_request_changes?: boolean
  /** This person may correct the wording directly (no AI call). */
  can_edit_text?: boolean
  /** Stored preview image for a creative, served by `/files/{id}`. */
  preview_file_id?: string | null
}

export interface ApprovalDetail extends ApprovalOut {
  payload: Record<string, unknown>
}

/**
 * Result of "Ask for changes" or "Edit text" on an approval. The item acted on
 * is superseded; `approval_id` is the new item that replaces it, when one was
 * queued.
 */
export interface RevisionOut extends ActionResult {
  approval_id?: string | null
  /** Revisions used so far for this piece of work. */
  revision?: number
  /** The most revisions this workspace allows per piece of work. */
  max_revisions?: number
  /** The background job making the revised version (creatives), when one started. */
  job_id?: string | null
}

/** The states an item can leave the queue with. */
export type ApprovalHistoryStatus = 'approved' | 'rejected' | 'superseded' | 'expired'

/** One item from `GET /approvals/history`: what it was, who decided, and when. */
export interface ApprovalHistoryItem {
  id: string
  type: string
  title: string
  agent: string
  agent_slug: string
  target_kind: string
  status: ApprovalHistoryStatus | string
  /** The status in plain words, e.g. "Replaced by a revised version". */
  status_label: string
  submitted_at: string
  decided_at: string | null
  /** Who decided ("System" for an expiry). */
  decided_by: string
  /** The reviewer's note, or why it closed. */
  note: string
  revision: number
  /** The earlier version this one replaced, when it is a revision. */
  revises_id: string | null
  /** The revised version that replaced this one, once it exists. */
  replaced_by_id: string | null
  replaced_by_status: string | null
}

export interface ApprovalHistoryPage {
  items: ApprovalHistoryItem[]
  /** Pass back as `cursor` for the next page; null on the last page. */
  next_cursor: string | null
}

/** Query of `GET /approvals/history`. `limit` is at most 100. */
export interface ApprovalHistoryParams {
  status?: ApprovalHistoryStatus
  type?: string
  cursor?: string
  limit?: number
}

/** Body of `POST /approvals/{id}/request-changes`. */
export interface RequestChangesBody {
  feedback: string
}

/** Body of `POST /approvals/{id}/edit-text`: field name to corrected text. */
export interface EditTextBody {
  fields: Record<string, string>
}

// ── Audit export ───────────────────────────────────────────────────────────
export type AuditExportFormat = 'csv' | 'json'

export interface AuditExportParams {
  /** Inclusive start date, YYYY-MM-DD. */
  from: string
  /** Inclusive end date, YYYY-MM-DD. */
  to: string
  format: AuditExportFormat
  /** Opaque cursor from the previous page's `nextCursor`. */
  cursor?: string
}

export interface AuditExportFile {
  blob: Blob
  filename: string
  /** Present when more rows remain; pass back as `cursor` for the next page. */
  nextCursor: string | null
}

// ── Onboarding ─────────────────────────────────────────────────────────────
export interface OnboardingOut {
  step: number
  domain: string
  cms: string
  guardrail: string
  guardrail_label: string
  completed: boolean
  /** Each CMS with its connector slug, so the mark can be shown beside it. */
  cms_options: { slug: string; name: string }[]
  /** The short name and the consequence, kept apart - the label is both. */
  guardrail_options: { value: string; name: string; label: string; detail: string }[]
  /** What is actually connected, replacing three checkboxes nothing read. */
  ad_platforms: { slug: string; name: string; connected: boolean }[]
}

// ── Workspace policy (PUT /onboarding/policy) ─────────────────────────────
/**
 * "Review before publishing" and the agents' switch, saved together in one
 * transaction. Switching to 'full' needs `confirm: true` (422 with
 * `needs_confirmation` otherwise).
 */
export interface WorkspacePolicyRequest {
  guardrail: 'human' | 'hybrid' | 'full'
  autonomous: boolean
  confirm?: boolean
}

export interface WorkspacePolicyOut extends ActionResult {
  guardrail: string
  guardrail_label: string
  autonomous: boolean
  /** How many agents' own switches changed with it. */
  agents_updated: number
  /** The version recorded; null when nothing changed. */
  version: number | null
}

// ── Dashboard & reports ────────────────────────────────────────────────────
export interface KpiOut {
  organic_sessions: string
  organic_sessions_raw: number
  agents_running: number
  agents_total: number
  connectors_connected: number
  connectors_total: number
  pending_approvals: number
}

export interface DashboardOut {
  kpis: KpiOut
  /** The agents actually doing something, not the head of the catalogue. */
  agents: AgentOut[]
  /** How many there are in total, so the panel can say "showing 6 of 9". */
  active_agents: number
  /** Configured and stopped - the state the panel could not see. */
  ready: AgentOut[]
  ready_agents: number
  /** The connected integrations. Empty for a role that cannot see them. */
  connectors: ConnectorOut[]
  approvals: ApprovalOut[]
  global_autonomy: boolean
}

export interface ReportsOut {
  range: '7d' | '30d' | '90d'
  organic_sessions: string
  aeo_citations: number
  /** null when there is no source to count won links from. */
  backlinks_won: number | null
  /** Why backlinks_won is missing or partial, in plain words. */
  backlinks_won_note?: string
  /** null when no ad source is connected (not tracked, rather than zero). */
  blended_cac: number | null
  /** Why blended_cac is missing, in plain words ("Not tracked yet", "No conversions yet"). */
  blended_cac_note?: string
  /** null when no ad source is connected (not tracked, rather than zero). */
  ad_spend: string | null
  /** Why ad_spend is missing, in plain words. */
  ad_spend_note?: string
  fraud_blocked: number
  referral_spam_blocked: number
  pages_optimised: number
  trend: number[]
  /** False when nothing is connected that the report can read. */
  available: boolean
  /** What to connect to fill the report, when it is not available. */
  connect_hint: string
  /** The trend with null on days that have no data (not zero). */
  trend_with_gaps: (number | null)[]
  /** How many days the trend covers. */
  trend_days: number
}

// ── Admin ──────────────────────────────────────────────────────────────────
export interface TeamMemberOut {
  id: string
  name: string
  email: string
  role: Role
  role_label: string
  is_owner: boolean
  is_active: boolean
  email_verified: boolean
  last_login_at: string | null
  editable: boolean
}

export interface NotificationsOut {
  entries: AuditEntryOut[]
  /** Activity you have not looked at, excluding your own actions. */
  unseen: number
  pending_approvals: number
}

export interface AuditEntryOut {
  time: string
  actor: string
  actor_type: 'user' | 'agent' | 'system'
  action: string
  module: string
}

export interface BillingOut {
  plan_name: string
  seats_total: number
  seats_used: number
  seats_percent: number
  renews_on: string | null
}

export interface InvitationOut {
  id: string
  email: string
  role: Role
  role_label: string
  expires_at: string
  accepted_at: string | null
}

export interface AdminOut {
  team: TeamMemberOut[]
  invitations: InvitationOut[]
  audit: AuditEntryOut[]
  billing: BillingOut
  role_options: { value: Role; label: string }[]
  read_only: boolean
}

// ── Portal Admin (platform) ────────────────────────────────────────────────
export interface PortalOverview {
  organizations: number
  users: number
  accounts: number
  features_total: number
  features_disabled: number
}

export interface PortalOrganization {
  id: string
  name: string
  slug: string
  plan_name: string
  plan_tier: string
  seats_total: number
  member_count: number
  is_active: boolean
  created_at: string
}

export interface PortalUser {
  id: string
  name: string
  email: string
  role: string
  role_label: string
  is_owner: boolean
  is_active: boolean
  last_login_at: string | null
}

export interface PortalAccountSeat {
  organization_id: string
  organization_name: string
  organization_slug: string
  plan_name: string
  plan_tier: string
  organization_active: boolean
  user_id: string
  role: string
  role_label: string
  is_owner: boolean
  is_active: boolean
  last_login_at: string | null
  sso_provider: string
  locked: boolean
}

export interface PortalAccount {
  email: string
  name: string
  workspace_count: number
  last_login_at: string | null
  sso_provider: string
  is_locked: boolean
  seats: PortalAccountSeat[]
}

export interface PortalFeature {
  kind: 'connector' | 'agent' | 'module' | string
  slug: string
  name: string
  enabled: boolean
}

// ── SEO Assistant ──────────────────────────────────────────────────────────
export type AssistantMode = 'ask' | 'action'

export interface AssistantChatRequest {
  message: string
  mode: AssistantMode
  history?: { role: 'user' | 'assistant'; content: string }[]
}

export interface RecommendedItem {
  slug: string
  name: string
  reason: string
  depends_on?: string[]
}

export interface ActionField {
  key: string
  label: string
  secret: boolean
  required: boolean
  placeholder: string
  help: string
  type: string
  is_oauth: boolean
}

export interface ActionStep {
  id: string
  type: 'connect_connector' | 'configure_agent' | 'resume_agent'
  slug: string
  title: string
  reason: string
  fields?: ActionField[]
  config?: Record<string, string | number> | null
}

export interface ActionPlan {
  summary: string
  steps: ActionStep[]
}

export interface AssistantChatResponse {
  reply: string
  mode: AssistantMode
  recommendations: {
    connectors: RecommendedItem[]
    agents: RecommendedItem[]
  }
  action_plan: ActionPlan | null
  model: string
}

// ── Health ─────────────────────────────────────────────────────────────────
/**
 * `/health`. Only `status` is guaranteed; the API no longer discloses its
 * version, environment, app name or model, and may add or drop the rest.
 */
export interface HealthStatus {
  status: string
  database?: string
  agents_registered?: number
  connectors_registered?: number
  scheduler?: string
  /** Which creative renderer is active; reported as degraded on fallback. */
  renderer?: string
  checked_at?: string
}

// ── Brand profile & content library ────────────────────────────────────────
export interface BrandProfile {
  business_name: string
  summary: string
  industry: string
  tone: string
  language: string
  call_to_action: string
  products_services: string[]
  value_propositions: string[]
  differentiators: string[]
  audiences: string[]
  competitors: string[]
  keywords: string[]
  locations: string[]
  source_url?: string
  pages_read?: string[]
  generated_at?: string
  edited?: boolean
}

export interface BrandProfileState {
  profile: BrandProfile | null
  /** An AI model is connected, so the profile can be generated. */
  can_generate: boolean
  suggested_url: string
  /** Workspace time zone; "" means UTC. */
  timezone: string
}

export interface ContentAsset {
  id: string
  /** article | search_campaign | social_post | email */
  kind: string
  channel: string
  title: string
  status: string
  status_label: string
  agent_slug: string
  agent_name: string
  scheduled_for: string | null
  published_at: string | null
  created_at: string | null
  remote_url: string
  error: string
  image_ids: string[]
  approval_id: string | null
  can_publish: boolean
  /** format key -> label */
  formats: Record<string, string>
  detail: Record<string, unknown>
}

export interface ContentAssetDetail extends ContentAsset {
  content: Record<string, unknown>
}

// ── Site analysis (in-portal history) and Portal Admin analyses ─────────────
export type AnalysisStatus = 'queued' | 'running' | 'done' | 'failed'

export interface SiteAnalysisSummary {
  id: string
  website: string
  business_url: string
  status: AnalysisStatus
  error: string | null
  score: number | null
  grade: string | null
  /** Overall score change since the previous completed run of the same site. */
  change: number | null
  issues: number | null
  model: string
  requested_by: string
  connectors: number
  created_at: string | null
  finished_at: string | null
  percent: number
}

export interface SiteAnalysisContext {
  connectors?: { slug: string; name: string; category: string; health: string; last_error: string; connected_at: string | null }[]
  agents?: { slug: string; name: string; status: string; configured: boolean; last_run_at: string | null }[]
  llm?: { slug: string; name: string }
  pagespeed?: string
  taken_at?: string
}

export interface ScoreChange {
  before: number | null
  after: number | null
  change: number | null
}

export interface SiteAnalysisComparison {
  previous_id: string
  previous_at: string | null
  overall: ScoreChange
  categories: Record<string, ScoreChange & { label: string }>
  fixed: { title: string; before: string; after: string }[]
  new_issues: { title: string; evidence: string; status: string }[]
  still_open: string[]
  connectors_added: string[]
  connectors_removed: string[]
}

export interface SiteAnalysisDetail extends SiteAnalysisSummary {
  progress: import('@/components/AnalysisReport').Step[]
  report: import('@/components/AnalysisReport').Report | null
  context: SiteAnalysisContext
  comparison: SiteAnalysisComparison | null
}

export interface SiteAnalysisList {
  analyses: SiteAnalysisSummary[]
  default_website: string
  models: { slug: string; name: string }[]
}

export interface PortalAnalysisRow {
  id: string
  source: 'website' | 'workspace'
  workspace: string | null
  workspace_id: string | null
  website: string
  business_url: string
  business_name: string
  status: AnalysisStatus
  error: string | null
  score: number | null
  grade: string | null
  model: string
  seconds: number | null
  created_at: string | null
  requested_by?: string
  connectors?: number
}

export interface PortalAnalyses {
  items: PortalAnalysisRow[]
  totals: { website: number; workspace: number; last_24h: number; failed: number }
}

// ── Audit history (Technical SEO, SEO & AEO scores, local ranks, brand) ─────
export type HistoryKind = 'technical_seo' | 'seo_scores' | 'local_ranks' | 'brand_profile'

export interface HistoryEntry {
  id: string
  created_at: string | null
  /** agent_run | baseline | generated | edited | restored */
  source: string
  label: string
  created_by: string | null
  run_id: string | null
  /** More items than the snapshot keeps (5,000), so lists may be incomplete. */
  truncated: boolean
  headline: number | null
  /** Headline change since the entry before it. */
  change: number | null
  metrics: Record<string, number | null | undefined>
  /** Brand profile only. */
  changed_fields?: string[] | null
  is_current?: boolean
}

export interface HistoryList {
  kind: HistoryKind
  entries: HistoryEntry[]
  /** When the first snapshot was kept; null for local ranks (read from the checks themselves). */
  started_at: string | null
  baseline_recorded?: boolean
  limit?: number
  /** Local ranks only. */
  trends?: LocalPhraseTrend[]
}

export interface Pair {
  before: number | null
  after: number | null
  change: number | null
}

export interface HistoryIssue {
  id: string
  severity: string
  kind: string
  label: string
  url: string
  target: string
}

export interface Listed<T> {
  count: number
  items: T[]
}

export interface TechnicalComparison {
  previous_id: string
  previous_at: string | null
  previous_label: string
  truncated: boolean
  open: Pair
  pages_audited: { before: number | null; after: number | null }
  speed: { measured_before: number | null; measured_after: number | null; open_before: number | null; open_after: number | null }
  severity: Record<'high' | 'medium' | 'low', Pair>
  kinds: (Pair & { kind: string; label: string })[]
  new_issues: Listed<HistoryIssue>
  fixed: Listed<HistoryIssue>
  ignored_since: Listed<HistoryIssue>
  still_open: Listed<HistoryIssue>
}

export interface ScoredPage {
  url: string
  title: string
  gap: number
  before?: number
  after?: number
  change?: number
}

export interface ScoresComparison {
  previous_id: string
  previous_at: string | null
  previous_label: string
  truncated: boolean
  average_gap: Pair
  pages_scored: Pair
  rewrites_pending: Pair
  rewritten: Pair
  distribution: { key: string; label: string; before: number; after: number }[]
  improved: Listed<ScoredPage>
  worsened: Listed<ScoredPage>
  unchanged: number
  added: Listed<ScoredPage>
  removed: Listed<ScoredPage>
}

export interface SnapshotDetail extends HistoryEntry {
  kind: HistoryKind
  all_metrics: Record<string, unknown>
  comparison: TechnicalComparison | ScoresComparison | null
}

export interface RankedPlace {
  position: number | null
  name: string
  rating: number | null
  review_count: number | null
}

export interface LocalPhraseTrend {
  phrase: string
  points: { at: string; position: number | null; results_seen: number }[]
  latest: number | null
  above: RankedPlace[]
}

export interface PhraseMove {
  phrase: string
  before: number | null
  after: number | null
  moved?: number
}

export interface LocalCheckDetail extends HistoryEntry {
  kind: 'local_ranks'
  phrases: { phrase: string; position: number | null; results_seen: number; above: RankedPlace[] }[]
  comparison: {
    previous_id: string
    previous_at: string
    before: Record<string, number | null>
    moved_up: PhraseMove[]
    moved_down: PhraseMove[]
    entered_pack: PhraseMove[]
    left_pack: PhraseMove[]
    entered_results: PhraseMove[]
    dropped_out: PhraseMove[]
    unchanged: string[]
    new_phrases: string[]
    stopped_phrases: string[]
  } | null
}

export interface BrandFieldDiff {
  field: string
  label: string
  type: 'text' | 'list'
  version: string | string[]
  current: string | string[]
  changed: boolean
  only_in_version?: string[]
  only_in_current?: string[]
}

export interface BrandVersionDetail extends HistoryEntry {
  kind: 'brand_profile'
  profile: Record<string, string | string[]>
  diff: BrandFieldDiff[]
}

// ── Spending-agent safety ──────────────────────────────────────────────────
export interface SafetyStatus {
  practice_mode: boolean
  stopped: boolean
  stopped_at: string
  stopped_by: string
  platform_stop: boolean
  platform_practice: boolean
  caps: Record<string, number>
  default_caps: Record<string, number>
  cap_fields: { key: string; label: string; help: string }[]
  spending_agents: string[]
  currency_symbol: string
  can_manage: boolean
}

export interface SafetyAction {
  status: SafetyStatus
  toast: Toast | null
}

export interface PracticeSummary {
  days: number
  changes: number
  automatic: number
  needs_approval: number
  budget_moved: number | null
  budget_moved_label: string
  sentence: string
}

export interface PlatformSwitch {
  on: boolean
  by: string
  at: string
}

export interface PlatformSafety {
  emergency_stop: PlatformSwitch
  practice_mode: PlatformSwitch
}

export interface PlatformSafetyAction {
  status: PlatformSafety
  toast: Toast | null
}
