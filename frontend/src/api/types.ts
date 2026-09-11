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
}

export interface AgentOptions {
  schedules: string[]
  notify_channels: NotifyChannelOut[]
  llm_connectors: LlmConnectorOut[]
}

export interface AgentRun {
  id: string
  agent_slug: string
  trigger: string
  status: string
  started_at: string
  finished_at: string | null
  duration_ms: number
  actions_taken: number
  actions_queued: number
  summary: string
  error: string
  cost: number
}

export interface AgentConfigRequest {
  schedule: string
  scope: string
  notify_channel: string
  llm_connector: string
  max_actions_per_day: number
}

// ── Connectors ─────────────────────────────────────────────────────────────
export interface CredentialField {
  key: string
  label: string
  type: 'text' | 'password' | 'number' | 'url' | 'oauth'
  placeholder: string
  required: boolean
  help_text: string
  is_oauth: boolean
  oauth_label: string
  /** Pre-filled when the workspace has no saved hint yet. */
  default?: string
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
}

export interface ApprovalDetail extends ApprovalOut {
  payload: Record<string, unknown>
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
  backlinks_won: number
  blended_cac: number
  ad_spend: string
  fraud_blocked: number
  referral_spam_blocked: number
  pages_optimised: number
  trend: number[]
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
export interface HealthStatus {
  status: string
  app: string
  environment: string
  version: string
  database: string
  agents_registered: number
  connectors_registered: number
  model: string
  scheduler: string
  checked_at: string
}
