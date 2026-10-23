/**
 * Enrol a TOTP authenticator and download recovery codes.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ApiError } from '@/api/client'
import { Blueprint, EmptyState, Field, PageHeader, SectionHeading } from '@/components/ui'

export function MfaSetupPage() {
  const [secret, setSecret] = useState('')
  const [otpauth, setOtpauth] = useState('')
  const [recovery, setRecovery] = useState<string[]>([])
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const [busy, setBusy] = useState(false)

  async function begin() {
    setError('')
    setBusy(true)
    try {
      const out = await api.mfaTotpBegin()
      setSecret(out.secret)
      setOtpauth(out.otpauth_url)
      setRecovery(out.recovery_codes)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not start enrolment')
    } finally {
      setBusy(false)
    }
  }

  async function confirm() {
    setError('')
    setBusy(true)
    try {
      await api.mfaTotpConfirm({ secret, code, recovery_codes: recovery })
      setDone(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not confirm authenticator')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack" style={{ maxWidth: 640 }}>
      <PageHeader
        title="Authenticator app"
        description="Add a TOTP second factor. Recovery codes are shown once — store them offline."
      />

      {!secret ? (
        <Blueprint className="card elev-sm">
          <SectionHeading
            title="Get started"
            description="You will receive a secret and recovery codes. Confirm with a live code from your authenticator."
          />
          <div className="row">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void begin()}
              disabled={busy}
            >
              {busy ? 'Starting…' : 'Start enrolment'}
            </button>
            <Link className="btn btn-ghost" to="/account">
              Back to account
            </Link>
          </div>
          {error ? (
            <div className="auth-error" role="alert">
              {error}
            </div>
          ) : null}
        </Blueprint>
      ) : done ? (
        <EmptyState
          title="Authenticator enrolled"
          description="Use it at the next sign-in when prompted. Keep your recovery codes somewhere safe offline."
          action={
            <Link className="btn btn-secondary" to="/account">
              Back to account
            </Link>
          }
        />
      ) : (
        <div className="stack">
          <Blueprint className="card elev-sm">
            <SectionHeading
              title="Scan or enter the secret"
              description="Scan this URI in your authenticator, or type the secret manually."
            />
            <Field label="otpauth URI" htmlFor="mfa-otpauth">
              <textarea
                id="mfa-otpauth"
                className="input mono-sub"
                value={otpauth}
                readOnly
                rows={3}
              />
            </Field>
            <Field label="Secret" htmlFor="mfa-secret">
              <input id="mfa-secret" className="input mono-sub" value={secret} readOnly />
            </Field>
          </Blueprint>

          <Blueprint className="card elev-sm">
            <SectionHeading
              title="Recovery codes"
              description="Save these now. Each code works once if you lose the authenticator."
            />
            <ul className="stack-sm">
              {recovery.map((item) => (
                <li key={item} className="list-row is-compact">
                  <code className="mono-sub">{item}</code>
                </li>
              ))}
            </ul>
          </Blueprint>

          <Blueprint className="card elev-sm">
            <SectionHeading title="Confirm enrolment" />
            <Field label="Code from authenticator" htmlFor="mfa-confirm" required>
              <input
                id="mfa-confirm"
                className="input"
                value={code}
                onChange={(e) => {
                  setCode(e.target.value)
                  if (error) setError('')
                }}
                inputMode="numeric"
                autoComplete="one-time-code"
                placeholder="6-digit code"
              />
            </Field>
            <div className="row">
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void confirm()}
                disabled={busy || code.trim().length < 6}
              >
                {busy ? 'Confirming…' : 'Confirm'}
              </button>
              <Link className="btn btn-ghost" to="/account">
                Cancel
              </Link>
            </div>
            {error ? (
              <div className="auth-error" role="alert">
                {error}
              </div>
            ) : null}
          </Blueprint>
        </div>
      )}
    </div>
  )
}
