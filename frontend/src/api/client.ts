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
  ApprovalOut,
  AuthResponse,
  CodeChallenge,
  ConnectorOut,
  DashboardOut,
  HealthStatus,
  InvitationOut,
  InvitationPreview,
  Message,
  NotificationsOut,
  OffPageWorkspace,
  OnboardingOut,
  RegistrationPolicy,
  ReportsOut,
  Role,
  SeoAuditOut,
  SeoPageDetail,
  SeoWorkspace,
  SessionOut,
  Tokens,
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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly fields: Record<string, string> = {},
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

/** Called when the session is unrecoverable, so the app can route to login. */
let onSessionLost: (() => void) | null = null
export function setSessionLostHandler(handler: () => void): void {
  onSessionLost = handler
}

// ── Core request ───────────────────────────────────────────────────────────
type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

interface RequestOptions {
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

async function parseError(response: Response): Promise<ApiError> {
  let code = 'error'
  let detail = response.statusText || 'Request failed'
  let fields: Record<string, string> = {}
  try {
    const body = await response.json()
    code = body.code ?? code
    detail = body.detail ?? detail
    fields = body.fields ?? {}
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(response.status, code, detail, fields)
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
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

// ── Endpoints ──────────────────────────────────────────────────────────────
export const api = {
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
  dashboard: () => request<DashboardOut>('/dashboard'),
  reports: (range: '7d' | '30d' | '90d') => request<ReportsOut>(`/reports?range=${range}`),

  // Onboarding
  onboarding: () => request<OnboardingOut>('/onboarding'),
  saveOnboarding: (payload: Partial<{
    step: number
    domain: string
    cms: string
    guardrail: string
  }>) => request<OnboardingOut>('/onboarding', { method: 'PUT', body: payload }),
  completeOnboarding: () =>
    request<ActionResult>('/onboarding/complete', { method: 'POST' }),

  // Agents
  agents: () => request<AgentOut[]>('/agents'),
  agent: (slug: string) => request<AgentOut>(`/agents/${slug}`),
  agentOptions: () => request<AgentOptions>('/agents/options'),
  agentRuns: (slug: string) => request<AgentRun[]>(`/agents/${slug}/runs`),
  pauseAgent: (slug: string) =>
    request<ActionResult>(`/agents/${slug}/pause`, { method: 'POST' }),
  resumeAgent: (slug: string) =>
    request<ActionResult>(`/agents/${slug}/resume`, { method: 'POST' }),
  setAgentAutonomy: (slug: string, autonomous: boolean) =>
    request<ActionResult>(`/agents/${slug}/autonomy`, {
      method: 'POST',
      body: { autonomous },
    }),
  setGlobalAutonomy: (autonomous: boolean) =>
    request<ActionResult>('/agents/autonomy', { method: 'POST', body: { autonomous } }),
  configureAgent: (slug: string, config: AgentConfigRequest) =>
    request<AgentOut>(`/agents/${slug}/config`, { method: 'PUT', body: config }),
  /** Remove an agent's configuration and stop it. */
  resetAgentConfig: (slug: string) =>
    request<AgentOut>(`/agents/${slug}/config`, { method: 'DELETE' }),
  runAgent: (slug: string) =>
    request<ActionResult>(`/agents/${slug}/run`, { method: 'POST' }),

  // SEO
  seo: (search = '') =>
    request<SeoWorkspace>(`/seo${search ? `?search=${encodeURIComponent(search)}` : ''}`),
  seoPage: (id: string) => request<SeoPageDetail>(`/seo/pages/${id}`),

  /** The Technical SEO screen: findings, counts and what to do. */
  seoAudit: (params: { status?: string; kind?: string; severity?: string } = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value) query.set(key, value)
    }
    const suffix = query.toString()
    return request<SeoAuditOut>(`/seo/audit${suffix ? `?${suffix}` : ''}`)
  },
  ignoreIssue: (id: string, note: string) =>
    request<ActionResult>(`/seo/audit/${id}/ignore`, { method: 'POST', body: { note } }),
  reopenIssue: (id: string) =>
    request<ActionResult>(`/seo/audit/${id}/reopen`, { method: 'POST' }),
  requestPageApproval: (id: string) =>
    request<ActionResult>(`/seo/pages/${id}/request-approval`, { method: 'POST' }),

  // Off-page
  offpage: () => request<OffPageWorkspace>('/offpage'),
  launchOutreach: (targetId: string) =>
    request<ActionResult>(`/offpage/targets/${targetId}/outreach`, { method: 'POST' }),
  launchCounterPitch: (alertId: string) =>
    request<ActionResult>(`/offpage/alerts/${alertId}/counter-pitch`, { method: 'POST' }),

  // Ads
  ads: () => request<AdsWorkspace>('/ads'),
  setBudget: (channel: string, percent: number) =>
    request<AdsWorkspace>('/ads/budget', { method: 'PUT', body: { channel, percent } }),
  unlockChannel: (channel: string) =>
    request<ActionResult>(`/ads/budget/${channel}/unlock`, { method: 'POST' }),
  rebalanceBudget: () =>
    request<ActionResult>('/ads/budget/rebalance', { method: 'POST' }),
  generateCreatives: () =>
    request<ActionResult>('/ads/creatives/generate', { method: 'POST' }),

  // Connectors
  connectors: (category = '') =>
    request<ConnectorOut[]>(
      `/connectors${category && category !== 'All' ? `?category=${encodeURIComponent(category)}` : ''}`,
    ),
  connectorCategories: () => request<string[]>('/connectors/categories'),
  connect: (slug: string, values: Record<string, string>, oauth_completed = false) =>
    request<ConnectorOut>(`/connectors/${slug}/connect`, {
      method: 'POST',
      body: { values, oauth_completed },
    }),
  disconnect: (slug: string) =>
    request<ConnectorOut>(`/connectors/${slug}/disconnect`, { method: 'POST' }),
  // Approvals
  approvals: () => request<ApprovalOut[]>('/approvals'),
  approvalCount: () => request<{ pending: number }>('/approvals/count'),
  approval: (id: string) => request<ApprovalDetail>(`/approvals/${id}`),
  approve: (id: string, note = '') =>
    request<ActionResult>(`/approvals/${id}/approve`, { method: 'POST', body: { note } }),
  reject: (id: string, note = '') =>
    request<ActionResult>(`/approvals/${id}/reject`, { method: 'POST', body: { note } }),

  // Admin
  admin: (search = '') =>
    request<AdminOut>(`/admin${search ? `?search=${encodeURIComponent(search)}` : ''}`),
  auditLog: (search = '') =>
    request<import('./types').AuditEntryOut[]>(
      `/admin/audit${search ? `?search=${encodeURIComponent(search)}` : ''}`,
    ),
  changeMemberRole: (id: string, role: Role) =>
    request<import('./types').TeamMemberOut>(`/admin/team/${id}/role`, {
      method: 'PUT',
      body: { role },
    }),
  deactivateMember: (id: string) =>
    request<ActionResult>(`/admin/team/${id}/deactivate`, { method: 'POST' }),
  invite: (email: string, role: Role) =>
    request<InvitationOut>('/admin/invitations', { method: 'POST', body: { email, role } }),
  revokeInvitation: (id: string) =>
    request<ActionResult>(`/admin/invitations/${id}`, { method: 'DELETE' }),

  // Portal Admin (platform operators)
  portalOverview: () => request<import('./types').PortalOverview>('/portal/overview'),
  portalOrganizations: () =>
    request<import('./types').PortalOrganization[]>('/portal/organizations'),
  portalOrganizationUsers: (orgId: string) =>
    request<import('./types').PortalUser[]>(`/portal/organizations/${orgId}/users`),
  portalFeatures: () => request<import('./types').PortalFeature[]>('/portal/features'),
  setPortalFeature: (kind: string, slug: string, enabled: boolean) =>
    request<ActionResult>(`/portal/features/${encodeURIComponent(kind)}/${encodeURIComponent(slug)}`, {
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
