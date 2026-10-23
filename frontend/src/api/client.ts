/**
 * The single HTTP client.
 *
 * Two things it owns that would otherwise be scattered through every screen:
 *
 * - **Token refresh.** Access tokens are short-lived. On a 401 the client
 *   redeems the refresh token once, then replays the original request. All
 *   concurrent 401s wait on the same refresh, so a screen that loads six
 *   endpoints at once does not burn six refresh tokens - which would trip the
 *   backend's reuse detection and sign the user out.
 *
 * - **Error shape.** The API returns `{code, detail, fields}`; that becomes an
 *   `ApiError` with the message already suitable for display, because the
 *   backend owns the wording.
 */
import type {
  ActionResult,
  AdminOut,
  AdsWorkspace,
  AgentConfigRequest,
  AgentOptions,
  AgentOut,
  AgentRun,
  ApprovalDetail,
  ApprovalHistoryPage,
  ApprovalHistoryParams,
  ApprovalOut,
  AuditExportFile,
  AuditExportParams,
  AuthResponse,
  CodeChallenge,
  ConnectorOut,
  DashboardOut,
  EditTextBody,
  HealthStatus,
  InvitationOut,
  InvitationPreview,
  Message,
  NotificationsOut,
  OffPageWorkspace,
  OnboardingOut,
  RegistrationPolicy,
  ReportsOut,
  RequestChangesBody,
  RevisionOut,
  Role,
  SeoAuditOut,
  SeoPageDetail,
  SeoWorkspace,
  SessionOut,
  VisibilityWorkspace,
  Tokens,
  WorkspacePolicyOut,
  WorkspacePolicyRequest,
} from './types'

declare global {
  interface Window {
    __APP_CONFIG__?: {
      VITE_API_BASE_URL?: string
    }
  }
}

const BASE_URL =
  window.__APP_CONFIG__?.VITE_API_BASE_URL ?? import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

let policyPromise: Promise<RegistrationPolicy> | null = null

const ACCESS_KEY = 'automarket.access_token'
const REFRESH_KEY = 'automarket.refresh_token'

/** Field map on `{code, detail, fields}`. Values are primitives only; nested objects are dropped. */
export type ApiErrorFields = Record<string, string | boolean | number>

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly fields: ApiErrorFields = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** True when the caller's role forbids this, rather than the request being wrong. */
  get isForbidden(): boolean {
    return this.status === 403
  }

  get isUnauthenticated(): boolean {
    return this.status === 401
  }
}

// ── Token storage ──────────────────────────────────────────────────────────
// sessionStorage rather than localStorage: closing the tab ends the session,
// which is the safer default for a console that can spend money.
export const tokenStore = {
  get access(): string | null {
    try {
      return sessionStorage.getItem(ACCESS_KEY)
    } catch {
      return null
    }
  },
  get refresh(): string | null {
    try {
      return sessionStorage.getItem(REFRESH_KEY)
    } catch {
      return null
    }
  },
  set(tokens: Tokens): void {
    try {
      sessionStorage.setItem(ACCESS_KEY, tokens.access_token)
      sessionStorage.setItem(REFRESH_KEY, tokens.refresh_token)
    } catch {
      /* private mode; the session simply will not survive a reload */
    }
  },
  clear(): void {
    try {
      sessionStorage.removeItem(ACCESS_KEY)
      sessionStorage.removeItem(REFRESH_KEY)
    } catch {
      /* ignore */
    }
  },
}

// ── Path and file-name guards ──────────────────────────────────────────────
const MAX_SEGMENT_CHARS = 200

/**
 * One id or slug as a single, encoded path segment. Every value that goes
 * into a request path passes through here, so a value taken from a link or a
 * reply ("..", "a/b", "..%2F") can never reach a different route: it is
 * either refused or encoded so it stays inside its own segment.
 */
function seg(value: string | null | undefined): string {
  const text = typeof value === 'string' ? value : ''
  if (
    !text ||
    text === '.' ||
    text === '..' ||
    text.length > MAX_SEGMENT_CHARS ||
    /[\u0000-\u001f\u007f]/.test(text)
  ) {
    throw new ApiError(400, 'invalid_path', 'That link is not valid. Please go back and try again.')
  }
  return encodeURIComponent(text)
}

/**
 * A download name safe to hand to the browser: the server's name (from
 * Content-Disposition) reduced to plain characters, with a fallback.
 */
export function safeFilename(name: string, fallback: string): string {
  const base = (name.split(/[\\/]/).pop() ?? '').replace(/[^A-Za-z0-9._-]/g, '_').replace(/^\.+/, '')
  return base && base.length <= 120 ? base : fallback
}

