/**
 * Sign in.
 *
 * Two steps: give an address, then type the code that arrives in it. SSO
 * buttons sit above a divider when a deployment offers any.
 *
 * The API answers every code request the same way, whether or not the address
 * has an account, so this screen never reveals who has one. It shows the
 * server's one message and points newcomers to sign-up underneath.
 */
import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { BrandLogo } from '@/components/BrandLogo'
import { ApiError, api } from '@/api/client'
import { softenErrorMessage } from '@/lib/softenError'
import type { RegistrationPolicy } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { CodeEntry } from '@/components/CodeEntry'
import { useFitDensity } from '@/hooks/useFitDensity'

/**
 * A sign-in button only links to a same-site path ("/api/...") or an https
 * address; anything else (javascript:, data:, "//host") is not shown.
 */
function isSafeStartUrl(value: string): boolean {
  const text = (value || '').trim()
  if (!text || /\s/.test(text)) return false
  if (text.startsWith('/')) return !text.startsWith('//') && !text.startsWith('/\\')
  try {
    const url = new URL(text)
    return url.protocol === 'https:' && url.hostname !== '' && url.username === '' && url.password === ''
  } catch {
    return false
  }
}

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

/** Shown if the server's message is missing; mirrors its wording. */
const SENT_FALLBACK =
  "If that address has an account, we've sent it a sign-in code. Check your inbox."

export function LoginPage() {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { ref: shellRef, fit } = useFitDensity<HTMLDivElement>()

  const [email, setEmail] = useState('')
  const [step, setStep] = useState<'email' | 'code'>('email')
  const [resendIn, setResendIn] = useState(0)
  const [error, setError] = useState('')
  /** The server's uniform "if that address has an account…" message. */
  const [sentNote, setSentNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [policy, setPolicy] = useState<RegistrationPolicy | null>(null)

  useEffect(() => {
    api
      .policy()
      .then(setPolicy)
      .catch(() => setPolicy(null))
  }, [])

  const ssoProviders = (policy?.sso_providers ?? []).filter((provider) => isSafeStartUrl(provider.start_url))
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
      return
    }
    setError('')
    setBusy(true)
    try {
      const challenge = await api.requestCode(address)
      setSentNote(softenErrorMessage(challenge.detail, SENT_FALLBACK))
      setResendIn(challenge.resend_in)
      setStep('code')
    } catch (caught) {
      setError(message(caught, 'We could not send a code just now. Please try again.'))
    } finally {
      setBusy(false)
    }
  }

  const submitCode = async (code: string) => {
    setError('')
    setBusy(true)
    try {
      const nextSession = await signIn(email.trim(), code)
      const intended = (location.state as { from?: string } | null)?.from
      // Unfinished workspaces always finish the wizard first.
      const target = !nextSession.onboarding_complete
        ? '/onboarding'
        : intended && intended !== '/login' && intended !== '/register'
          ? intended
          : '/dashboard'
      navigate(target, { replace: true })
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
        <aside className="auth-stage auth-stage-enterprise" aria-label="What Synaprank.ai runs">
          <header className="auth-stage-top">
            <BrandLogo height={26} />
          </header>

          <div className="auth-stage-content">
            <div className="auth-stage-copy">
              <p className="auth-stage-kicker">Search &amp; growth automation platform</p>
              <h1 className="auth-stage-title">
                Autonomous SEO, AEO and paid media, governed by your team.
              </h1>
              <p className="auth-stage-lead">
                Agents audit, optimize and publish on schedule. Every change follows the
                approval policy you set.
              </p>

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
          </div>

          <footer className="auth-stage-foot">
            Role-based access · Encrypted credentials · Full audit trail
          </footer>
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
                  : sentNote || SENT_FALLBACK}
              </p>
              {step === 'code' ? (
                <p className="field-hint">
                  The code has {codeLength} digits. Nothing arrived? Check the address
                  {signupOpen ? (
                    <>
                      , or <Link to="/register">create a workspace</Link> if you are new
                    </>
                  ) : (
                    <>, or ask your workspace admin for an invitation</>
                  )}
                  .
                </p>
              ) : null}
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
                        if (error) setError('')
                      }}
                    />
                    <div className="field-hint">
                      No password needed. We email you a {codeLength}-digit code.
                    </div>
                  </div>

                  {error ? (
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
                  setSentNote('')
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
