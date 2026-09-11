/**
 * Routing and route guards.
 *
 * Guards driven by the session the API returned:
 *
 * - `RequireAuth` keeps unauthenticated visitors on the public screens, and
 *   remembers where they were going so signing in lands them there.
 * - `RequireModule` refuses a screen the caller's role cannot view. The API
 *   enforces the same rule; this only avoids rendering a page that would then
 *   fail every request it makes.
 * - `RequireOnboardingDone` sends unfinished workspaces to the wizard before
 *   the dashboard (and other product screens) are useful. Connectors stay
 *   reachable mid-wizard because step two links there.
 */
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'

import type { ModuleKey } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { Loading } from '@/components/ui'
import { AppShell } from '@/layout/AppShell'
import { AcceptInvitePage } from '@/pages/AcceptInvitePage'
import { AccountPage } from '@/pages/AccountPage'
import { AdminPage } from '@/pages/AdminPage'
import { AdsPage } from '@/pages/AdsPage'
import { AgentsPage } from '@/pages/AgentsPage'
import { ApprovalsPage } from '@/pages/ApprovalsPage'
import { ConnectorsPage } from '@/pages/ConnectorsPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { LoginPage } from '@/pages/LoginPage'
import { OffPagePage } from '@/pages/OffPagePage'
import { OnboardingPage } from '@/pages/OnboardingPage'
import { PortalAdminPage } from '@/pages/PortalAdminPage'
import { RegisterPage } from '@/pages/RegisterPage'
import { ReportsPage } from '@/pages/ReportsPage'
import { SeoPage } from '@/pages/SeoPage'
import { TechnicalSeoPage } from '@/pages/TechnicalSeoPage'

/** Paths reachable while the workspace still has unfinished onboarding. */
const ONBOARDING_OPEN_PATHS = new Set([
  '/onboarding',
  '/connectors',
  '/account',
])

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { session, loading } = useAuth()
  const location = useLocation()

  if (loading) return <Loading label="Restoring your session…" />
  if (!session) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <>{children}</>
}

function RequireModule({
  module,
  children,
}: {
  module: ModuleKey
  children: React.ReactNode
}) {
  const { canView } = useAuth()
  if (!canView(module)) return <Navigate to="/dashboard" replace />
  return <>{children}</>
}

/** Unfinished workspaces finish the wizard before the rest of the console. */
function RequireOnboardingDone({ children }: { children: React.ReactNode }) {
  const { session, canView } = useAuth()
  const location = useLocation()

  if (
    session &&
    !session.onboarding_complete &&
    canView('onboarding') &&
    !ONBOARDING_OPEN_PATHS.has(location.pathname)
  ) {
    return <Navigate to="/onboarding" replace />
  }
  return <>{children}</>
}

function PublicOnly({ children }: { children: React.ReactNode }) {
  const { session, loading } = useAuth()
  if (loading) return <Loading label="Loading…" />
  // Fresh workspaces land in the wizard; completed ones on the dashboard.
  if (session) {
    return (
      <Navigate
        to={session.onboarding_complete ? '/dashboard' : '/onboarding'}
        replace
      />
    )
  }
  return <>{children}</>
}

export function App() {
  return (
    <Routes>
      {/* ── Public ─────────────────────────────────────────────────────── */}
      <Route
        path="/login"
        element={
          <PublicOnly>
            <LoginPage />
          </PublicOnly>
        }
      />
      <Route
        path="/register"
        element={
          <PublicOnly>
            <RegisterPage />
          </PublicOnly>
        }
      />
      <Route path="/accept-invite" element={<AcceptInvitePage />} />

      {/* ── Authenticated console ──────────────────────────────────────── */}
      <Route
        element={
          <RequireAuth>
            <RequireOnboardingDone>
              <AppShell />
            </RequireOnboardingDone>
          </RequireAuth>
        }
      >
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route
          path="/onboarding"
          element={
            <RequireModule module="onboarding">
              <OnboardingPage />
            </RequireModule>
          }
        />
        <Route
          path="/agents"
          element={
            <RequireModule module="agents">
              <AgentsPage />
            </RequireModule>
          }
        />
        <Route
          path="/seo"
          element={
            <RequireModule module="seo">
              <SeoPage />
            </RequireModule>
          }
        />
        <Route
          path="/technical"
          element={
            <RequireModule module="seo">
              <TechnicalSeoPage />
            </RequireModule>
          }
        />
        <Route
          path="/offpage"
          element={
            <RequireModule module="offpage">
              <OffPagePage />
            </RequireModule>
          }
        />
        <Route
          path="/ads"
          element={
            <RequireModule module="ads">
              <AdsPage />
            </RequireModule>
          }
        />
        <Route
          path="/connectors"
          element={
            <RequireModule module="connectors">
              <ConnectorsPage />
            </RequireModule>
          }
        />
        <Route
          path="/approvals"
          element={
            <RequireModule module="approvals">
              <ApprovalsPage />
            </RequireModule>
          }
        />
        <Route
          path="/reports"
          element={
            <RequireModule module="reports">
              <ReportsPage />
            </RequireModule>
          }
        />
        <Route
          path="/admin"
          element={
            <RequireModule module="admin">
              <AdminPage />
            </RequireModule>
          }
        />
        <Route path="/portal" element={<PortalAdminPage />} />
        <Route path="/account" element={<AccountPage />} />
      </Route>

      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}