/** Called when the session is unrecoverable, so the app can route to login. */
let onSessionLost: (() => void) | null = null
export function setSessionLostHandler(handler: () => void): void {
  onSessionLost = handler
}

// ── Core request ───────────────────────────────────────────────────────────
type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface RequestOptions {
  method?: Method
  body?: unknown
  /** Set for the auth endpoints, which must not attempt a refresh. */
  skipAuth?: boolean
  signal?: AbortSignal
}

// One in-flight refresh, shared by every request that hits a 401.
let refreshInFlight: Promise<boolean> | null = null

async function refreshSession(): Promise<boolean> {
  const refresh_token = tokenStore.refresh
  if (!refresh_token) return false

  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(`${BASE_URL}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token }),
        })
        if (!response.ok) return false
        const data = (await response.json()) as AuthResponse
        tokenStore.set(data.tokens)
        return true
      } catch {
        return false
      } finally {
        // Cleared on the next tick so simultaneous callers all observe the
        // same result before a new refresh can start.
        setTimeout(() => {
          refreshInFlight = null
        }, 0)
      }
    })()
  }
  return refreshInFlight
}

/** Keep only primitive field values from an untrusted error body. */
function parseFields(raw: unknown): ApiErrorFields {
  if (raw == null || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const fields: ApiErrorFields = {}
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!key) continue
    if (typeof value === 'string' || typeof value === 'boolean') {
      fields[key] = value
    } else if (typeof value === 'number' && Number.isFinite(value)) {
      fields[key] = value
    }
  }
  return fields
}

async function parseError(response: Response): Promise<ApiError> {
  let code = 'error'
  let detail = response.statusText || 'Request failed'
  let fields: ApiErrorFields = {}
  try {
    const body = await response.json()
    code = typeof body.code === 'string' && body.code ? body.code : code
    detail = typeof body.detail === 'string' && body.detail ? body.detail : detail
    fields = parseFields(body.fields)
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(response.status, code, detail, fields)
}

/**
 * A JSON request with auth and one refresh-and-replay on 401. Exported for the
 * feature API modules (`api/creative.ts` and friends) so they share this
 * client rather than growing their own.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, skipAuth = false, signal } = options

  const send = async (): Promise<Response> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const token = tokenStore.access
    if (token && !skipAuth) headers.Authorization = `Bearer ${token}`

    return fetch(`${BASE_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  }

  let response = await send()

  if (response.status === 401 && !skipAuth) {
    const refreshed = await refreshSession()
    if (refreshed) {
      response = await send()
    } else {
      tokenStore.clear()
      onSessionLost?.()
      throw await parseError(response)
    }
  }

  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T

  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

/**
 * A binary download (export file, image), with the same auth and refresh.
 * `headers` is returned for downloads that page (the audit export's cursor).
 */
export async function requestBlob(
  path: string,
  options: { signal?: AbortSignal } = {},
): Promise<{ blob: Blob; filename: string; headers: Headers }> {
  const send = () => {
    const token = tokenStore.access
    return fetch(`${BASE_URL}${path}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: options.signal,
    })
  }
  let response = await send()
  if (response.status === 401 && (await refreshSession())) response = await send()
  if (!response.ok) throw await parseError(response)
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^";]+)"?/.exec(disposition)
  return {
    blob: await response.blob(),
    filename: safeFilename(match?.[1] ?? '', 'download'),
    headers: response.headers,
  }
}

/**
 * A multipart upload (logo, product photo), with the same auth and refresh.
 *
 * No Content-Type is set: the browser writes the multipart boundary itself.
 * The same FormData is replayed after a refresh, which is safe because a
 * FormData body (unlike a stream) can be sent more than once.
 */
export async function requestForm<T>(
  path: string,
  form: FormData,
  options: { method?: 'POST' | 'PUT'; signal?: AbortSignal } = {},
): Promise<T> {
  const { method = 'POST', signal } = options

  const send = async (): Promise<Response> => {
    const headers: Record<string, string> = {}
    const token = tokenStore.access
    if (token) headers.Authorization = `Bearer ${token}`
    return fetch(`${BASE_URL}${path}`, { method, headers, body: form, signal })
  }

  let response = await send()

  if (response.status === 401) {
    const refreshed = await refreshSession()
    if (refreshed) {
      response = await send()
    } else {
      tokenStore.clear()
      onSessionLost?.()
      throw await parseError(response)
    }
  }

  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T

  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

/** The audit export's paging header (`GET /admin/audit/export`). */
const NEXT_CURSOR_HEADER = 'X-Next-Cursor'

// ── Endpoints ──────────────────────────────────────────────────────────────
export const api = {
  // Brand profile and content library
  brandProfile: (signal?: AbortSignal) =>
    request<import('./types').BrandProfileState>('/brand-profile', { signal }),
  generateBrandProfile: (url: string) =>
    request<import('./types').BrandProfileState>('/brand-profile/generate', { method: 'POST', body: { url } }),
  updateBrandProfile: (values: Partial<import('./types').BrandProfile>) =>
    request<import('./types').BrandProfileState>('/brand-profile', { method: 'PUT', body: values }),
  assets: (filters: { kind?: string; status?: string; agent?: string } = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams(
      Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1])),
    ).toString()
    return request<import('./types').ContentAsset[]>(`/assets${query ? `?${query}` : ''}`, { signal })
  },
  asset: (id: string) => request<import('./types').ContentAssetDetail>(`/assets/${seg(id)}`),
  updateAsset: (id: string, body: { title?: string; content?: Record<string, unknown>; scheduled_for?: string }) =>
    request<import('./types').ContentAssetDetail>(`/assets/${seg(id)}`, { method: 'PUT', body }),
  publishAsset: (id: string) => request<ActionResult>(`/assets/${seg(id)}/publish`, { method: 'POST' }),
  rejectAsset: (id: string) => request<ActionResult>(`/assets/${seg(id)}/reject`, { method: 'POST' }),
  exportAsset: (id: string, format: string) =>
    requestBlob(`/assets/${seg(id)}/export?format=${encodeURIComponent(format)}`),
  imageBlob: (id: string) => requestBlob(`/assets/images/${seg(id)}`),

  // Auth
  //
  // Cached for the life of the page: the sign-in, sign-up and account screens
  // all need it, React re-invokes effects in development, and the answer is
  // deployment configuration that cannot change while the tab is open. The
  // promise itself is cached rather than the result, so concurrent callers
  // share one in-flight request instead of racing.
  policy: () => {
    if (!policyPromise) {
      policyPromise = request<RegistrationPolicy>('/auth/policy', {
        skipAuth: true,
      }).catch((error: unknown) => {
        // A failed fetch must not be cached, or the screens are stuck with it
        // until reload.
        policyPromise = null
        throw error
      })
    }
    return policyPromise
  },

  /** Step one of signing in: mail a code to the address. */
  requestCode: (email: string) =>
    request<CodeChallenge>('/auth/request-code', {
      method: 'POST',
      body: { email },
      skipAuth: true,
    }),

  /** Step one of signing up: prove the address before anything is created. */
  requestSignupCode: (email: string) =>
    request<CodeChallenge>('/auth/request-signup-code', {
      method: 'POST',
      body: { email },
      skipAuth: true,
    }),

  register: (payload: {
    organization_name: string
    full_name: string
    email: string
    code: string
    primary_domain?: string
  }) =>
    request<AuthResponse>('/auth/register', {
      method: 'POST',
      body: payload,
      skipAuth: true,
    }),

  /** Step two of signing in: redeem the code for a session. */
  login: (email: string, code: string) =>
    request<AuthResponse>('/auth/login', {
      method: 'POST',
      body: { email, code },
      skipAuth: true,
    }),

  logout: () => {
    const refresh_token = tokenStore.refresh
    if (!refresh_token) return Promise.resolve({ detail: 'Signed out' } as Message)
    return request<Message>('/auth/logout', {
      method: 'POST',
      body: { refresh_token },
      skipAuth: true,
    })
  },

  me: () => request<SessionOut>('/auth/me'),

  workspaces: () => request<import('./types').WorkspaceCard[]>('/workspaces'),
  createWorkspace: (payload: { name: string; primary_domain?: string }) =>
    request<AuthResponse>('/workspaces', { method: 'POST', body: payload }),
  switchWorkspace: (organizationId: string) =>
    request<AuthResponse>(`/workspaces/${seg(organizationId)}/switch`, {
      method: 'POST',
    }),
  deleteWorkspace: (organizationId: string) =>
    request<AuthResponse>(`/workspaces/${seg(organizationId)}`, {
      method: 'DELETE',
    }),

  /** Revoke every session, this one included. The passwordless remedy. */
  signOutEverywhere: () =>
    request<Message>('/auth/sign-out-everywhere', { method: 'POST' }),

  invitationPreview: (token: string) =>
    request<InvitationPreview>(`/auth/invitation?token=${encodeURIComponent(token)}`, {
      skipAuth: true,
    }),

  acceptInvitation: (payload: { token: string; full_name: string }) =>
    request<AuthResponse>('/auth/accept-invitation', {
      method: 'POST',
      body: payload,
      skipAuth: true,
    }),

  /** Recent workspace activity - what the header's bell shows. */
  notifications: () => request<NotificationsOut>('/notifications'),

  /** Clear the unseen count. A write, so a prefetch cannot clear it. */
  markNotificationsSeen: () =>
    request<Message>('/notifications/seen', { method: 'POST' }),

  // Dashboard & reports
  dashboard: (signal?: AbortSignal) =>
    request<DashboardOut>('/dashboard', { signal }),
  reports: (range: '7d' | '30d' | '90d', signal?: AbortSignal) =>
    request<ReportsOut>(`/reports?range=${range}`, { signal }),

  // Onboarding
  onboarding: (signal?: AbortSignal) =>
    request<OnboardingOut>('/onboarding', { signal }),
  saveOnboarding: (payload: Partial<{
    step: number
    domain: string
    cms: string
    guardrail: string
    /** Required with guardrail 'full' ("Publish automatically"), after the person confirms. */
    confirm: boolean
  }>) => request<OnboardingOut>('/onboarding', { method: 'PUT', body: payload }),
  /**
   * "Review before publishing" and the agents' switch in one atomic save.
   * 'full' needs `confirm: true` (422 with `needs_confirmation` otherwise).
   */
  setWorkspacePolicy: (body: WorkspacePolicyRequest) =>
    request<WorkspacePolicyOut>('/onboarding/policy', { method: 'PUT', body }),
  completeOnboarding: () =>
    request<ActionResult>('/onboarding/complete', { method: 'POST' }),

  // Agents
  agents: (signal?: AbortSignal) => request<AgentOut[]>('/agents', { signal }),
  agent: (slug: string) => request<AgentOut>(`/agents/${seg(slug)}`),
  agentOptions: (signal?: AbortSignal) =>
    request<AgentOptions>('/agents/options', { signal }),
  agentRuns: (slug: string, signal?: AbortSignal) =>
    request<AgentRun[]>(`/agents/${seg(slug)}/runs`, { signal }),
  agentRun: (slug: string, runId: string) =>
    request<AgentRun>(`/agents/${seg(slug)}/runs/${seg(runId)}`),
  agentResults: (slug: string, signal?: AbortSignal) =>
    request<import('./types').AgentResults>(`/agents/${seg(slug)}/results`, { signal }),
  agentActivity: (signal?: AbortSignal) =>
    request<import('./types').AgentActivity>('/agents/activity', { signal }),
  pauseAgent: (slug: string) =>
    request<ActionResult>(`/agents/${seg(slug)}/pause`, { method: 'POST' }),
  resumeAgent: (slug: string) =>
    request<ActionResult>(`/agents/${seg(slug)}/resume`, { method: 'POST' }),
  setAgentAutonomy: (slug: string, autonomous: boolean) =>
    request<ActionResult>(`/agents/${seg(slug)}/autonomy`, {
      method: 'POST',
      body: { autonomous },
    }),
  setGlobalAutonomy: (autonomous: boolean) =>
    request<ActionResult>('/agents/autonomy', { method: 'POST', body: { autonomous } }),
  configureAgent: (slug: string, config: AgentConfigRequest) =>
    request<AgentOut>(`/agents/${seg(slug)}/config`, { method: 'PUT', body: config }),
  /** Remove an agent's configuration and stop it. */
  resetAgentConfig: (slug: string) =>
    request<AgentOut>(`/agents/${seg(slug)}/config`, { method: 'DELETE' }),
  runAgent: (slug: string) =>
    request<ActionResult>(`/agents/${seg(slug)}/run`, { method: 'POST' }),

  // SEO
  seo: (search = '', signal?: AbortSignal) =>
    request<SeoWorkspace>(
      `/seo${search ? `?search=${encodeURIComponent(search)}` : ''}`,
      { signal },
    ),
  seoPage: (id: string) => request<SeoPageDetail>(`/seo/pages/${seg(id)}`),

  /** The Technical SEO screen: findings, counts and what to do. */
  seoAudit: (
    params: { status?: string; kind?: string; severity?: string } = {},
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value) query.set(key, value)
    }
    const suffix = query.toString()
    return request<SeoAuditOut>(`/seo/audit${suffix ? `?${suffix}` : ''}`, { signal })
  },
  ignoreIssue: (id: string, note: string) =>
    request<ActionResult>(`/seo/audit/${seg(id)}/ignore`, { method: 'POST', body: { note } }),
  reopenIssue: (id: string) =>
    request<ActionResult>(`/seo/audit/${seg(id)}/reopen`, { method: 'POST' }),
  requestPageApproval: (id: string) =>
    request<ActionResult>(`/seo/pages/${seg(id)}/request-approval`, { method: 'POST' }),

  // AI visibility
  visibility: (signal?: AbortSignal) =>
    request<VisibilityWorkspace>('/visibility', { signal }),
  addVisibilityQuery: (body: { question: string; label?: string; tags?: string[] }) =>
    request<VisibilityWorkspace['queries'][number]>('/visibility/queries', {
      method: 'POST',
      body,
    }),
  deleteVisibilityQuery: (id: string) =>
    request<ActionResult>(`/visibility/queries/${seg(id)}`, { method: 'DELETE' }),
  setVisibilityCompetitors: (competitor_domains: string[]) =>
    request<{ competitor_domains: string[] }>('/visibility/competitors', {
      method: 'PUT',
      body: { competitor_domains },
    }),

  // Off-page
  offpage: (signal?: AbortSignal) =>
    request<OffPageWorkspace>('/offpage', { signal }),
  setTargetContact: (targetId: string, email: string | null) =>
    request<ActionResult>(`/offpage/targets/${seg(targetId)}/contact`, {
      method: 'PUT',
      body: { email },
    }),
  launchOutreach: (targetId: string) =>
    request<ActionResult>(`/offpage/targets/${seg(targetId)}/outreach`, { method: 'POST' }),
  launchCounterPitch: (alertId: string) =>
    request<ActionResult>(`/offpage/alerts/${seg(alertId)}/counter-pitch`, { method: 'POST' }),

  // Ads
  ads: (signal?: AbortSignal) => request<AdsWorkspace>('/ads', { signal }),
  setBudget: (channel: string, percent: number) =>
    request<AdsWorkspace>('/ads/budget', { method: 'PUT', body: { channel, percent } }),
  unlockChannel: (channel: string) =>
    request<ActionResult>(`/ads/budget/${seg(channel)}/unlock`, { method: 'POST' }),
  rebalanceBudget: () =>
    request<ActionResult>('/ads/budget/rebalance', { method: 'POST' }),
  generateCreatives: () =>
    request<ActionResult>('/ads/creatives/generate', { method: 'POST' }),

  // Connectors
  connectors: (category = '', signal?: AbortSignal) =>
    request<ConnectorOut[]>(
      `/connectors${category && category !== 'All' ? `?category=${encodeURIComponent(category)}` : ''}`,
      { signal },
    ),
  connectorCategories: (signal?: AbortSignal) =>
    request<string[]>('/connectors/categories', { signal }),
  connect: (slug: string, values: Record<string, string>, oauth_completed = false) =>
    request<ConnectorOut>(`/connectors/${seg(slug)}/connect`, {
      method: 'POST',
      body: { values, oauth_completed },
    }),
  /** The models a connected AI integration's key can use, read live from the vendor. */
  connectorModels: (slug: string, signal?: AbortSignal) =>
    request<import('./types').ModelList>(`/connectors/${seg(slug)}/models`, { signal }),
  /** The same list with the key typed in the dialog, before saving. The key is not stored. */
  connectorModelsFor: (slug: string, values: Record<string, string>, signal?: AbortSignal) =>
    request<import('./types').ModelList>(`/connectors/${seg(slug)}/models`, {
      method: 'POST',
      body: { values },
      signal,
    }),
  disconnect: (slug: string) =>
    request<ConnectorOut>(`/connectors/${seg(slug)}/disconnect`, { method: 'POST' }),
  connector: (slug: string, signal?: AbortSignal) =>
    request<ConnectorOut>(`/connectors/${seg(slug)}`, { signal }),
  /** Where to send the browser to sign in (or, with reuse, connect at once). */
  oauthStart: (
    slug: string,
    body: { return_path: string; site_url?: string; reuse?: boolean; inputs?: Record<string, string> },
  ) =>
    request<import('./types').OAuthStartOut>(`/connectors/${seg(slug)}/oauth/start`, {
      method: 'POST',
      body,
    }),
  /** "Notify my admin": a sign-in this person wants is not switched on yet. */
  requestSignIn: (slug: string) =>
    request<ActionResult>(`/connectors/${seg(slug)}/request-sign-in`, { method: 'POST' }),
  oauthChoices: (slug: string, signal?: AbortSignal) =>
    request<import('./types').OAuthChoice[]>(`/connectors/${seg(slug)}/oauth/choices`, {
      signal,
    }),
  oauthChoose: (slug: string, choice_id: string) =>
    request<ConnectorOut>(`/connectors/${seg(slug)}/oauth/choose`, {
      method: 'POST',
      body: { choice_id },
    }),
  // Approvals
  approvals: (signal?: AbortSignal) =>
    request<ApprovalOut[]>('/approvals', { signal }),
  approvalCount: () => request<{ pending: number }>('/approvals/count'),
  approval: (id: string) => request<ApprovalDetail>(`/approvals/${seg(id)}`),
  /** Items that left the queue, newest decision first; page with `next_cursor`. */
  listApprovalHistory: (params: ApprovalHistoryParams = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams()
    if (params.status) query.set('status', params.status)
    if (params.type) query.set('type', params.type)
    if (params.cursor) query.set('cursor', params.cursor)
    if (params.limit) query.set('limit', String(Math.min(Math.max(1, Math.floor(params.limit)), 100)))
    const qs = query.toString()
    return request<ApprovalHistoryPage>(`/approvals/history${qs ? `?${qs}` : ''}`, { signal })
  },
  approve: (id: string, note = '') =>
    request<ActionResult>(`/approvals/${seg(id)}/approve`, { method: 'POST', body: { note } }),
  reject: (id: string, note = '') =>
    request<ActionResult>(`/approvals/${seg(id)}/reject`, { method: 'POST', body: { note } }),
  /** "Ask for changes": send it back with feedback for a new version. */
  requestChanges: (id: string, feedback: string) =>
    request<RevisionOut>(`/approvals/${seg(id)}/request-changes`, {
      method: 'POST',
      body: { feedback } satisfies RequestChangesBody,
    }),
  /** "Edit text": correct the wording directly; no AI call is made. */
  editApprovalText: (id: string, fields: Record<string, string>) =>
    request<RevisionOut>(`/approvals/${seg(id)}/edit-text`, {
      method: 'POST',
      body: { fields } satisfies EditTextBody,
    }),

  // Admin
  admin: (search = '', signal?: AbortSignal) =>
    request<AdminOut>(
      `/admin${search ? `?search=${encodeURIComponent(search)}` : ''}`,
      { signal },
    ),
  auditLog: (search = '') =>
    request<import('./types').AuditEntryOut[]>(
      `/admin/audit${search ? `?search=${encodeURIComponent(search)}` : ''}`,
    ),
  changeMemberRole: (id: string, role: Role) =>
    request<import('./types').TeamMemberOut>(`/admin/team/${seg(id)}/role`, {
      method: 'PUT',
      body: { role },
    }),
  deactivateMember: (id: string) =>
    request<ActionResult>(`/admin/team/${seg(id)}/deactivate`, { method: 'POST' }),
  invite: (email: string, role: Role) =>
    request<InvitationOut>('/admin/invitations', { method: 'POST', body: { email, role } }),
  revokeInvitation: (id: string) =>
    request<ActionResult>(`/admin/invitations/${seg(id)}`, { method: 'DELETE' }),
  /** One page of the activity log as a file; `nextCursor` fetches the next. */
  downloadAuditExport: async (params: AuditExportParams, signal?: AbortSignal): Promise<AuditExportFile> => {
    const query = new URLSearchParams({ from: params.from, to: params.to, format: params.format })
    if (params.cursor) query.set('cursor', params.cursor)
    const { blob, filename, headers } = await requestBlob(`/admin/audit/export?${query.toString()}`, { signal })
    return { blob, filename, nextCursor: headers.get(NEXT_CURSOR_HEADER) || null }
  },

  // Site analysis (in-portal, kept as history)
  siteAnalyses: (signal?: AbortSignal) =>
    request<import('./types').SiteAnalysisList>('/analyses', { signal }),
  siteAnalysis: (id: string, signal?: AbortSignal) =>
    request<import('./types').SiteAnalysisDetail>(`/analyses/${seg(id)}`, { signal }),
  startSiteAnalysis: (body: { website_url: string; business_url: string; llm_connector: string }) =>
    request<import('./types').SiteAnalysisDetail>('/analyses', { method: 'POST', body }),
  deleteSiteAnalysis: (id: string) => request<{ ok: boolean }>(`/analyses/${seg(id)}`, { method: 'DELETE' }),
  downloadSiteAnalysis: (id: string, format: 'pdf' | 'html') => requestBlob(`/analyses/${seg(id)}/download?format=${format}`),

  // Audit history (dated snapshots, trends, what changed)
  history: (kind: import('./types').HistoryKind, signal?: AbortSignal) =>
    request<import('./types').HistoryList>(`/history/${seg(kind)}`, { signal }),
  historyEntry: <T = import('./types').SnapshotDetail>(kind: import('./types').HistoryKind, id: string, signal?: AbortSignal) =>
    request<T>(`/history/${seg(kind)}/${seg(id)}`, { signal }),
  restoreBrandVersion: (id: string) =>
    request<import('./types').BrandProfileState>(`/history/brand_profile/${seg(id)}/restore`, { method: 'POST' }),

  // Portal Admin (platform operators)
  portalOverview: (signal?: AbortSignal) =>
    request<import('./types').PortalOverview>('/portal/overview', { signal }),
  portalAccounts: (signal?: AbortSignal) =>
    request<import('./types').PortalAccount[]>('/portal/accounts', { signal }),
  portalOrganizations: (signal?: AbortSignal) =>
    request<import('./types').PortalOrganization[]>('/portal/organizations', {
      signal,
    }),
  portalOrganizationUsers: (orgId: string, signal?: AbortSignal) =>
    request<import('./types').PortalUser[]>(
      `/portal/organizations/${seg(orgId)}/users`,
      { signal },
    ),
  portalAnalyses: (params: { source?: string; status?: string; q?: string }, signal?: AbortSignal) =>
    request<import('./types').PortalAnalyses>(
      `/portal/analyses?${new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][]).toString()}`,
      { signal },
    ),
  downloadPortalAnalysis: (row: { source: string; id: string; workspace_id: string | null }, format: 'pdf' | 'html') =>
    requestBlob(
      row.source === 'website'
        ? `/portal/analyses/website/${seg(row.id)}/download?format=${format}`
        : `/portal/analyses/workspace/${seg(row.workspace_id)}/${seg(row.id)}/download?format=${format}`,
    ),
  portalAnalysis: (row: { source: string; id: string; workspace_id: string | null }, signal?: AbortSignal) =>
    request<import('./types').SiteAnalysisDetail & { source: string; workspace?: string }>(
      row.source === 'website' ? `/portal/analyses/website/${seg(row.id)}` : `/portal/analyses/workspace/${seg(row.workspace_id)}/${seg(row.id)}`,
      { signal },
    ),
  portalSignInProviders: (signal?: AbortSignal) =>
    request<import('./types').SignInProvider[]>('/portal/sign-in-providers', { signal }),
  savePortalSignInProvider: (name: string, values: Record<string, string>) =>
    request<import('./types').SignInProviderSaved>(`/portal/sign-in-providers/${seg(name)}`, {
      method: 'PUT',
      body: { values },
    }),
  checkPortalSignInProvider: (name: string) =>
    request<import('./types').SignInCheck>(`/portal/sign-in-providers/${seg(name)}/check`, {
      method: 'POST',
    }),
  clearPortalSignInProvider: (name: string) =>
    request<import('./types').SignInProviderSaved>(`/portal/sign-in-providers/${seg(name)}`, {
      method: 'DELETE',
    }),
  portalVerification: (signal?: AbortSignal) =>
    request<import('./types').VerificationList>('/portal/verification', { signal }),
  portalVerificationWorkspaces: (slug: string, signal?: AbortSignal) =>
    request<import('./types').VerificationWorkspace[]>(
      `/portal/verification/${seg(slug)}/workspaces`,
      { signal },
    ),
  runPortalVerification: (
    slug: string,
    body: {
      workspace_id: string
      allow_writes: boolean
      environment: string
      /** Run-only verify* options; never saved. */
      options?: Record<string, string>
    },
  ) =>
    request<import('./types').VerificationRunStarted>(
      `/portal/verification/${seg(slug)}/run`,
      { method: 'POST', body },
    ),
  savePortalVerificationSettings: (hide_unverified_connectors: boolean) =>
    request<import('./types').VerificationSettings>('/portal/verification/settings', {
      method: 'PUT',
      body: { hide_unverified_connectors },
    }),
  portalFeatures: (signal?: AbortSignal) =>
    request<import('./types').PortalFeature[]>('/portal/features', { signal }),
  setPortalFeature: (kind: string, slug: string, enabled: boolean) =>
    request<ActionResult>(`/portal/features/${seg(kind)}/${seg(slug)}`, {
      method: 'PUT',
      body: { enabled },
    }),

  // SEO Assistant
  assistantChat: (body: import('./types').AssistantChatRequest) =>
    request<import('./types').AssistantChatResponse>('/assistant/chat', {
      method: 'POST',
      body,
    }),

  /**
   * Stream Willy's reply over SSE (delta events + final structured payload).
   * Uses fetch (not EventSource) so Bearer auth works.
   */
  assistantChatStream: async (
    body: import('./types').AssistantChatRequest,
    handlers: {
      onDelta?: (text: string) => void
      onStatus?: (text: string) => void
      onFinal?: (payload: import('./types').AssistantChatResponse) => void
      onError?: (detail: string) => void
    } = {},
    signal?: AbortSignal,
  ): Promise<import('./types').AssistantChatResponse> => {
    const send = async (): Promise<Response> => {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      }
      const token = tokenStore.access
      if (token) headers.Authorization = `Bearer ${token}`
      return fetch(`${BASE_URL}/assistant/chat/stream`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal,
      })
    }

    let response = await send()
    if (response.status === 401) {
      const refreshed = await refreshSession()
      if (refreshed) {
        response = await send()
      } else {
        tokenStore.clear()
        onSessionLost?.()
        throw await parseError(response)
      }
    }
    if (!response.ok) throw await parseError(response)
    if (!response.body) {
      throw new ApiError(502, 'error', 'Streaming response had no body')
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let finalPayload: import('./types').AssistantChatResponse | null = null
    let streamError: string | null = null

    const flushBlock = (block: string) => {
      const lines = block.split(/\r?\n/)
      let eventName = 'message'
      const dataLines: string[] = []
      for (const line of lines) {
        if (line.startsWith('event:')) eventName = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      }
      if (!dataLines.length) return
      let parsed: Record<string, unknown> = {}
      try {
        parsed = JSON.parse(dataLines.join('\n')) as Record<string, unknown>
      } catch {
        return
      }
      if (eventName === 'delta') {
        const text = String(parsed.text ?? '')
        if (text) handlers.onDelta?.(text)
      } else if (eventName === 'status') {
        handlers.onStatus?.(String(parsed.text ?? ''))
      } else if (eventName === 'final') {
        finalPayload = parsed as unknown as import('./types').AssistantChatResponse
        handlers.onFinal?.(finalPayload)
      } else if (eventName === 'error') {
        streamError = String(parsed.detail ?? 'Streaming failed')
        handlers.onError?.(streamError)
      }
    }

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let sep = buffer.indexOf('\n\n')
      while (sep >= 0) {
        const block = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        if (block.trim()) flushBlock(block)
        sep = buffer.indexOf('\n\n')
      }
    }
    if (buffer.trim()) flushBlock(buffer)

    if (streamError) {
      throw new ApiError(502, 'error', streamError)
    }
    if (!finalPayload) {
      throw new ApiError(502, 'error', 'Willy stream ended without a final reply')
    }
    return finalPayload
  },

  // Meta
  health: () => fetch('/health').then((r) => r.json() as Promise<HealthStatus>),
}

/**
 * Spending-agent safety: practice mode, limits and the stop switches.
 * Kept apart from `api` so these shapes sit together.
 */
export const safetyApi = {
  status: (signal?: AbortSignal) =>
    request<import('./types').SafetyStatus>('/safety', { signal }),
  saveCaps: (caps: Record<string, number>) =>
    request<import('./types').SafetyAction>('/safety/caps', { method: 'PUT', body: { caps } }),
  setPractice: (on: boolean) =>
    request<import('./types').SafetyAction>('/safety/practice', { method: 'POST', body: { on } }),
  stopAll: () => request<import('./types').SafetyAction>('/safety/stop', { method: 'POST' }),
  resumeAll: () => request<import('./types').SafetyAction>('/safety/resume', { method: 'POST' }),
  setAgentPractice: (slug: string, on: boolean) =>
    request<AgentOut>(`/agents/${seg(slug)}/practice`, { method: 'POST', body: { on } }),
  practiceSummary: (slug: string, signal?: AbortSignal) =>
    request<import('./types').PracticeSummary>(`/agents/${seg(slug)}/practice-summary`, { signal }),
  practised: (agent = '', signal?: AbortSignal) =>
    request<ApprovalOut[]>(
      `/approvals/practice${agent ? `?agent=${encodeURIComponent(agent)}` : ''}`,
      { signal },
    ),
  platform: (signal?: AbortSignal) =>
    request<import('./types').PlatformSafety>('/safety/platform', { signal }),
  setEmergencyStop: (on: boolean) =>
    request<import('./types').PlatformSafetyAction>('/safety/platform/emergency-stop', {
      method: 'POST',
      body: { on },
    }),
  setPlatformPractice: (on: boolean) =>
    request<import('./types').PlatformSafetyAction>('/safety/platform/practice', {
      method: 'POST',
      body: { on },
    }),
}
