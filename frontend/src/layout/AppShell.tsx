/**
 * The authenticated shell: sidebar, header, and the page outlet.
 *
 * The navigation is built from the session's access map, so a role that cannot
 * see a module never gets a link to it — and the header's autonomy control and
 * role switcher act on real state rather than local UI toggles.
 */
import { NavLink, Outlet, useLocation } from 'react-router-dom'

import type { ModuleKey, SessionOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { BrandLogo } from '@/components/BrandLogo'
import { NotificationBell } from '@/components/NotificationBell'
import {
  AdminIcon,
  AdsIcon,
  AgentsIcon,
  ApprovalsIcon,
  ConnectorsIcon,
  DashboardIcon,
  OffPageIcon,
  OnboardingIcon,
  ReportsIcon,
  SeoIcon,
  TechnicalIcon,
} from '@/components/icons'

interface NavEntry {
  to: string
  label: string
  module: ModuleKey
  icon: typeof DashboardIcon
}

// Order matches the design's sidebar exactly.
const NAV: NavEntry[] = [
  { to: '/dashboard', label: 'Dashboard', module: 'dashboard', icon: DashboardIcon },
  { to: '/onboarding', label: 'Onboarding', module: 'onboarding', icon: OnboardingIcon },
  { to: '/agents', label: 'AI Agents', module: 'agents', icon: AgentsIcon },
  { to: '/seo', label: 'SEO & AEO', module: 'seo', icon: SeoIcon },
  { to: '/technical', label: 'Technical SEO', module: 'seo', icon: TechnicalIcon },
  { to: '/offpage', label: 'Off-Page & PR', module: 'offpage', icon: OffPageIcon },
  { to: '/ads', label: 'Ads & Programmatic', module: 'ads', icon: AdsIcon },
  { to: '/connectors', label: 'Connectors', module: 'connectors', icon: ConnectorsIcon },
  { to: '/approvals', label: 'Approvals', module: 'approvals', icon: ApprovalsIcon },
  { to: '/reports', label: 'Reports', module: 'reports', icon: ReportsIcon },
  { to: '/admin', label: 'Admin', module: 'admin', icon: AdminIcon },
]

const TITLES: Record<string, string> = {
  '/dashboard': 'Dashboard',
  '/onboarding': 'Onboarding',
  '/agents': 'AI Agents',
  '/seo': 'SEO & AEO',
  '/technical': 'Technical SEO',
  '/offpage': 'Off-Page & PR',
  '/ads': 'Ads & Programmatic',
  '/connectors': 'Connectors',
  '/approvals': 'Approvals',
  '/reports': 'Reports',
  '/admin': 'Admin',
  '/account': 'Account',
}

/**
 * The number on a navigation item, or nothing.
 *
 * The counts come from the server as a route-keyed map, computed in one
 * place and filtered by the access map — so a role that cannot open a screen
 * is not told how much is waiting on it.
 *
 * Approvals is the loud one because it is the only count that is work
 * assigned to a person. The rest are quieter: they say how much is there,
 * not that somebody is blocked.
 */
const LOUD = new Set(['/approvals'])

/** What each number means, for the title attribute. */
const MEANING: Record<string, (n: number) => string> = {
  '/agents': (n) => `${n} running`,
  '/connectors': (n) => `${n} connected`,
  '/approvals': (n) => `${n} awaiting your approval`,
  '/technical': (n) => `${n} open issue${n === 1 ? '' : 's'}`,
  '/seo': (n) => `${n} page${n === 1 ? '' : 's'} with a rewrite pending`,
  '/offpage': (n) => `${n} opportunit${n === 1 ? 'y' : 'ies'} to act on`,
  '/admin': (n) => `${n} invitation${n === 1 ? '' : 's'} not yet accepted`,
}

function badgeFor(to: string, session: SessionOut) {
  const count = session.nav_counts?.[to] ?? 0
  if (count <= 0) return null
  const describe = MEANING[to]
  return (
    <span
      className={`nav-badge${LOUD.has(to) ? '' : ' is-quiet'}`}
      title={describe ? describe(count) : String(count)}
    >
      {count > 99 ? '99+' : count}
    </span>
  )
}

export function AppShell() {
  const { session, signOut, canView, patch } = useAuth()
  const location = useLocation()

  if (!session) return null

  const pageTitle = TITLES[location.pathname] ?? 'AutoMarket AI'

  return (
    <div className="app-shell">
      <nav className="sidebar" aria-label="Main navigation">
        <NavLink to="/dashboard" className="sidebar-brand" aria-label="Alambrix — dashboard">
          <BrandLogo height={24} />
        </NavLink>

        <div className="sidebar-nav">
          {NAV.map(({ to, label, module, icon: Icon }) => {
            const locked = !canView(module)
            if (locked) {
              return (
                <span
                  key={to}
                  className="nav-item locked"
                  title="Restricted for your role"
                  aria-disabled="true"
                >
                  <Icon />
                  <span>{label}</span>
                </span>
              )
            }
            return (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
              >
                <Icon />
                <span>{label}</span>
                {badgeFor(to, session)}
              </NavLink>
            )
          })}
        </div>

        <div className="sidebar-footer">
          Signed in as
          <strong>{session.user.role_label}</strong>
          <div className="small muted" style={{ marginTop: 4 }}>
            {session.organization.name}
          </div>
        </div>
      </nav>

      <div className="app-main">
        <header className="topbar">
          <div className="topbar-title">{pageTitle}</div>

          <div className="topbar-spacer" />

          <NotificationBell pendingApprovals={session.pending_approvals} />

          <NavLink to="/account" className="btn btn-secondary nowrap">
            {session.user.name.split(' ')[0]}
          </NavLink>

          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              void signOut()
              patch({ pending_approvals: 0 })
            }}
          >
            Sign out
          </button>
        </header>

        <main className="page">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
