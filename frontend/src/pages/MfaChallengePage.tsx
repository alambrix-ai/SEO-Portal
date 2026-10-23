/**
 * Second factor after email OTP when the account has MFA enrolled.
 */
import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'

import { api, ApiError } from '@/api/client'
import { useAuth } from '@/auth/AuthContext'
import { BrandLogo } from '@/components/BrandLogo'
import { Field } from '@/components/ui'
import { useFitDensity } from '@/hooks/useFitDensity'

type LocState = { mfa_token?: string; methods?: string[] }

export function MfaChallengePage() {
  const location = useLocation()
  const navigate = useNavigate()
  const { adopt } = useAuth()
  const { ref: shellRef, fit } = useFitDensity<HTMLDivElement>()
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
      <div
        ref={shellRef}
        className="auth-login-shell"
        data-fit-w={fit.widthBand}
        data-fit-h={fit.heightBand}
      >
        <aside className="auth-stage auth-stage-enterprise" aria-label="Multi-factor sign-in">
          <header className="auth-stage-top">
            <BrandLogo height={26} />
          </header>
          <div className="auth-stage-content">
            <div className="auth-stage-copy">
              <p className="auth-stage-kicker">Extra step</p>
              <h1 className="auth-stage-title">Confirm it is you</h1>
              <p className="auth-stage-lead">
                This account uses an authenticator app. Enter the current code, or a
                one-time recovery code you saved when you enrolled.
              </p>
            </div>
          </div>
          <footer className="auth-stage-foot">Encrypted sessions · Role-based access</footer>
        </aside>

        <section className="auth-card auth-card-login" aria-label="Authenticator challenge">
          <div className="auth-login-panel">
            <div className="auth-login-brand">
              <BrandLogo height={28} />
            </div>
            <div className="auth-header auth-header-login">
              <p className="auth-panel-kicker">Almost there</p>
              <h2 className="auth-headline">Enter your authenticator code</h2>
              <p className="auth-tagline">
                Open your authenticator app, or use a recovery code if you do not have
                the device with you.
              </p>
            </div>

            <form
              className="auth-form auth-form-login stack"
              onSubmit={(event) => {
                event.preventDefault()
                void finish({ mfa_token: state.mfa_token!, totp_code: code })
              }}
            >
              <Field label="Authenticator code" htmlFor="mfa-totp">
                <input
                  id="mfa-totp"
                  className="input input-login"
                  value={code}
                  onChange={(e) => {
                    setCode(e.target.value)
                    if (error) setError('')
                  }}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  autoFocus
                  placeholder="6-digit code"
                />
              </Field>
              <button
                type="submit"
                className="btn btn-primary btn-block btn-login"
                disabled={busy || code.trim().length < 6}
              >
                {busy ? 'Verifying…' : 'Verify'}
              </button>
            </form>

            <div className="auth-divider">or use a recovery code</div>

            <div className="stack">
              <Field label="Recovery code" htmlFor="mfa-recovery">
                <input
                  id="mfa-recovery"
                  className="input input-login"
                  value={recovery}
                  onChange={(e) => {
                    setRecovery(e.target.value)
                    if (error) setError('')
                  }}
                  autoComplete="off"
                  placeholder="XXXX-XXXX-XXXX"
                />
              </Field>
              <button
                type="button"
                className="btn btn-secondary btn-block"
                disabled={busy || recovery.trim().length < 8}
                onClick={() =>
                  void finish({ mfa_token: state.mfa_token!, recovery_code: recovery })
                }
              >
                Use recovery code
              </button>
            </div>

            {error ? (
              <div className="auth-error" role="alert">
                {error}
              </div>
            ) : null}

            <div className="auth-footer auth-footer-login">
              <Link to="/login">Back to sign in</Link>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
