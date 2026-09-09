/**
 * Session state and the access map.
 *
 * The access map comes from the API, which reads it from the same RBAC table
 * the endpoints enforce. So the navigation a user sees and the requests they
 * are allowed to make cannot drift apart — the console never decides
 * permissions for itself.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { api, setSessionLostHandler, tokenStore } from '@/api/client'
import type { Access, AuthResponse, ModuleKey, SessionOut } from '@/api/types'

interface AuthState {
  session: SessionOut | null
  loading: boolean
  /** Redeem a mailed code for a session. There is no password to pass. */
  signIn: (email: string, code: string) => Promise<void>
  signUp: (payload: {
    organization_name: string
    full_name: string
    email: string
    code: string
    primary_domain?: string
  }) => Promise<void>
  adopt: (response: AuthResponse) => void
  signOut: () => Promise<void>
  refresh: () => Promise<void>
  /** Optimistically update the cached session (e.g. the approvals badge). */
  patch: (partial: Partial<SessionOut>) => void
  access: (module: ModuleKey) => Access
  canView: (module: ModuleKey) => boolean
  canWrite: (module: ModuleKey) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionOut | null>(null)
  const [loading, setLoading] = useState(true)

  const clear = useCallback(() => {
    tokenStore.clear()
    setSession(null)
  }, [])

  // The client calls this when a refresh fails, so an expired session lands
  // on the login screen rather than a wall of failed requests.
  useEffect(() => {
    setSessionLostHandler(() => setSession(null))
  }, [])

  // Restore on load: a stored token means the tab was reloaded, not closed.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      if (!tokenStore.access) {
        setLoading(false)
        return
      }
      try {
        const restored = await api.me()
        if (!cancelled) setSession(restored)
      } catch {
        if (!cancelled) clear()
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [clear])

  const adopt = useCallback((response: AuthResponse) => {
    tokenStore.set(response.tokens)
    setSession(response.session)
  }, [])

  const signIn = useCallback(
    async (email: string, code: string) => {
      adopt(await api.login(email, code))
    },
    [adopt],
  )

  const signUp = useCallback(
    async (payload: Parameters<AuthState['signUp']>[0]) => {
      adopt(await api.register(payload))
    },
    [adopt],
  )

  const signOut = useCallback(async () => {
    try {
      await api.logout()
    } finally {
      clear()
    }
  }, [clear])

  const refresh = useCallback(async () => {
    try {
      setSession(await api.me())
    } catch {
      clear()
    }
  }, [clear])

  const patch = useCallback((partial: Partial<SessionOut>) => {
    setSession((current) => (current ? { ...current, ...partial } : current))
  }, [])

  const value = useMemo<AuthState>(() => {
    const access = (module: ModuleKey): Access => session?.access?.[module] ?? 'none'
    return {
      session,
      loading,
      signIn,
      signUp,
      adopt,
      signOut,
      refresh,
      patch,
      access,
      canView: (module) => access(module) !== 'none',
      canWrite: (module) => access(module) === 'full',
    }
  }, [session, loading, signIn, signUp, adopt, signOut, refresh, patch])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside an AuthProvider')
  return context
}
