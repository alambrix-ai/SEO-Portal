/**
 * Enrol a TOTP authenticator and download recovery codes.
 */
import { useState } from 'react'

import { api, ApiError } from '@/api/client'
import { Blueprint, Field, PageHeader } from '@/components/ui'

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
    <div style={{ maxWidth: 640 }}>
      <PageHeader
        title="Authenticator app"
        description="Add a TOTP second factor. Recovery codes are shown once — store them offline."
      />
      <Blueprint>
        {!secret ? (
          <button type="button" className="btn btn-primary" onClick={() => void begin()} disabled={busy}>
            Start enrolment
          </button>
        ) : done ? (
          <p>Authenticator enrolled. Use it at the next sign-in when prompted.</p>
        ) : (
          <div className="stack gap-md">
            <p className="muted">Scan this URI in your authenticator, or enter the secret manually.</p>
            <code className="block">{otpauth}</code>
            <Field label="Secret">
              <input className="input" value={secret} readOnly />
            </Field>
            <Field label="Recovery codes (save these)">
              <ul>
                {recovery.map((c) => (
                  <li key={c}>
                    <code>{c}</code>
                  </li>
                ))}
              </ul>
            </Field>
            <Field label="Code from authenticator">
              <input
                className="input"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
              />
            </Field>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void confirm()}
              disabled={busy || code.length < 6}
            >
              Confirm
            </button>
          </div>
        )}
        {error ? <p className="error">{error}</p> : null}
      </Blueprint>
    </div>
  )
}
