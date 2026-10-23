/**
 * Lightweight API stub for UI style audits (no Postgres).
 * Run: node scripts/mock-api.mjs
 * Then: npm run dev (Vite proxies /api → :8000)
 */
import http from 'node:http'

const FULL = {
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
}

const session = {
  user: {
    id: 'u-audit',
    name: 'Style Auditor',
    email: 'auditor@synaprank.test',
    role: 'admin',
    role_label: 'Admin',
    is_owner: true,
    email_verified: true,
    last_login_at: new Date().toISOString(),
  },
  organization: {
    id: 'org-audit',
    name: 'Synaprank Demo',
    slug: 'synaprank-demo',
    primary_domain: 'synaprank.test',
    global_autonomy: false,
    plan_name: 'Growth',
    seats_total: 10,
    seats_used: 3,
    renews_on: null,
  },
  access: FULL,
  enabled_modules: Object.keys(FULL),
  is_portal_admin: false,
  onboarding_complete: true,
  pending_approvals: 0,
  running_agents: 2,
  total_agents: 20,
  connected_connectors: 4,
  total_connectors: 12,
  nav_counts: {},
  workspaces: [
    {
      id: 'org-audit',
      name: 'Synaprank Demo',
      slug: 'synaprank-demo',
      primary_domain: 'synaprank.test',
      role: 'admin',
      role_label: 'Admin',
      onboarding_complete: true,
      is_current: true,
      is_owner: true,
    },
  ],
}

const tokens = {
  access_token: 'audit-access',
  refresh_token: 'audit-refresh',
  token_type: 'bearer',
  expires_in: 3600,
}

const visibility = {
  queries: [
    {
      id: 'q1',
      question: 'What is the best CRM for mid-market SaaS?',
      label: 'Best CRM',
      active: true,
      tags: [],
      created_at: new Date().toISOString(),
    },
    {
      id: 'q2',
      question: 'How does Synaprank compare to traditional SEO agencies?',
      label: 'Agency comparison',
      active: true,
      tags: [],
      created_at: new Date().toISOString(),
    },
  ],
  competitors: ['rival.example', 'other-brand.com'],
  recent: [
    {
      id: 'o1',
      query_id: 'q1',
      question: 'What is the best CRM for mid-market SaaS?',
      engine: 'chatgpt',
      brand_cited: true,
      brand_position: 2,
      cited_domains: ['synaprank.test', 'rival.example'],
      competitors_cited: ['rival.example'],
      answer_excerpt: 'Several CRMs…',
      observed_at: new Date().toISOString(),
    },
    {
      id: 'o2',
      query_id: 'q2',
      question: 'How does Synaprank compare to traditional SEO agencies?',
      engine: 'perplexity',
      brand_cited: false,
      brand_position: null,
      cited_domains: ['other-brand.com'],
      competitors_cited: ['other-brand.com'],
      answer_excerpt: 'Agencies typically…',
      observed_at: new Date().toISOString(),
    },
  ],
  summary: {
    probes: 12,
    brand_citations: 5,
    brand_share: 0.42,
    by_engine: [
      { engine: 'chatgpt', probes: 4, brand_citations: 2, share: 0.5 },
      { engine: 'perplexity', probes: 4, brand_citations: 1, share: 0.25 },
      { engine: 'gemini', probes: 4, brand_citations: 2, share: 0.5 },
    ],
    top_competitors: [
      { domain: 'rival.example', sightings: 4 },
      { domain: 'other-brand.com', sightings: 3 },
    ],
    read_only: false,
  },
  read_only: false,
}

const topics = {
  clusters: [
    {
      id: 'c1',
      name: 'Programmatic SEO',
      seed_terms: ['template pages', 'location pages'],
      gap_score: 0.62,
      page_count: 8,
      status: 'active',
      page_ids: [],
    },
  ],
  briefs: [
    {
      id: 'b1',
      cluster_id: 'c1',
      title: 'City landing page brief',
      target_keywords: ['seo agency {city}'],
      status: 'draft',
      body: 'Outline…',
    },
  ],
  read_only: false,
}

const alerts = {
  rules: [
    {
      id: 'r1',
      name: 'Citation share drop',
      metric: 'ai_brand_share',
      threshold: 0.3,
      channel: 'email',
      webhook_url: '',
      enabled: 'yes',
    },
  ],
  events: [
    {
      id: 'e1',
      rule_id: 'r1',
      metric: 'ai_brand_share',
      summary: 'Brand share fell 18% week over week',
      status: 'open',
      dedupe_key: 'share-drop-1',
    },
  ],
  read_only: false,
}

