/**
 * Accept a teammate invitation.
 *
 * There is nothing to choose here but a name. The invite link arrived in the
 * invitee's own mailbox, which proves the address as well as a mailed code
 * would, so asking for a second confirmation would be ceremony. From here on
 * they sign in with a code like everybody else.
 *
 * The preview call confirms the invitation is still valid before showing a
 * form, so an expired link fails immediately rather than after the user has
 * typed everything.
 */
import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { ApiError, api } from '@/api/client'
import type { InvitationPreview } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { Loading } from '@/components/ui'

export function AcceptInvitePage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const { adopt } = useAuth()
  const token = params.get('token') ?? ''

  const [preview, setPreview] = useState<InvitationPreview | null>(null)
  const [loadError, setLoadError] = useState('')
  const [loading, setLoading] = useState(Boolean(token))
  const [fullName, setFullName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!token) {
      setLoadError('This invitation link is incomplete.')
      return
    }
    api
      .invitationPreview(token)
      .then(setPreview)
      .catch((caught: unknown) =>
        setLoadError(
          caught instanceof ApiError ? caught.message : 'This invitation is not valid',
        ),
      )
      .finally(() => setLoading(false))
  }, [token])

  const submit = async () => {
    setError('')
    setBusy(true)
    try {
      adopt(
        await api.acceptInvitation({ token, full_name: fullName.trim() }),
      )
      navigate('/dashboard', { replace: true })
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : 'Could not accept this invitation',
      )
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="auth-screen">
        <Loading label="Checking your invitation…" />
      </div>
    )
  }

  if (loadError || !preview) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <i className="auth-mark" />
          <div className="auth-header">
            <div className="auth-brand">Invitation unavailable</div>
            <div className="auth-tagline">{loadError}</div>
          </div>
          <Link to="/login" className="btn btn-secondary btn-block">
            Back to sign in
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <i className="auth-mark" />
        <div className="auth-header">
          <div className="auth-brand">Join {preview.organization_name}</div>
          <div className="auth-tagline">
            Invited as {preview.role_label} — {preview.email}
          </div>
        </div>

        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault()
            void submit()
          }}
        >
          <div className="field">
            <label htmlFor="invite-name">Your name</label>
            <input
              id="invite-name"
              className="input"
              autoComplete="name"
              value={fullName}
              onChange={(event) => setFullName(event.target.value)}
            />
          </div>

          <div className="field-hint">
            No password to set. Each time you sign in, {preview.organization_name}{' '}
            emails a one-time code to {preview.email} — so keep access to that
            mailbox.
          </div>

          {error ? <div className="auth-error">{error}</div> : null}

          <button
            type="submit"
            className="btn btn-primary btn-block"
            disabled={busy || !fullName.trim()}
          >
            {busy ? 'Joining…' : 'Join workspace'}
          </button>
        </form>
      </div>
    </div>
  )
}
