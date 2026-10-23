/**
 * Smoke: every new feature route paints non-empty content with no page errors.
 * API calls are mocked so the suite does not need Postgres or a live backend.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

const SESSION = {
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
  access: {
    dashboard: 'full',
    onboarding: 'full',
    agents: 'full',
    seo: 'full',
    offpage: 'full',
    ads: 'full',
    connectors: 'full',
    approvals: 'full',
    reports: 'full',
    admin: 'full',
    content: 'full',
    brand_kit: 'full',
  },
  enabled_modules: [
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
  ],
  onboarding_complete: true,
  pending_approvals: 0,
  running_agents: 0,
  total_agents: 0,
  connected_connectors: 0,
  total_connectors: 0,
  is_portal_admin: false,
  workspaces: [],
  nav_counts: {},
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

type Mode = 'success' | 'empty' | 'error' | 'legacy-catalog'

async function installMocks(page: Page, mode: Mode) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname.replace(/^\/api\/v1/, '') || '/'
    const method = route.request().method()

    // Session restore — must succeed or RequireAuth sends us to /login.
    if (path === '/auth/me' || path.startsWith('/auth/me')) {
      return json(route, SESSION)
    }
    if (method === 'POST' && path.includes('/auth/refresh')) {
      return json(route, {
        tokens: { access_token: 'test-access', refresh_token: 'test-refresh' },
        session: SESSION,
      })
    }

    if (mode === 'error' && !path.startsWith('/auth/')) {
      return json(
        route,
        {
          code: 'feature_not_configured',
          detail: 'This feature is not configured yet',
          fields: {},
        },
        503,
      )
    }

    if (path.includes('/billing/catalog')) {
      if (mode === 'legacy-catalog') {
        return json(route, {
          plans: { trial: { name: 'Trial', price_monthly: 0 } },
          provider: 'fake',
        })
      }
      return json(
        route,
        mode === 'empty'
          ? { plans: [], provider: 'fake' }
          : { plans: [{ slug: 'trial', name: 'Trial' }], provider: 'fake' },
      )
    }
    if (path.includes('/billing/account')) return json(route, { account: null })
    if (path.includes('/billing/usage')) return json(route, { meters: [] })
    if (path.includes('/billing/entitlements')) {
      return json(route, { plan_slug: null, entitlements: {} })
    }
    if (path.includes('/enterprise/sso')) return json(route, { connections: [] })
    if (path.includes('/enterprise/roles')) return json(route, { roles: [] })
    if (path.includes('/enterprise/ip-allowlist')) {
      return json(route, { enabled: false, entries: [] })
    }
    if (path.includes('/workflow/api-tokens')) return json(route, { tokens: [] })
    if (path.includes('/workflow/webhooks')) return json(route, { webhooks: [] })
    if (path.includes('/warehouse/exports')) return json(route, { jobs: [] })
    if (path === '/status' || path.startsWith('/status')) {
      return json(route, {
        status: 'operational',
        checked_at: new Date().toISOString(),
        components: mode === 'empty' ? [] : [{ name: 'api', status: 'operational' }],
      })
    }
    if (path.includes('/safety')) {
      return json(route, {
        platform_stop: false,
        emergency_stop: false,
        practice_mode: false,
        agents: [],
      })
    }
    if (path.includes('/notifications')) return json(route, { items: [], unread: 0 })

    return json(route, {})
  })
}

async function signIn(page: Page) {
  await page.addInitScript(() => {
    // tokenStore reads sessionStorage (survives reload within the tab).
    window.sessionStorage.setItem('automarket.access_token', 'test-access-token')
    window.sessionStorage.setItem('automarket.refresh_token', 'test-refresh-token')
  })
}

const AUTHED_ROUTES: { path: string; text: RegExp }[] = [
  { path: '/admin/billing', text: /Billing|Could not load this|No plans|No billing/i },
  { path: '/admin/enterprise', text: /Enterprise|Could not load this|No SSO/i },
  { path: '/admin/developer', text: /API & webhooks|Could not load this|No tokens/i },
  { path: '/admin/agency', text: /Agency|Create agency/i },
  { path: '/admin/warehouse', text: /Warehouse|Could not load this|No exports/i },
  { path: '/account/mfa', text: /Authenticator|enrolment|Start enrolment/i },
  { path: '/billing', text: /Billing|Could not load this|No plans|No billing/i },
]

test.describe('new feature routes (success)', () => {
  for (const route of AUTHED_ROUTES) {
    test(`${route.path} paints content`, async ({ page }) => {
      const pageErrors: string[] = []
      page.on('pageerror', (err) => pageErrors.push(String(err)))
      await signIn(page)
      await installMocks(page, 'success')
      await page.goto(route.path, { waitUntil: 'networkidle' })
      await expect(page.getByRole('heading', { name: /sign in/i })).toHaveCount(0)
      await expect(page.getByText(route.text).first()).toBeVisible({ timeout: 15_000 })
      expect(pageErrors, pageErrors.join('\n')).toEqual([])
    })
  }

  test('/status is public and non-empty', async ({ page }) => {
    await installMocks(page, 'success')
    await page.goto('/status', { waitUntil: 'networkidle' })
    await expect(page.getByTestId('status-page')).toBeVisible()
    await expect(page.getByText(/Platform status/i)).toBeVisible()
  })

  test('/login/mfa without state redirects without crashing', async ({ page }) => {
    await page.goto('/login/mfa', { waitUntil: 'networkidle' })
    await expect(page.locator('body')).not.toBeEmpty()
    await expect(page).toHaveURL(/\/login/)
  })
})

test.describe('new feature routes (API error / empty)', () => {
  test('billing shows friendly error on 503, not a blank page', async ({ page }) => {
    const pageErrors: string[] = []
    page.on('pageerror', (err) => pageErrors.push(String(err)))
    await signIn(page)
    await installMocks(page, 'error')
    await page.goto('/admin/billing', { waitUntil: 'networkidle' })
    await expect(page.getByRole('heading', { name: /sign in/i })).toHaveCount(0)
    await expect(page.getByText(/Could not load this|not configured|Billing/i).first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByTestId('error-boundary')).toHaveCount(0)
    expect(pageErrors).toEqual([])
  })

  test('legacy dict catalog shape does not blank the page', async ({ page }) => {
    const pageErrors: string[] = []
    page.on('pageerror', (err) => pageErrors.push(String(err)))
    await signIn(page)
    await installMocks(page, 'legacy-catalog')
    await page.goto('/admin/billing', { waitUntil: 'networkidle' })
    await expect(page.getByRole('heading', { name: 'Billing' })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Trial').first()).toBeVisible()
    expect(pageErrors.filter((e) => /plans\.map/i.test(e))).toEqual([])
  })
})
