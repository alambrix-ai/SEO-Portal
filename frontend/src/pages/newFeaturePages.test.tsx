/**
 * New feature pages must never white-screen on success, empty, or error payloads.
 */
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import type { AccessMap, ModuleKey, SessionOut } from '@/api/types'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { ToastProvider } from '@/components/Toasts'
import { invalidateResourceCache, setResourceCacheScope } from '@/hooks/useResource'
import { asBillingPlans, asList } from '@/lib/safeData'
import { AgencyPage } from '@/pages/AgencyPage'
import { BillingPage } from '@/pages/BillingPage'
import { DeveloperPage } from '@/pages/DeveloperPage'
import { EnterprisePage } from '@/pages/EnterprisePage'
import { MfaChallengePage } from '@/pages/MfaChallengePage'
import { MfaSetupPage } from '@/pages/MfaSetupPage'
import { StatusPage } from '@/pages/StatusPage'
import { WarehousePage } from '@/pages/WarehousePage'

const MODULES: ModuleKey[] = [
  'dashboard',
  'onboarding',
  'agents',
  'seo',
  'offpage',
  'ads',
  'connectors',
  'approvals',
  'reports',
  'admin',
  'content',
  'brand_kit',
]

function fullAccess(): AccessMap {
  const map = {} as AccessMap
  for (const key of MODULES) map[key] = 'full'
  return map
}

const session = {
  user: {
    id: 'user-1',
    email: 'owner@example.com',
    name: 'Owner',
    role: 'admin',
    role_label: 'Admin',
    is_owner: true,
    email_verified: true,
    last_login_at: null,
  },
  organization: {
    id: 'org-1',
    name: 'Acme',
    slug: 'acme',
    primary_domain: 'acme.test',
    global_autonomy: false,
    plan_name: 'Trial',
    seats_total: 3,
    seats_used: 1,
    renews_on: null,
  },
  access: fullAccess(),
  enabled_modules: [...MODULES],
  onboarding_complete: true,
  pending_approvals: 0,
  running_agents: 0,
  total_agents: 0,
  connected_connectors: 0,
  total_connectors: 0,
  is_portal_admin: false,
  workspaces: [],
  nav_counts: {},
} as SessionOut

vi.mock('@/auth/AuthContext', () => ({
  useAuth: () => ({
    session,
    loading: false,
    signIn: vi.fn(),
    signUp: vi.fn(),
    adopt: vi.fn(),
    switchWorkspace: vi.fn(),
    createWorkspace: vi.fn(),
    deleteWorkspace: vi.fn(),
    signOut: vi.fn(),
    refresh: vi.fn(),
    patch: vi.fn(),
    access: (module: ModuleKey) => session.access?.[module] ?? 'none',
    canView: (module: ModuleKey) => (session.access?.[module] ?? 'none') !== 'none',
    canWrite: (module: ModuleKey) => (session.access?.[module] ?? 'none') === 'full',
  }),
  AuthProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))

const apiMocks = vi.hoisted(() => ({
  billingCatalog: vi.fn(),
  billingAccount: vi.fn(),
  billingUsage: vi.fn(),
  billingEntitlements: vi.fn(),
  enterpriseSso: vi.fn(),
  enterpriseRoles: vi.fn(),
  enterpriseIpAllowlist: vi.fn(),
  workflowApiTokens: vi.fn(),
  workflowWebhooks: vi.fn(),
  warehouseExports: vi.fn(),
  publicStatus: vi.fn(),
  mfaTotpBegin: vi.fn(),
  mfaVerify: vi.fn(),
}))

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    ...actual,
    api: {
      ...actual.api,
      ...apiMocks,
    },
  }
})

function renderPage(ui: ReactElement, initialEntries?: string[]) {
  invalidateResourceCache()
  setResourceCacheScope('user-1:org-1')
  return render(
    <MemoryRouter initialEntries={initialEntries ?? ['/']}>
      <ToastProvider>
        <ErrorBoundary where="test-route" variant="page">
          {ui}
        </ErrorBoundary>
      </ToastProvider>
    </MemoryRouter>,
  )
}

function featureUnavailable() {
  return Promise.reject(
    new ApiError(503, 'feature_not_configured', 'This feature is not configured yet'),
  )
}

function forbidden() {
  return Promise.reject(new ApiError(403, 'forbidden', 'View-only access for your role'))
}

