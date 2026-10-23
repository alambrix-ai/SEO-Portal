/**
 * The authenticated shell: sidebar, header, and the page outlet.
 *
 * The navigation is built from the session's access map, so a role that cannot
 * see a module never gets a link to it. Links are grouped by job (optimise,
 * automate, measure, administer) rather than listed flat, and setup only
 * appears in the navigation while the workspace still needs it.
 */
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import type { ModuleKey, SessionOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { ActivityIndicator } from '@/components/ActivityIndicator'
import { AssistantDock } from '@/components/AssistantDock'
import { BRAND_NAME, BrandLogo } from '@/components/BrandLogo'
import { Menu } from '@/components/Menu'
import { NotificationBell } from '@/components/NotificationBell'
import { OAuthReturn } from '@/components/OAuthReturn'
import { SafetyBanner } from '@/components/SafetyBits'
import { useToasts } from '@/components/Toasts'
import { useFitDensity } from '@/hooks/useFitDensity'
import {
  AdminIcon,
  AdsIcon,
  AgentsIcon,
  ApprovalsIcon,
  BrandKitIcon,
  ChevronDownIcon,
  ConnectorsIcon,
  CreativeIcon,
  DashboardIcon,
  MenuIcon,
  OffPageIcon,
  OnboardingIcon,
  PortalIcon,
  ReportsIcon,
  SeoIcon,
  SidebarIcon,
  TechnicalIcon,
} from '@/components/icons'

const RAIL_STORAGE_KEY = 'synaprank.sidebar.rail'

function readRailCollapsed(): boolean {
  try {
    return window.localStorage.getItem(RAIL_STORAGE_KEY) === 'stowed'
  } catch {
    return false
  }
}

interface NavEntry {
  to: string
  label: string
  module: ModuleKey | null
  icon: typeof DashboardIcon
  /** Only shown to PLATFORM portal admins (PORTAL_ADMIN_EMAILS). */
  portalAdminOnly?: boolean
}

interface NavGroup {
  label: string | null
  items: NavEntry[]
}

const NAV_GROUPS: NavGroup[] = [
  {
    label: null,
    items: [
      { to: '/dashboard', label: 'Overview', module: 'dashboard', icon: DashboardIcon },
      { to: '/approvals', label: 'Approvals', module: 'approvals', icon: ApprovalsIcon },
    ],
  },
  {
    label: 'Content',
    items: [
      { to: '/content', label: 'Content library', module: 'content', icon: SeoIcon },
      { to: '/creative', label: 'Creative Studio', module: 'content', icon: CreativeIcon },
      { to: '/brand-kit', label: 'Brand kit', module: 'brand_kit', icon: BrandKitIcon },
      { to: '/brand', label: 'Brand profile', module: 'content', icon: OnboardingIcon },
    ],
  },
  {
    label: 'Optimization',
    items: [
      { to: '/analysis', label: 'Site analysis', module: 'seo', icon: TechnicalIcon },
      { to: '/seo', label: 'SEO & AEO', module: 'seo', icon: SeoIcon },
      { to: '/visibility', label: 'AI Visibility', module: 'seo', icon: SeoIcon },
      { to: '/topics', label: 'Topic clusters', module: 'seo', icon: SeoIcon },
      { to: '/alerts', label: 'Anomaly alerts', module: 'seo', icon: TechnicalIcon },
      { to: '/technical', label: 'Technical SEO', module: 'seo', icon: TechnicalIcon },
      { to: '/offpage', label: 'Off-Page & PR', module: 'offpage', icon: OffPageIcon },
      { to: '/ads', label: 'Paid Media', module: 'ads', icon: AdsIcon },
    ],
  },
  {
    label: 'Automation',
    items: [
      { to: '/agents', label: 'Agents', module: 'agents', icon: AgentsIcon },
      { to: '/connectors', label: 'Integrations', module: 'connectors', icon: ConnectorsIcon },
    ],
  },
  {
    label: 'Insights',
    items: [{ to: '/reports', label: 'Reports', module: 'reports', icon: ReportsIcon }],
  },
  {
    label: 'Administration',
    items: [
      { to: '/admin', label: 'Team & Access', module: 'admin', icon: AdminIcon },
      { to: '/admin/enterprise', label: 'Enterprise', module: 'admin', icon: AdminIcon },
      { to: '/admin/billing', label: 'Billing', module: 'admin', icon: AdminIcon },
      { to: '/admin/developer', label: 'API & webhooks', module: 'admin', icon: ConnectorsIcon },
      { to: '/admin/agency', label: 'Agency', module: 'admin', icon: AdminIcon },
      { to: '/admin/warehouse', label: 'Warehouse exports', module: 'admin', icon: ReportsIcon },
    ],
  },
  {
    label: 'Synaprank',
    items: [
      {
        to: '/portal',
        label: 'Operator console',
        module: null,
        icon: PortalIcon,
        portalAdminOnly: true,
      },
    ],
  },
]

/** Section and page for the header breadcrumb. */
const TITLES: Record<string, [string | null, string]> = {
  '/dashboard': [null, 'Overview'],
  '/onboarding': ['Workspace', 'Setup'],
  '/agents': ['Automation', 'Agents'],
  '/analysis': ['Optimization', 'Site analysis'],
  '/seo': ['Optimization', 'SEO & AEO'],
  '/visibility': ['Optimization', 'AI Visibility'],
  '/topics': ['Optimization', 'Topic clusters'],
  '/alerts': ['Optimization', 'Anomaly alerts'],
  '/technical': ['Optimization', 'Technical SEO'],
  '/offpage': ['Optimization', 'Off-Page & PR'],
  '/ads': ['Optimization', 'Paid Media'],
  '/connectors': ['Automation', 'Integrations'],
  '/approvals': [null, 'Approvals'],
  '/reports': ['Insights', 'Reports'],
  '/admin': ['Administration', 'Team & Access'],
  '/admin/enterprise': ['Administration', 'Enterprise'],
  '/admin/billing': ['Administration', 'Billing'],
  '/admin/developer': ['Administration', 'API & webhooks'],
  '/admin/agency': ['Administration', 'Agency'],
  '/admin/warehouse': ['Administration', 'Warehouse exports'],
  '/portal': ['Synaprank', 'Operator console'],
  '/account': ['Settings', 'Account'],
  '/account/mfa': ['Settings', 'Authenticator'],
  '/content': ['Content', 'Content library'],
  '/brand': ['Content', 'Brand profile'],
  '/creative': ['Content', 'Creative Studio'],
  '/brand-kit': ['Content', 'Brand kit'],
  '/status': [null, 'Status'],
}

/** Reachable while setup is unfinished (mirrors ONBOARDING_OPEN_PATHS in App). */
const SETUP_OPEN = new Set(['/onboarding', '/connectors', '/account'])

/** Approvals is the only count that is work assigned to a person. */
const LOUD = new Set(['/approvals'])

const MEANING: Record<string, (n: number) => string> = {
  '/agents': (n) => `${n} ready or running`,
  '/connectors': (n) => `${n} connected`,
  '/approvals': (n) => `${n} awaiting your approval`,
  '/technical': (n) => `${n} open issue${n === 1 ? '' : 's'}`,
  '/seo': (n) => `${n} page${n === 1 ? '' : 's'} with a rewrite pending`,
  '/offpage': (n) => `${n} opportunit${n === 1 ? 'y' : 'ies'} to act on`,
  '/admin': (n) => `${n} invitation${n === 1 ? '' : 's'} not yet accepted`,
}

/** Only counts that ask for action are shown; inventory counts are noise. */
const BADGED = new Set(['/approvals', '/technical'])

function badgeFor(to: string, session: SessionOut) {
  if (!BADGED.has(to)) return null
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

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  const first = parts[0]?.[0] ?? '?'
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? '') : ''
  return (first + last).toUpperCase()
}

