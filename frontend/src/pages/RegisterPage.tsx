/**
 * Create a workspace.
 *
 * Two steps, and the order matters: the address is proven with a mailed code
 * *before* anything is created. So the form is filled in first, then the code
 * confirms it - and if the code is never entered, no organisation, no user and
 * no encryption key ever come into existence.
 *
 * Field-level errors from a 422 are mapped back onto their inputs.
 */
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ApiError, api } from '@/api/client'
import type { RegistrationPolicy } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { BrandLogo } from '@/components/BrandLogo'
import { CodeEntry } from '@/components/CodeEntry'
import { useToasts } from '@/components/Toasts'

export function RegisterPage() {
  const { signUp } = useAuth()
  const { fromError } = useToasts()
  const navigate = useNavigate()

  const [policy, setPolicy] = useState<RegistrationPolicy | null>(null)
  const [form, setForm] = useState({
    organization_name: '',
    full_name: '',
    email: '',
    primary_domain: '',
  })
  const [step, setStep] = useState<'details' | 'code'>('details')
  const [resendIn, setResendIn] = useState(0)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.policy().then(setPolicy).catch(() => setPolicy(null))
  }, [])

  const codeLength = policy?.code_length ?? 6

  const update = (key: keyof typeof form) => (value: string) => {
    setForm((current) => ({ ...current, [key]: value }))
    setFieldErrors((current) => {
      if (!current[key]) return current
      const next = { ...current }
      delete next[key]
      return next
    })
  }

  const requestCode = async () => {
    setError('')
    setFieldErrors({})
    if (form.organization_name.trim().length < 2) {
      setFieldErrors({ organization_name: 'Enter your organisation name' })
      return
    }
    if (form.full_name.trim().length < 2) {
      setFieldErrors({ full_name: 'Enter your name' })
      return
    }
    setBusy(true)
    try {
      const challenge = await api.requestSignupCode(form.email.trim())
      setResendIn(challenge.resend_in)
      setStep('code')
    } catch (caught) {
      if (caught instanceof ApiError) {
        setFieldErrors(caught.fields ?? {})
        setError(caught.message)
      } else {
        fromError(caught)
        setError('Could not send a verification code')
      }
    } finally {
      setBusy(false)
    }
  }

  const submitCode = async (code: string) => {
    setError('')
    setBusy(true)
    try {
      await signUp({
        organization_name: form.organization_name.trim(),
        full_name: form.full_name.trim(),
        email: form.email.trim(),
        code,
        primary_domain: form.primary_domain.trim(),
      })
      // A new workspace goes to onboarding, which is where it becomes useful.
      navigate('/onboarding', { replace: true })
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : 'Could not create your workspace',
      )
    } finally {
      setBusy(false)
    }
  }

  if (policy && !policy.public_signup) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-header">
            <div className="auth-brand">
              <BrandLogo height={44} />
            </div>
            <h1 className="auth-headline">Sign-up is closed</h1>
            <p className="auth-tagline">
              Ask an administrator to invite you, then use the link in your invitation email.
            </p>
          </div>
          <Link to="/login" className="btn btn-secondary btn-block">
            Back to sign in
          </Link>
        </div>
      </div>
    )
  }

  if (step === 'code') {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-header">
            <div className="auth-brand">
              <BrandLogo height={44} />
            </div>
            <h1 className="auth-headline">Confirm your email</h1>
            <p className="auth-tagline">
              {form.organization_name.trim()} is created once this checks out.
            </p>
          </div>
          <CodeEntry
            email={form.email.trim()}
            length={codeLength}
            resendIn={resendIn}
            busy={busy}
            error={error}
            submitLabel="Create workspace"
            busyLabel="Creating your workspace…"
            onSubmit={(code) => void submitCode(code)}
            onResend={() => void requestCode()}
            onChangeEmail={() => {
              setStep('details')
              setError('')
            }}
          />
        </div>
      </div>
    )
  }

  return (
    <div className="auth-screen">
      <div className="auth-card auth-card-wide">
        <div className="auth-header">
          <div className="auth-brand">
            <BrandLogo height={44} />
          </div>
          <h1 className="auth-headline">Create your workspace</h1>
          <p className="auth-tagline">
            You become its Super Admin, and can invite your team afterwards.
          </p>
        </div>

        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault()
            void requestCode()
          }}
        >
          <div className="field">
            <label htmlFor="reg-org">Organisation name</label>
            <input
              id="reg-org"
              className="input"
              placeholder="Your dealership group"
              value={form.organization_name}
              onChange={(event) => update('organization_name')(event.target.value)}
            />
            {fieldErrors.organization_name ? (
              <div className="field-error">{fieldErrors.organization_name}</div>
            ) : null}
          </div>

          <div className="field">
            <label htmlFor="reg-name">Your name</label>
            <input
              id="reg-name"
              className="input"
              autoComplete="name"
              placeholder="Dana Whitfield"
              value={form.full_name}
              onChange={(event) => update('full_name')(event.target.value)}
            />
            {fieldErrors.full_name ? (
              <div className="field-error">{fieldErrors.full_name}</div>
            ) : null}
          </div>

          <div className="field">
            <label htmlFor="reg-email">Work email</label>
            <input
              id="reg-email"
              className="input"
              type="email"
              autoComplete="email"
              placeholder="you@dealergroup.com"
              value={form.email}
              onChange={(event) => update('email')(event.target.value)}
            />
            {fieldErrors.email ? (
              <div className="field-error">{fieldErrors.email}</div>
            ) : (
              <div className="field-hint">
                {policy?.work_email_required
                  ? 'A work address is required to sign up. '
                  : ''}
                This is how you sign in - we email a code each time, so there is no
                password to choose or lose.
              </div>
            )}
          </div>

          <div className="field">
            <label htmlFor="reg-domain">Website domain (optional)</label>
            <input
              id="reg-domain"
              className="input"
              placeholder="yourdealership.com"
              value={form.primary_domain}
              onChange={(event) => update('primary_domain')(event.target.value)}
            />
            <div className="field-hint">
              The site the agents will optimise. You can set this during onboarding
              instead.
            </div>
          </div>

          {error && Object.keys(fieldErrors).length === 0 ? (
            <div className="auth-error">{error}</div>
          ) : null}

          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            {busy ? 'Sending a code…' : 'Continue'}
          </button>
        </form>

        <div className="auth-footer">
          Already have an account? <Link to="/login">Sign in</Link>
        </div>
      </div>
    </div>
  )
}