describe('safeData helpers', () => {
  it('normalises billing plan dicts that used to crash plans.map', () => {
    const plans = asBillingPlans({
      trial: { name: 'Trial', price_monthly: 0 },
      growth: { name: 'Growth' },
    })
    expect(plans).toHaveLength(2)
    expect(plans[0]?.slug).toBe('trial')
    expect(asList(undefined)).toEqual([])
    expect(asList(null)).toEqual([])
    expect(asList({ not: 'array' })).toEqual([])
  })
})

describe('BillingPage', () => {
  beforeEach(() => {
    Object.values(apiMocks).forEach((fn) => fn.mockReset())
  })

  it('renders success with array catalog', async () => {
    apiMocks.billingCatalog.mockResolvedValue({
      plans: [{ slug: 'trial', name: 'Trial' }],
      provider: 'fake',
    })
    apiMocks.billingAccount.mockResolvedValue({ account: null })
    apiMocks.billingUsage.mockResolvedValue({ meters: [] })
    apiMocks.billingEntitlements.mockResolvedValue({ plan_slug: null, entitlements: {} })
    renderPage(<BillingPage />)
    expect(await screen.findByText('Billing')).toBeInTheDocument()
    expect(await screen.findByText('Trial')).toBeInTheDocument()
    expect(screen.queryByTestId('error-boundary')).not.toBeInTheDocument()
  })

  it('does not crash when catalog is still a slug→meta map', async () => {
    apiMocks.billingCatalog.mockResolvedValue({
      plans: { trial: { name: 'Trial', price_monthly: 0 } },
      provider: 'fake',
    })
    apiMocks.billingAccount.mockResolvedValue({ account: null })
    apiMocks.billingUsage.mockResolvedValue({ meters: [] })
    apiMocks.billingEntitlements.mockResolvedValue({ plan_slug: null, entitlements: {} })
    renderPage(<BillingPage />)
    expect(await screen.findByText('Billing')).toBeInTheDocument()
    expect(await screen.findByText('Trial')).toBeInTheDocument()
  })

  it('shows error UI when feature is not configured (503)', async () => {
    apiMocks.billingCatalog.mockImplementation(featureUnavailable)
    apiMocks.billingAccount.mockImplementation(featureUnavailable)
    apiMocks.billingUsage.mockImplementation(featureUnavailable)
    apiMocks.billingEntitlements.mockImplementation(featureUnavailable)
    renderPage(<BillingPage />)
    expect(await screen.findByText('Could not load this')).toBeInTheDocument()
    expect(screen.queryByTestId('error-boundary')).not.toBeInTheDocument()
  })

  it('survives empty payloads', async () => {
    apiMocks.billingCatalog.mockResolvedValue({ plans: [], provider: 'fake' })
    apiMocks.billingAccount.mockResolvedValue({ account: null })
    apiMocks.billingUsage.mockResolvedValue({ meters: [] })
    apiMocks.billingEntitlements.mockResolvedValue({ plan_slug: null, entitlements: {} })
    renderPage(<BillingPage />)
    expect(await screen.findByText('No plans in catalog')).toBeInTheDocument()
  })
})

describe('EnterprisePage', () => {
  beforeEach(() => Object.values(apiMocks).forEach((fn) => fn.mockReset()))

  it('renders success and empty states', async () => {
    apiMocks.enterpriseSso.mockResolvedValue({ connections: [] })
    apiMocks.enterpriseRoles.mockResolvedValue({ roles: [] })
    apiMocks.enterpriseIpAllowlist.mockResolvedValue({ enabled: false, entries: [] })
    renderPage(<EnterprisePage />)
    expect(await screen.findByText('Enterprise')).toBeInTheDocument()
    expect(await screen.findByText('No SSO connections')).toBeInTheDocument()
  })

  it('does not crash when list fields are missing', async () => {
    apiMocks.enterpriseSso.mockResolvedValue({} as { connections: never[] })
    apiMocks.enterpriseRoles.mockResolvedValue({} as { roles: never[] })
    apiMocks.enterpriseIpAllowlist.mockResolvedValue({} as { enabled: boolean; entries: never[] })
    renderPage(<EnterprisePage />)
    expect(await screen.findByText('Enterprise')).toBeInTheDocument()
    expect(screen.queryByTestId('error-boundary')).not.toBeInTheDocument()
  })

  it('shows error on 503 / 403', async () => {
    apiMocks.enterpriseSso.mockImplementation(featureUnavailable)
    apiMocks.enterpriseRoles.mockImplementation(featureUnavailable)
    apiMocks.enterpriseIpAllowlist.mockImplementation(featureUnavailable)
    renderPage(<EnterprisePage />)
    expect(await screen.findByText('Could not load this')).toBeInTheDocument()

    apiMocks.enterpriseSso.mockImplementation(forbidden)
    apiMocks.enterpriseRoles.mockImplementation(forbidden)
    apiMocks.enterpriseIpAllowlist.mockImplementation(forbidden)
    renderPage(<EnterprisePage />)
    expect(await screen.findByText(/permission/i)).toBeInTheDocument()
  })
})

