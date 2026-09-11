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
import { WillyBot } from '@/components/WillyBot'
import { useFitDensity } from '@/hooks/useFitDensity'

const CAPABILITIES = [
  {
    mark: '01',
    title: 'SEO',
    body: 'On-page sync, rankings, and content that compounds.',
  },
  {
    mark: '02',
    title: 'AEO',
    body: 'Answer engines, citations, and structured Q&A.',
  },
  {
    mark: '03',
    title: 'Technical',
    body: 'Crawl health, schema, and fix-ready findings.',
  },
  {
    mark: '04',
    title: 'Ads',
    body: 'Programmatic spend with fraud and quality guardrails.',
  },
] as const

export function LoginPage() {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { ref: shellRef, fit } = useFitDensity<HTMLDivElement>()

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
    <div className="auth-screen auth-screen-login">
      <div
        ref={shellRef}
        className="auth-login-shell"
        data-fit-w={fit.widthBand}
        data-fit-h={fit.heightBand}
      >
        <aside className="auth-stage" aria-label="What AutoMarket AI runs">
          <div className="auth-stage-glow" aria-hidden="true" />
          <div className="auth-stage-grid" aria-hidden="true" />

          <header className="auth-stage-top">
            <BrandLogo height={34} />
            <span className="auth-stage-live">
              <span className="auth-stage-live-dot" />
              Agents online
            </span>
          </header>

          <div className="auth-stage-content">
            <div className="auth-stage-copy">
              <div className="auth-stage-hero">
                <p className="auth-stage-kicker">SEO · AEO · Ads console</p>
                <h1 className="auth-stage-title">Rank. Cite. Convert.</h1>
                <p className="auth-stage-lead">
                  Autonomous SEO, answer-engine visibility, technical health, and
                  programmatic ads in one branded workspace.
                </p>
              </div>

              <div className="auth-serp" aria-hidden="true">
                <div className="auth-serp-bar">
                  <span className="auth-serp-url">yourbrand.com</span>
                  <span className="auth-serp-query">best midsize SUV near me</span>
                </div>
                <div className="auth-serp-row is-aeo">
                  <span className="auth-serp-badge">AI overview</span>
                  <span>Cited in answer engines · AEO injector</span>
                </div>
                <div className="auth-serp-row is-organic">
                  <span className="auth-serp-rank">#1</span>
                  <span>Organic listing synced from CMS · On-page SEO</span>
                </div>
                <div className="auth-serp-row is-ads">
                  <span className="auth-serp-badge is-ads">Sponsored</span>
                  <span>Programmatic creative live · fraud-checked</span>
                </div>
              </div>

              <ul className="auth-capabilities">
                {CAPABILITIES.map((item) => (
                  <li key={item.title} className="auth-capability">
                    <span className="auth-capability-mark">{item.mark}</span>
                    <div>
                      <strong>{item.title}</strong>
                      <p>{item.body}</p>
                    </div>
                  </li>
                ))}
              </ul>
            </div>

            <div className="auth-stage-willy" aria-hidden="true">
              <WillyBot mode="hero" className="auth-willy" />
            </div>
          </div>
        </aside>

        <section className="auth-card auth-card-login" aria-label="Sign in">
          <div className="auth-login-panel">
            <div className="auth-login-brand">
              <BrandLogo height={28} />
            </div>

            <div className="auth-header auth-header-login">
              <p className="auth-panel-kicker">Welcome back</p>
              <h2 className="auth-headline">
                {step === 'email' ? 'Sign in to your console' : 'Check your inbox'}
              </h2>
              <p className="auth-tagline">
                {step === 'email'
                  ? 'Work email only. We send a one-time code, so there is no password to manage.'
                  : `We sent a ${codeLength}-digit code to ${email.trim() || 'your email'}.`}
              </p>
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
                  className="auth-form auth-form-login"
                  onSubmit={(event) => {
                    event.preventDefault()
                    void requestCode()
                  }}
                >
                  <div className="field">
                    <label htmlFor="login-email">Work email</label>
                    <input
                      id="login-email"
                      className="input input-login"
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
                      No password needed. We email you a {codeLength}-digit code.
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

                  <button
                    type="submit"
                    className="btn btn-primary btn-block btn-login"
                    disabled={busy}
                  >
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
              <div className="auth-footer auth-footer-login">
                New here? <Link to="/register">Create a workspace</Link>
              </div>
            ) : null}
          </div>
        </section>
      </div>
    </div>
  )
}
