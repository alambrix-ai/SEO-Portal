/**
 * Second factor after email OTP when the account has MFA enrolled.
 */
import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'

import { api, ApiError } from '@/api/client'
import { useAuth } from '@/auth/AuthContext'
import { Field } from '@/components/ui'

type LocState = { mfa_token?: string; methods?: string[] }

export function MfaChallengePage() {
  const location = useLocation()
  const navigate = useNavigate()
  const { adopt } = useAuth()
  const state = (location.state || {}) as LocState
  const [code, setCode] = useState('')
  const [recovery, setRecovery] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  if (!state.mfa_token) {
    return <Navigate to="/login" replace />
  }

  async function finish(body: Parameters<typeof api.mfaVerify>[0]) {
    setBusy(true)
    setError('')
    try {
      const response = await api.mfaVerify(body)
      if (!response.tokens || !response.session) {
        setError('Could not complete sign-in')
        return
      }
      adopt(response)
      navigate('/dashboard', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Verification failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen auth-screen-login">
      <div className="auth-card">
        <h1>Confirm it is you</h1>
        <p className="muted">Enter the code from your authenticator app, or a recovery code.</p>
        <Field label="Authenticator code">
          <input
            className="input"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode="numeric"
            autoFocus
          />
        </Field>
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy || code.length < 6}
          onClick={() => void finish({ mfa_token: state.mfa_token!, totp_code: code })}
        >
          Verify
        </button>
        <Field label="Recovery code">
          <input className="input" value={recovery} onChange={(e) => setRecovery(e.target.value)} />
        </Field>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={busy || recovery.length < 8}
          onClick={() => void finish({ mfa_token: state.mfa_token!, recovery_code: recovery })}
        >
          Use recovery code
        </button>
        {error ? <p className="error">{error}</p> : null}
      </div>
    </div>
  )
}