function json(res, status, body) {
  const raw = JSON.stringify(body)
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(raw),
  })
  res.end(raw)
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8')
      if (!raw) return resolve(null)
      try {
        resolve(JSON.parse(raw))
      } catch {
        resolve(null)
      }
    })
  })
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', 'http://127.0.0.1')
  const path = url.pathname.replace(/^\/api\/v1/, '') || '/'
  const method = req.method || 'GET'
  await readBody(req)

  if (path === '/health' || url.pathname === '/health') {
    return json(res, 200, { status: 'ok' })
  }
  if (path === '/status') {
    return json(res, 200, {
      status: 'operational',
      checked_at: new Date().toISOString(),
      components: [
        { name: 'api', status: 'operational' },
        { name: 'database', status: 'operational' },
        { name: 'agent_runner', status: 'operational' },
      ],
    })
  }
  if (path === '/auth/me' || path === '/auth/login') {
    return json(res, 200, method === 'GET' ? session : { tokens, session })
  }
  if (path === '/auth/request-code') {
    return json(res, 200, {
      detail: 'If that address has an account, we sent a code.',
      expires_in: 600,
      resend_in: 30,
    })
  }
  if (path === '/auth/policy' || path === '/policy') {
    return json(res, 200, {
      public_signup: true,
      sso_providers: [],
      auth_method: 'email_code',
      code_length: 6,
      code_expires_in: 600,
      resend_in: 30,
      work_email_required: false,
      roles: [{ value: 'admin', label: 'Admin' }],
    })
  }
  if (path === '/visibility') return json(res, 200, visibility)
  if (path.startsWith('/visibility/')) return json(res, 200, { ok: true, toast: null })
  if (path === '/topics') return json(res, 200, topics)
  if (path === '/alerts') return json(res, 200, alerts)
  if (path.startsWith('/alerts/')) return json(res, 200, { ok: true, toast: { message: 'Saved', kind: 'success' } })
  if (path === '/enterprise/sso') return json(res, 200, { connections: [] })
  if (path === '/enterprise/roles') return json(res, 200, { roles: [] })
  if (path === '/enterprise/ip-allowlist') {
    return json(res, 200, { enabled: false, entries: [] })
  }
  if (path === '/billing/catalog') {
    return json(res, 200, {
      plans: [{ slug: 'trial', name: 'Trial' }, { slug: 'growth', name: 'Growth' }],
      provider: 'fake',
    })
  }
  if (path === '/billing/account') {
    return json(res, 200, {
      account: {
        id: 'bill-1',
        provider: 'fake',
        status: 'active',
        plan_slug: 'growth',
        seats_purchased: 10,
        current_period_end: null,
        external_customer_id: null,
      },
    })
  }
  if (path === '/billing/entitlements') {
    return json(res, 200, { plan_slug: 'growth', entitlements: {}, synced_at: null })
  }
  if (path === '/billing/usage') return json(res, 200, { meters: [] })
  if (path === '/workflow/api-tokens') return json(res, 200, { tokens: [] })
  if (path === '/workflow/webhooks') return json(res, 200, { webhooks: [] })
  if (path === '/warehouse/exports') return json(res, 200, { jobs: [] })
  if (path === '/agency' && method === 'POST') {
    return json(res, 200, { id: 'agency-1', slug: 'demo', name: 'Demo Agency' })
  }
  if (path === '/admin') {
    return json(res, 200, {
      members: [],
      invitations: [],
      roles: [{ value: 'admin', label: 'Admin' }],
      billing: {
        plan_name: 'Growth',
        seats_total: 10,
        seats_used: 3,
        renews_on: null,
      },
      audit: [],
      policy: { require_approval: true },
      read_only: false,
    })
  }
  if (path === '/dashboard' || path === '/insights/dashboard') {
    return json(res, 200, { cards: [], setup: [] })
  }
  if (path === '/agents') return json(res, 200, { agents: [], read_only: false })
  if (path === '/approvals') return json(res, 200, [])
  if (path === '/seo') return json(res, 200, { pages: [], total: 0, referral_spam: [], read_only: false })
  if (path === '/reports') return json(res, 200, {})
  if (path === '/mfa/totp/begin') {
    return json(res, 200, {
      secret: 'JBSWY3DPEHPK3PXP',
      otpauth_url: 'otpauth://totp/Synaprank:auditor@synaprank.test?secret=JBSWY3DPEHPK3PXP&issuer=Synaprank',
      recovery_codes: ['AAAA-BBBB-CCCC', 'DDDD-EEEE-FFFF', 'GGGG-HHHH-IIII'],
    })
  }
  if (path === '/mfa/totp/confirm') return json(res, 200, { mfa_enabled: true })
  if (path === '/mfa/verify') return json(res, 200, { tokens, session })

  // Default empty success for GETs; 404 for unknown writes
  if (method === 'GET') return json(res, 200, {})
  return json(res, 200, { ok: true, toast: null })
})

server.listen(8000, '127.0.0.1', () => {
  console.log('Mock API on http://127.0.0.1:8000 (proxied as /api/v1)')
})
