/**
 * Onboarding — four steps, and a reason to finish each one.
 *
 * The previous version was four text buttons, two dropdowns, three
 * checkboxes and a three-line summary. It was dull for a structural reason
 * rather than a cosmetic one: **step two did nothing at all.** It wrote three
 * hard-coded flags — google, meta, linkedin — to a field no code in the
 * platform ever read, out of six ad connectors that exist. A step that
 * cannot affect anything cannot be made interesting by restyling it.
 *
 * So the shape changed with the paint:
 *
 * 1. **Your site** — the domain, and where its content lives, chosen from
 *    the real connector catalogue with each vendor's own mark.
 * 2. **Ad platforms** — what is actually connected, and a way to connect
 *    more. It reports rather than pretends.
 * 3. **Guardrails** — three cards that each say what will happen to the
 *    customer's site, instead of three radio buttons carrying half a
 *    sentence each.
 * 4. **Review** — what was chosen, and precisely what Launch does.
 *
 * Progress still saves on every advance, so a reload resumes where the
 * workspace left off.
 */
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '@/api/client'
import type { OnboardingOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { BrandLogo } from '@/components/BrandLogo'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { ErrorState, Loading } from '@/components/ui'
import { useResource } from '@/hooks/useResource'

const STEPS = [
  { label: 'Your site', purpose: 'Where your content lives' },
  { label: 'Ad platforms', purpose: 'What we can spend and measure on' },
  { label: 'Guardrails', purpose: 'How much the agents may do alone' },
  { label: 'Review', purpose: 'What happens when you launch' },
] as const

export function OnboardingPage() {
  const navigate = useNavigate()
  const { refresh, canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const { data, loading, error, reload, set } = useResource(() => api.onboarding())

  const [step, setStep] = useState(0)
  const [busy, setBusy] = useState(false)
  const readOnly = !canWrite('onboarding')

  // Resume where the workspace left off.
  useEffect(() => {
    if (data) setStep(Math.min(data.step, STEPS.length - 1))
  }, [data])

  if (loading && !data) return <Loading label="Loading onboarding…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const state = data

  const save = async (partial: Partial<OnboardingOut> & { step?: number }) => {
    if (readOnly) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusy(true)
    try {
      set(
        await api.saveOnboarding({
          step: partial.step,
          domain: partial.domain,
          cms: partial.cms,
          guardrail: partial.guardrail,
        }),
      )
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const next = async () => {
    if (step === 0 && !state.domain.trim()) {
      push('Enter your website domain — everything else is scoped to it', 'warning')
      return
    }
    const target = Math.min(STEPS.length - 1, step + 1)
    setStep(target)
    await save({ step: target })
  }

  const back = async () => {
    const target = Math.max(0, step - 1)
    setStep(target)
    await save({ step: target })
  }

  const launch = async () => {
    setBusy(true)
    try {
      fromResult(await api.completeOnboarding())
      await refresh()
      navigate('/dashboard')
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const connectedAds = state.ad_platforms.filter((platform) => platform.connected)
  const chosenGuardrail = state.guardrail_options.find(
    (option) => option.value === state.guardrail,
  )

  return (
    <div className="onboard">
      {/* The hero exists to answer "what is this going to do to my site?"
          before the first field, because that is the question somebody has
          when a product asks them for their domain. */}
      <header className="onboard-hero">
        <BrandLogo height={26} />
        <h1 className="onboard-title">Set up your workspace</h1>
        <p className="onboard-sub">
          Four steps, about two minutes. Nothing touches your site until you
          launch — and after that, only within the guardrail you pick in step
          three.
        </p>
      </header>

      {/* The rail. Numbered, with the step's purpose under its name, so the
          wizard says where it is going rather than only where it is. */}
      <nav className="onboard-rail" aria-label="Onboarding progress">
        {STEPS.map(({ label, purpose }, index) => {
          const done = index < step
          const current = index === step
          return (
            <button
              key={label}
              type="button"
              className={[
                'onboard-stepper',
                current ? 'is-current' : '',
                done ? 'is-done' : '',
              ]
                .filter(Boolean)
                .join(' ')}
              aria-current={current ? 'step' : undefined}
              onClick={() => setStep(index)}
            >
              <span className="onboard-bullet" aria-hidden="true">
                {done ? '✓' : index + 1}
              </span>
              <span className="onboard-stepper-text">
                <span className="onboard-stepper-label">{label}</span>
                <span className="onboard-stepper-purpose">{purpose}</span>
              </span>
            </button>
          )
        })}
      </nav>

      <div className="onboard-panel card">
        {step === 0 ? (
          <>
            <h2 className="onboard-h">Where does your content live?</h2>
            <p className="onboard-lede">
              The domain scopes everything — audits, keywords, the pages agents
              read. Pick the system it is published from and the right
              connector is waiting for you afterwards.
            </p>

            <div className="field">
              <label htmlFor="ob-domain">
                Primary domain
                <span className="field-required" aria-hidden="true">
                  *
                </span>
              </label>
              <input
                id="ob-domain"
                className="input onboard-domain"
                placeholder="yourcompany.com"
                value={state.domain}
                disabled={readOnly}
                onChange={(event) => set({ ...state, domain: event.target.value })}
                onBlur={(event) => void save({ domain: event.target.value })}
              />
              <div className="field-hint">
                Just the domain — no https:// and no trailing path.
              </div>
            </div>

            {/* Cards rather than a dropdown of nine strings. The marks are
                the connectors' own, and the slug comes from the registry, so
                a new CMS connector appears here the day it exists. */}
            <div className="field">
              <label>Content system</label>
              <div className="onboard-choices">
                {state.cms_options.map((option) => (
                  <button
                    key={option.name}
                    type="button"
                    className={`onboard-choice${
                      state.cms === option.name ? ' is-picked' : ''
                    }`}
                    disabled={readOnly}
                    onClick={() => {
                      set({ ...state, cms: option.name })
                      void save({ cms: option.name })
                    }}
                  >
                    {option.slug ? (
                      <ConnectorIcon slug={option.slug} name={option.name} size={26} />
                    ) : (
                      <span className="onboard-choice-blank" aria-hidden="true">
                        ?
                      </span>
                    )}
                    <span className="onboard-choice-name">{option.name}</span>
                  </button>
                ))}
              </div>
              <div className="field-hint">
                Not sure, or it is something bespoke? Pick “Other” — the Site
                Crawler reads any site by fetching its pages, whatever built
                them.
              </div>
            </div>
          </>
        ) : null}

        {step === 1 ? (
          <>
            <h2 className="onboard-h">Ad platforms</h2>
            <p className="onboard-lede">
              These are the platforms this workspace can buy and report on.
              Anything connected is shown as connected — there is nothing to
              tick here, because what matters is the account being wired up
              rather than an intention being recorded.
            </p>

            <div className="onboard-choices is-wide">
              {state.ad_platforms.map((platform) => (
                <div
                  key={platform.slug}
                  className={`onboard-choice is-static${
                    platform.connected ? ' is-picked' : ''
                  }`}
                >
                  <ConnectorIcon slug={platform.slug} name={platform.name} size={26} />
                  <span className="onboard-choice-name">{platform.name}</span>
                  <span
                    className={`onboard-choice-state${
                      platform.connected ? ' is-on' : ''
                    }`}
                  >
                    {platform.connected ? 'Connected' : 'Not connected'}
                  </span>
                </div>
              ))}
            </div>

            <p className="onboard-note">
              {connectedAds.length === 0
                ? 'None connected yet. The SEO and content agents do not need any of these — connect them when you want the ad side working too.'
                : `${connectedAds.length} connected. You can add the rest at any time.`}{' '}
              <Link to="/connectors">Open Connectors →</Link>
            </p>
          </>
        ) : null}

        {step === 2 ? (
          <>
            <h2 className="onboard-h">How much may the agents do alone?</h2>
            <p className="onboard-lede">
              This is the one setting on this screen that changes what reaches
              your site. It applies to every agent, and each agent can be
              tightened further on its own card.
            </p>

            <div className="onboard-rails">
              {state.guardrail_options.map((option) => {
                const picked = state.guardrail === option.value
                return (
                  <button
                    key={option.value}
                    type="button"
                    className={`onboard-rail-card${picked ? ' is-picked' : ''}`}
                    disabled={readOnly}
                    aria-pressed={picked}
                    onClick={() => {
                      set({ ...state, guardrail: option.value })
                      void save({ guardrail: option.value })
                    }}
                  >
                    <span className="onboard-rail-head">
                      <span className="onboard-radio" aria-hidden="true" />
                      <span className="onboard-rail-name">{option.name}</span>
                      {option.value === 'hybrid' ? (
                        <span className="onboard-recommend">Recommended</span>
                      ) : null}
                    </span>
                    <span className="onboard-rail-detail">{option.detail}</span>
                  </button>
                )
              })}
            </div>
          </>
        ) : null}

        {step === 3 ? (
          <>
            <h2 className="onboard-h">Ready to launch</h2>
            <p className="onboard-lede">
              Launching applies the guardrail and schedules every configured
              agent. Agents you have not configured stay paused — nothing
              starts working on your site by surprise.
            </p>

            <dl className="onboard-review">
              <div className="onboard-review-row">
                <dt>Domain</dt>
                <dd>{state.domain || <span className="muted">not set</span>}</dd>
              </div>
              <div className="onboard-review-row">
                <dt>Content system</dt>
                <dd>{state.cms || <span className="muted">not chosen</span>}</dd>
              </div>
              <div className="onboard-review-row">
                <dt>Ad platforms</dt>
                <dd>
                  {connectedAds.length > 0 ? (
                    connectedAds.map((platform) => platform.name).join(', ')
                  ) : (
                    <span className="muted">none connected</span>
                  )}
                </dd>
              </div>
              <div className="onboard-review-row">
                <dt>Guardrail</dt>
                <dd>{chosenGuardrail?.name ?? state.guardrail_label}</dd>
              </div>
            </dl>

            {state.completed ? (
              <p className="onboard-note">
                This workspace is already live. Launching again re-applies the
                guardrail and schedules every configured agent to run now.
              </p>
            ) : null}
          </>
        ) : null}
      </div>

      <div className="onboard-nav">
        <button
          type="button"
          className="btn btn-secondary"
          disabled={step === 0 || busy}
          onClick={() => void back()}
        >
          Back
        </button>
        <span className="onboard-count">
          Step {step + 1} of {STEPS.length}
        </span>
        {step === STEPS.length - 1 ? (
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || readOnly}
            onClick={() => void launch()}
          >
            {busy ? 'Launching…' : 'Launch workspace'}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void next()}
          >
            Continue
          </button>
        )}
      </div>
    </div>
  )
}