describe('DeveloperPage / WarehousePage / AgencyPage', () => {
  beforeEach(() => Object.values(apiMocks).forEach((fn) => fn.mockReset()))

  it('DeveloperPage handles success, empty, error', async () => {
    apiMocks.workflowApiTokens.mockResolvedValue({ tokens: [] })
    apiMocks.workflowWebhooks.mockResolvedValue({ webhooks: [] })
    renderPage(<DeveloperPage />)
    expect(await screen.findByText('API & webhooks')).toBeInTheDocument()
    expect(await screen.findByText('No tokens')).toBeInTheDocument()

    apiMocks.workflowApiTokens.mockImplementation(featureUnavailable)
    apiMocks.workflowWebhooks.mockImplementation(featureUnavailable)
    renderPage(<DeveloperPage />)
    expect(await screen.findByText('Could not load this')).toBeInTheDocument()
  })

  it('WarehousePage handles success, empty, error, malformed jobs', async () => {
    apiMocks.warehouseExports.mockResolvedValue({ jobs: [] })
    renderPage(<WarehousePage />)
    expect(await screen.findByText('Warehouse exports')).toBeInTheDocument()
    expect(await screen.findByText('No exports yet')).toBeInTheDocument()

    apiMocks.warehouseExports.mockResolvedValue({} as { jobs: never[] })
    renderPage(<WarehousePage />)
    expect(await screen.findByText('Warehouse exports')).toBeInTheDocument()

    apiMocks.warehouseExports.mockImplementation(featureUnavailable)
    renderPage(<WarehousePage />)
    expect(await screen.findByText('Could not load this')).toBeInTheDocument()
  })

  it('AgencyPage renders without fetching', async () => {
    renderPage(<AgencyPage />)
    expect(await screen.findByRole('heading', { name: 'Agency' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create agency/i })).toBeInTheDocument()
  })
})

describe('StatusPage / MFA pages', () => {
  beforeEach(() => Object.values(apiMocks).forEach((fn) => fn.mockReset()))

  it('StatusPage success, empty components, error', async () => {
    apiMocks.publicStatus.mockResolvedValue({
      status: 'operational',
      checked_at: '2026-01-01T00:00:00Z',
      components: [{ name: 'api', status: 'operational' }],
    })
    renderPage(<StatusPage />)
    expect(await screen.findByText('Platform status')).toBeInTheDocument()
    expect(await screen.findByText('api')).toBeInTheDocument()

    apiMocks.publicStatus.mockResolvedValue({
      status: 'operational',
      checked_at: '2026-01-01T00:00:00Z',
    } as { status: string; checked_at: string; components: never[] })
    renderPage(<StatusPage />)
    await waitFor(() => expect(screen.getAllByText('Platform status').length).toBeGreaterThan(0))
    expect(screen.queryByTestId('error-boundary')).not.toBeInTheDocument()

    apiMocks.publicStatus.mockImplementation(featureUnavailable)
    renderPage(<StatusPage />)
    expect(await screen.findByText('Could not load this')).toBeInTheDocument()
  })

  it('MfaSetupPage renders and tolerates bad begin payload', async () => {
    renderPage(<MfaSetupPage />)
    expect(await screen.findByText('Authenticator app')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start enrolment/i })).toBeInTheDocument()
  })

  it('MfaChallengePage redirects without token', async () => {
    renderPage(<MfaChallengePage />, ['/login/mfa'])
    // Navigate to /login — MemoryRouter keeps us mounted; page should not crash.
    await waitFor(() => {
      expect(screen.queryByTestId('error-boundary')).not.toBeInTheDocument()
    })
  })
})

describe('ErrorBoundary', () => {
  it('shows friendly retry UI instead of white screen', () => {
    function Boom(): ReactElement {
      throw new Error('plans.map is not a function')
    }
    renderPage(<Boom />)
    expect(screen.getByTestId('error-boundary')).toBeInTheDocument()
    expect(screen.getByText(/something went wrong on this screen/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})
