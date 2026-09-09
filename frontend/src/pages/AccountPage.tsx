/**
 * Account — your own details, and the one security control you own.
 *
 * There is no password to change here, because there is no password. What
 * replaces it is "sign out everywhere": if you think somebody has been in your
 * mailbox, ending every live session is the thing that actually cuts their
 * access off. A used code cannot be replayed, but a session minted from one
 * lasts until it is revoked.
 *
 * Available to every role, because it is about the signed-in person rather
 * than the workspace.
 */
import { useEffect, useState } from 'react'

import { ApiError, api } from '@/api/client'
import type { RegistrationPolicy } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import { Blueprint, SectionHeading, Tag } from '@/components/ui'

export function AccountPage() {
  const { session, signOut } = useAuth()
  const { push } = useToasts()

  const [policy, setPolicy] = useState<RegistrationPolicy | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.policy().then(setPolicy).catch(() => setPolicy(null))
  }, [])

  if (!session) return null

  const minutes = Math.round((policy?.code_expires_in ?? 600) / 60)

  const signOutEverywhere = async () => {
    setError('')
    setBusy(true)
    try {
      const result = await api.signOutEverywhere()
      push(result.detail, 'success')
      // This session was revoked too, so there is nothing left to stay on.
      await signOut()
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : 'Could not sign out your sessions',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ maxWidth: 640 }}>
      <SectionHeading title="Your account" />

      <Blueprint className="card elev-sm">
        <div className="card-kicker">{session.organization.name}</div>
        <div className="card-title">{session.user.name}</div>
        <div className="small muted">{session.user.email}</div>
        <div className="row" style={{ marginTop: 8 }}>
          <Tag tone="accent">{session.user.role_label}</Tag>
          {session.user.is_owner ? <Tag tone="outline">Workspace owner</Tag> : null}
          <Tag tone="accent-2">Email confirmed</Tag>
        </div>
      </Blueprint>

      <SectionHeading
        title="How you sign in"
        description="This platform has no passwords."
        spaced
      />

      <Blueprint className="card elev-sm">
        <p className="small">
          Each time you sign in, a {policy?.code_length ?? 6}-digit code is emailed to{' '}
          <strong>{session.user.email}</strong>. It works once and expires after{' '}
          {minutes} minutes.
        </p>
        <p className="small muted">
          Nothing that could be stolen and reused is stored — there is no password
          hash to leak, and no reset link to intercept. The trade is that your
          mailbox is the key to this workspace, so it is worth protecting as well
          as you would protect a password.
        </p>
      </Blueprint>

      <SectionHeading
        title="Sign out everywhere"
        description="Ends every session on every device, including this one."
        spaced
      />

      <div style={{ maxWidth: 460 }}>
        <p className="small muted">
          Use this if you have signed in on a device you no longer control, or if
          you think somebody else has had access to your email. You will need a new
          code to get back in.
        </p>
        {error ? <div className="auth-error">{error}</div> : null}
        <button
          type="button"
          className="btn btn-secondary"
          disabled={busy}
          onClick={() => void signOutEverywhere()}
        >
          {busy ? 'Signing out…' : 'Sign out everywhere'}
        </button>
      </div>
    </div>
  )
}