export function AppShell() {
  const { session, signOut, canView, patch, switchWorkspace } = useAuth()
  const { push, fromError } = useToasts()
  const location = useLocation()
  const navigate = useNavigate()
  const { ref: shellRef, fit } = useFitDensity<HTMLDivElement>()
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  useEffect(() => {
    setCollapsed(readRailCollapsed())
  }, [])

  // A route change closes the mobile drawer.
  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (fit.widthBand !== 'narrow') setMobileOpen(false)
  }, [fit.widthBand])

  function toggleRail() {
    setCollapsed((current) => {
      const next = !current
      try {
        window.localStorage.setItem(RAIL_STORAGE_KEY, next ? 'stowed' : 'open')
      } catch {
        /* ignore quota / private mode */
      }
      return next
    })
  }

  if (!session) return null

  const narrow = fit.widthBand === 'narrow'
  const railCollapsed = collapsed && !narrow
  const [section, pageTitle] =
    TITLES[location.pathname] ??
    (location.pathname.startsWith('/agents/')
      ? ['Automation', 'Agents']
      : location.pathname.startsWith('/analysis/')
        ? ['Optimization', 'Site analysis']
        : [null, BRAND_NAME])
  const showAssistant = location.pathname !== '/onboarding'
  const workspaces = session.workspaces ?? []
  const setupPending = !session.onboarding_complete && canView('onboarding')

  const onSwitch = async (id: string) => {
    if (id === session.organization.id) return
    try {
      const next = await switchWorkspace(id)
      push(`Switched to ${next.organization.name}`, 'success')
      navigate(next.onboarding_complete ? '/dashboard' : '/onboarding', { replace: true })
    } catch (caught) {
      fromError(caught)
    }
  }

  const renderEntry = ({ to, label, module, icon: Icon, portalAdminOnly }: NavEntry) => {
    if (portalAdminOnly && !session.is_portal_admin) return null
    // Portal-disabled modules leave the product entirely - not a locked link.
    if (module && !session.enabled_modules.includes(module)) return null
    // A screen the role cannot open is not offered at all.
    if (module && !canView(module)) return null
    // Until setup is finished the router sends these back to Setup, so say so
    // up front instead of offering links that bounce.
    if (setupPending && !SETUP_OPEN.has(to)) {
      return (
        <span
          key={to}
          className="nav-item is-disabled"
          aria-disabled="true"
          title="Finish setup to open this"
        >
          <Icon />
          <span className="nav-item-label">{label}</span>
        </span>
      )
    }
    return (
      <NavLink
        key={to}
        to={to}
        className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
        title={railCollapsed ? label : undefined}
      >
        <Icon />
        <span className="nav-item-label">{label}</span>
        {badgeFor(to, session)}
      </NavLink>
    )
  }

  return (
    <div
      ref={shellRef}
      className="app-shell"
      data-fit-w={fit.widthBand}
      data-fit-h={fit.heightBand}
      data-rail={railCollapsed ? 'collapsed' : 'open'}
      data-mobile-nav={mobileOpen ? 'open' : 'closed'}
    >
      {mobileOpen ? (
        <div className="sidebar-scrim" onClick={() => setMobileOpen(false)} />
      ) : null}

      <nav className="sidebar" aria-label="Main navigation">
        <div className="sidebar-top">
          <NavLink to="/dashboard" className="sidebar-brand" aria-label={`${BRAND_NAME} - overview`}>
            <BrandLogo height={railCollapsed ? 16 : 20} compact={railCollapsed} />
          </NavLink>
        </div>

        {!railCollapsed ? (
          <div className="workspace-switcher">
            <Menu
              label="Switch workspace"
              align="start"
              triggerClassName="workspace-trigger"
              trigger={
                <>
                  <span className="workspace-avatar" aria-hidden="true">
                    {initials(session.organization.name)}
                  </span>
                  <span className="workspace-trigger-text">
                    <span className="workspace-trigger-name">{session.organization.name}</span>
                    <span className="workspace-trigger-meta">
                      {session.organization.primary_domain || session.user.role_label}
                    </span>
                  </span>
                  <ChevronDownIcon size={16} />
                </>
              }
              header="Workspaces"
              items={[
                ...workspaces.map((ws) => ({
                  label: ws.name,
                  hint: ws.onboarding_complete ? ws.role_label : 'Setup incomplete',
                  checked: ws.id === session.organization.id,
                  onSelect: () => void onSwitch(ws.id),
                })),
                ...(canView('onboarding')
                  ? (['divider', {
                      label: 'Manage workspaces',
                      onSelect: () => navigate('/onboarding'),
                    }] as const)
                  : []),
              ]}
            />
          </div>
        ) : null}

        <div className="sidebar-nav">
          {setupPending ? (
            <NavLink
              to="/onboarding"
              className={({ isActive }) => `nav-item nav-item-setup${isActive ? ' active' : ''}`}
              title={railCollapsed ? 'Finish setup' : undefined}
            >
              <OnboardingIcon />
              <span className="nav-item-label">Finish setup</span>
            </NavLink>
          ) : null}

          {NAV_GROUPS.map((group, index) => {
            const entries = group.items.map(renderEntry).filter(Boolean)
            if (entries.length === 0) return null
            return (
              <div key={group.label ?? `g${index}`} className="nav-group">
                {group.label && !railCollapsed ? (
                  <div className="nav-group-label">{group.label}</div>
                ) : null}
                {entries}
              </div>
            )
          })}
        </div>

        {!narrow ? (
          <button
            type="button"
            className="sidebar-collapse"
            onClick={toggleRail}
            aria-pressed={railCollapsed}
            aria-label={railCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            title={railCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <SidebarIcon />
            <span className="nav-item-label">Collapse</span>
          </button>
        ) : null}
      </nav>

      <div className="app-main">
        <header className="topbar">
          <button
            type="button"
            className="btn btn-ghost btn-icon topbar-nav-toggle"
            aria-label={mobileOpen ? 'Close navigation' : 'Open navigation'}
            aria-expanded={mobileOpen}
            onClick={() => setMobileOpen((open) => !open)}
          >
            <MenuIcon />
          </button>

          <div className="topbar-crumbs" aria-label="Breadcrumb">
            {section ? (
              <>
                <span className="topbar-crumb">{section}</span>
                <span className="topbar-crumb-sep" aria-hidden="true">/</span>
              </>
            ) : null}
            <span className="topbar-crumb is-current">{pageTitle}</span>
          </div>

          <div className="topbar-spacer" />

          {canView('agents') && session.enabled_modules.includes('agents') ? <ActivityIndicator /> : null}

          <NotificationBell pendingApprovals={session.pending_approvals} />

          <Menu
            label="Account menu"
            triggerClassName="user-trigger"
            trigger={
              <span className="user-avatar" aria-hidden="true">
                {initials(session.user.name)}
              </span>
            }
            header={
              <div className="user-menu-head">
                <div className="user-menu-name">{session.user.name}</div>
                <div className="user-menu-email">{session.user.email}</div>
                <div className="user-menu-role">{session.user.role_label}</div>
              </div>
            }
            items={[
              { label: 'Account settings', onSelect: () => navigate('/account') },
              'divider',
              {
                label: 'Sign out',
                onSelect: () => {
                  void signOut()
                  patch({ pending_approvals: 0 })
                },
              },
            ]}
          />
        </header>

        {/* Stop switches and practice mode, on every screen. */}
        <SafetyBanner />

        <main className="page">
          <Outlet />
        </main>
        {/* Back from "Sign in with Google" or WordPress approval. */}
        <OAuthReturn />
      </div>

      {showAssistant ? <AssistantDock /> : null}
    </div>
  )
}
