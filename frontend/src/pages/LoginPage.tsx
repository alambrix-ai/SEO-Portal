/**
 * Sign in.
 *
 * Two steps: give an address, then type the code that arrives in it. SSO
 * buttons sit above a divider when a deployment offers any.
 *
 * If the address has no account, the API says so immediately so we can guide
 * the operator to create a workspace (or ask for an invite) instead of
 * waiting for a code that will never arrive.
 */
import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { BrandLogo } from '@/components/BrandLogo'
import { ApiError, api } from '@/api/client'
import { softenErrorMessage } from '@/lib/softenError'
import type { RegistrationPolicy } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { CodeEntry } from '@/components/CodeEntry'

export function LoginPage() {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [step, setStep] = useState<'email' | 'code'>('email')
  const [resendIn, setResendIn] = useState(0)
  const [error, setError] = useState('')
  const [noAccount, setNoAccount] = useState(false)
  const [busy, setBusy] = useState(false)
  const [policy, setPolicy] = useState<RegistrationPolicy | null>(null)

  useEffect(() => {
    api
      .policy()
      .then(setPolicy)
      .catch(() => setPolicy(null))
  }, [])

  const destination = (location.state as { from?: string } | null)?.from ?? '/dashboard'
  const ssoProviders = policy?.sso_providers ?? []
  const codeLength = policy?.code_length ?? 6
  const signupOpen = policy?.public_signup !== false

  const message = (caught: unknown, fallback: string) =>
    softenErrorMessage(
      caught instanceof ApiError || caught instanceof Error ? caught.message : '',
      fallback,
    )

  const requestCode = async () => {
    const address = email.trim()
    if (!address) {
      setError('Enter your email address')
      setNoAccount(false)
      return
    }
    setError('')
    setNoAccount(false)
    setBusy(true)
    try {
      const challenge = await api.requestCode(address)
      setResendIn(challenge.resend_in)
      setStep('code')
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 404) {
        setNoAccount(true)
        setError(
          caught.message ||
            'No account found for that email. Create a workspace to get started.',
        )
      } else if (caught instanceof ApiError && caught.status === 403) {
        setNoAccount(false)
        setError(
          caught.message ||
            'That account is deactivated. Ask a workspace admin to restore access.',
        )
      } else {
        setError(message(caught, 'Could not send a code'))
      }
    } finally {
      setBusy(false)
    }
  }

  const submitCode = async (code: string) => {
    setError('')
    setNoAccount(false)
    setBusy(true)
    try {
      await signIn(email.trim(), code)
      navigate(destination, { replace: true })
    } catch (caught) {
      setError(message(caught, 'Could not sign you in'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <i className="auth-mark" />

        <div className="auth-header">
          <div className="auth-brand">
            <BrandLogo height={38} />
          </div>
          <div className="auth-tagline">
            {step === 'email'
              ? 'Autonomous SEO, AEO & Programmatic Ads — Enterprise Console'
              : 'Check your inbox'}
          </div>
        </div>

        {step === 'email' ? (
          <>
            {ssoProviders.length > 0 ? (
              <>
                <div className="stack-sm">
                  {ssoProviders.map((provider) => (
                    <a
                      key={provider.id}
                      className="btn btn-secondary btn-block"
                      href={provider.start_url}
                    >
                      Continue with {provider.label}
                    </a>
                  ))}
                </div>
                <div className="auth-divider">or sign in</div>
              </>
            ) : null}

            <form
              className="auth-form"
              onSubmit={(event) => {
                event.preventDefault()
                void requestCode()
              }}
            >
              <div className="field">
                <label htmlFor="login-email">Work email</label>
                <input
                  id="login-email"
                  className="input"
                  type="email"
                  autoComplete="email"
                  autoFocus
                  placeholder="you@yourcompany.com"
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value)
                    if (noAccount) setNoAccount(false)
                    if (error) setError('')
                  }}
                />
                <div className="field-hint">
                  No password needed — we email you a {codeLength}-digit code.
                </div>
              </div>

              {noAccount ? (
                <div className="auth-error" role="alert">
                  <strong>No account for that email.</strong>
                  <div style={{ marginTop: 6 }}>
                    {signupOpen ? (
                      <>
                        <Link to="/register">Create a workspace</Link> to get
                        started, or ask your admin for an invitation.
                      </>
                    ) : (
                      <>Ask your workspace admin for an invitation to join.</>
                    )}
                  </div>
                </div>
              ) : error ? (
                <div className="auth-error" role="alert">
                  {error}
                </div>
              ) : null}

              <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
                {busy ? 'Sending a code…' : 'Email me a sign-in code'}
              </button>
            </form>
          </>
        ) : (
          <CodeEntry
            email={email.trim()}
            length={codeLength}
            resendIn={resendIn}
            busy={busy}
            error={error}
            submitLabel="Sign in"
            busyLabel="Signing in…"
            onSubmit={(code) => void submitCode(code)}
            onResend={() => void requestCode()}
            onChangeEmail={() => {
              setStep('email')
              setError('')
              setNoAccount(false)
            }}
          />
        )}

        {signupOpen ? (
          <div className="auth-footer">
            New here? <Link to="/register">Create a workspace</Link>
          </div>
        ) : null}
      </div>
    </div>
  )
}
