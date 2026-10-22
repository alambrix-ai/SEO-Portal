/**
 * Workspace hub + per-workspace onboarding wizard.
 *
 * A workspace is one isolated site setup (agents, connectors, domain, data).
 * This page lists them, lets you add or remove one, and runs the four-step
 * setup form for the workspace that still needs launching.
 */
import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '@/api/client'
import { needsConfirmation } from '@/api/history'
import type { ConnectorOut, OnboardingOut, WorkspaceCard, WorkspacePolicyRequest } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useConfirm } from '@/components/Confirm'
import { ConnectDialog } from '@/components/ConnectDialog'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { Dialog, ErrorState, Loading } from '@/components/ui'
import { invalidateResourceCache, useResource } from '@/hooks/useResource'
import { PUBLISH_AUTOMATICALLY_CONFIRM, REVIEW_SETTING_NAME, isReviewSetting, reviewSettingName } from '@/lib/plain'

const STEPS = [
  { label: 'Your site', purpose: 'Domain and CMS' },
  { label: 'Ad platforms', purpose: 'Optional' },
  { label: REVIEW_SETTING_NAME, purpose: 'When agents ask you' },
  { label: 'Launch', purpose: 'Review and go live' },
] as const

function fallbackCards(session: NonNullable<ReturnType<typeof useAuth>['session']>): WorkspaceCard[] {
  return [
    {
      id: session.organization.id,
      name: session.organization.name,
      slug: session.organization.slug,
      primary_domain: session.organization.primary_domain,
      role: session.user.role,
      role_label: session.user.role_label,
      onboarding_complete: session.onboarding_complete,
      is_current: true,
      is_owner: session.user.is_owner,
    },
  ]
}

export function OnboardingPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const {
    session,
    refresh,
    patch,
    canWrite,
    createWorkspace,
    switchWorkspace,
    deleteWorkspace,
  } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const confirm = useConfirm()

  const orgId = session?.organization.id ?? ''
  const { data, loading, error, reload, set } = useResource(
    (signal) => api.onboarding(signal),
    [orgId],
    orgId ? `onboarding:${orgId}` : undefined,
  )
  // Prefetch so CMS / ad clicks open the connect dialog immediately.
  const connectors = useResource(
    (signal) => api.connectors('', signal),
    [orgId],
    orgId ? `connectors:${orgId}` : undefined,
  )

  const [step, setStep] = useState(0)
  const [busy, setBusy] = useState(false)
  const [creating, setCreating] = useState(searchParams.get('create') === '1')
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [configuring, setConfiguring] = useState<ConnectorOut | null>(null)
  const [touchedGuardrail, setTouchedGuardrail] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDomain, setNewDomain] = useState('')
  /** Force the wizard open (e.g. after create, or "Continue setup"). */
  const [setupOpen, setSetupOpen] = useState(true)
  /** Workspace list, shown on demand while a setup is in progress. */
  const [hubOpen, setHubOpen] = useState(false)
  const readOnly = !canWrite('onboarding')

  useEffect(() => {
    if (data) setStep(Math.min(data.step, STEPS.length - 1))
  }, [data])

  useEffect(() => {
    // Unfinished workspaces always open the form; live ones stay on cards.
    if (session && !session.onboarding_complete) setSetupOpen(true)
    else if (session?.onboarding_complete && data?.completed) setSetupOpen(false)
    setTouchedGuardrail(false)
  }, [session?.organization.id, session?.onboarding_complete, data?.completed])

  useEffect(() => {
    if (searchParams.get('create') === '1') {
      setCreating(true)
      setSearchParams({}, { replace: true })
    }
  }, [searchParams, setSearchParams])

  if (!session) return <Loading label="Loading…" />

  const workspaces =
    session.workspaces?.length > 0 ? session.workspaces : fallbackCards(session)

  const state = data
  const needsSetup = !session.onboarding_complete || (state != null && !state.completed)
  const showWizard = setupOpen || needsSetup

  const goDashboard = () => {
    navigate('/dashboard', { replace: true })
  }

  const onSwitchWorkspace = async (organizationId: string) => {
    if (organizationId === session.organization.id || busy) return
    setBusy(true)
    try {
      const next = await switchWorkspace(organizationId)
      push(`Switched to ${next.organization.name}`, 'success')
      invalidateResourceCache()
      if (next.onboarding_complete) {
        setSetupOpen(false)
        navigate('/dashboard', { replace: true })
      } else {
        setSetupOpen(true)
        setStep(0)
        await reload()
      }
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const onCreateWorkspace = async () => {
    if (!newName.trim()) {
      push('Give the workspace a name', 'warning')
      return
    }
    setBusy(true)
    try {
      const next = await createWorkspace({
        name: newName.trim(),
        primary_domain: newDomain.trim() || undefined,
      })
      setCreating(false)
      setNewName('')
      setNewDomain('')
      setSetupOpen(true)
      setStep(0)
      push(`Created ${next.organization.name} — finish setup below`, 'success')
      invalidateResourceCache()
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const onDeleteWorkspace = async (organizationId: string) => {
    if (busy) return
    setBusy(true)
    try {
      const removedName =
        workspaces.find((ws) => ws.id === organizationId)?.name ?? 'Workspace'
      const next = await deleteWorkspace(organizationId)
      setDeletingId(null)
      push(`${removedName} removed`, 'success')
      invalidateResourceCache()
      if (next.organization.id !== session.organization.id) {
        if (next.onboarding_complete) {
          setSetupOpen(false)
          navigate('/dashboard', { replace: true })
        } else {
          setSetupOpen(true)
          setStep(0)
          await reload()
        }
      }
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  /** Save part of the setup; true when the server accepted it. */
  const save = async (partial: Partial<OnboardingOut> & { step?: number; confirm?: boolean }): Promise<boolean> => {
    if (readOnly) {
      push('View-only access for your role', 'warning')
      return false
    }
    setBusy(true)
    try {
      set(
        await api.saveOnboarding({
          step: partial.step,
          domain: partial.domain,
          cms: partial.cms,
          guardrail: partial.guardrail,
          confirm: partial.confirm,
        }),
      )
      return true
    } catch (caught) {
      fromError(caught)
      return false
    } finally {
      setBusy(false)
    }
  }

  /**
   * Persist the review setting the same way Team & Access does: one policy
   * save (workspace choice + agents' switch) with a confirmation retry, then
   * the wizard record. Rolls back the caller on failure.
   */
  const persistGuardrail = async (value: string): Promise<boolean> => {
    if (readOnly) {
      push('View-only access for your role', 'warning')
      return false
    }
    if (!isReviewSetting(value)) {
      return save({ guardrail: value, confirm: value === 'full' })
    }
    const body: WorkspacePolicyRequest = {
      guardrail: value,
      autonomous: value !== 'human',
      confirm: value === 'full',
    }
    setBusy(true)
    try {
      try {
        await api.setWorkspacePolicy(body)
      } catch (caught) {
        if (body.confirm || !needsConfirmation(caught)) throw caught
        if (!(await confirm(PUBLISH_AUTOMATICALLY_CONFIRM))) return false
        await api.setWorkspacePolicy({ ...body, confirm: true })
      }
      // Policy already wrote the guardrail; keep the local choice even if
      // the wizard record fails to save a second time.
      try {
        set(
          await api.saveOnboarding({
            guardrail: value,
            confirm: value === 'full',
          }),
        )
      } catch (caught) {
        fromError(caught)
      }
      invalidateResourceCache('agents')
      void refresh()
      return true
    } catch (caught) {
      fromError(caught)
      return false
    } finally {
      setBusy(false)
    }
  }

  const openConnector = (slug: string, name: string) => {
    if (!slug || readOnly) return
    const cached = connectors.data?.find((connector) => connector.slug === slug)
    if (cached) {
      setConfiguring(cached)
      return
    }
    // Catalogue still loading — fetch this one without blocking the click path longer than needed.
    void (async () => {
      try {
        const list = connectors.data ?? (await api.connectors())
        const match = list.find((connector) => connector.slug === slug)
        if (!match) {
          push(`Could not find the ${name} integration`, 'warning')
          return
        }
        if (!connectors.data) connectors.set(list)
        setConfiguring(match)
      } catch (caught) {
        fromError(caught)
      }
    })()
  }

  const pickCms = (option: { slug: string; name: string }) => {
    if (!state || readOnly) {
      if (readOnly) push('View-only access for your role', 'warning')
      return
    }
    set({ ...state, cms: option.name })
    // Open config immediately; persist CMS choice in the background.
    if (option.slug) openConnector(option.slug, option.name)
    void save({ cms: option.name })
  }

  const pickAdPlatform = (platform: { slug: string; name: string }) => {
    if (readOnly) {
      push('View-only access for your role', 'warning')
      return
    }
    openConnector(platform.slug, platform.name)
  }

  const pickGuardrail = async (value: string) => {
    if (!state || readOnly) {
      if (readOnly) push('View-only access for your role', 'warning')
      return
    }
    // Turning on automatic publishing is the one choice that lets changes go
    // live unseen, so it is confirmed rather than applied on a single click.
    if (value === 'full' && state.guardrail !== 'full') {
      const ok = await confirm(PUBLISH_AUTOMATICALLY_CONFIRM)
      if (!ok) return
    }
    const wasTouched = touchedGuardrail
    setTouchedGuardrail(true)
    // Already saved as "full": nothing to send, and re-sending it would need
    // confirming again.
    if (value === 'full' && state.guardrail === 'full') return
    const previous = state
    set({ ...state, guardrail: value })
    // Policy first (same as Team & Access), then the wizard record. The
    // server refuses "full" without confirm; it is sent only after the
    // dialog above was accepted.
    const saved = await persistGuardrail(value)
    if (!saved) {
      // Show what is really saved, not the choice the server did not take.
      set(previous)
      setTouchedGuardrail(wasTouched)
    }
  }

  /** Tick = actually complete. Skipped / unfinished steps keep their number. */
  const isStepComplete = (index: number): boolean => {
    if (!state) return false
    if (index === 0) return Boolean(state.domain.trim() && state.cms)
    if (index === 1) return state.ad_platforms.some((platform) => platform.connected)
    if (index === 2) return touchedGuardrail || state.completed || step > 2
    if (index === 3) return state.completed
    return false
  }

  const next = async () => {
    if (!state || busy) return
    if (step === 0) {
      if (!state.domain.trim()) {
        push('Enter your website domain - everything else is scoped to it', 'warning')
        return
      }
      if (!state.cms.trim()) {
        push('Pick a content system, or choose Other', 'warning')
        return
      }
      const cmsOption = state.cms_options.find((option) => option.name === state.cms)
      if (cmsOption?.slug) {
        const match = connectors.data?.find((connector) => connector.slug === cmsOption.slug)
        if (!match?.connected) {
          // Refresh once if cache is stale / still loading.
          let live = match
          if (!live) {
            setBusy(true)
            try {
              const list = await api.connectors('CMS')
              connectors.set(
                [...(connectors.data ?? []).filter((c) => c.category !== 'CMS'), ...list],
              )
              live = list.find((connector) => connector.slug === cmsOption.slug)
            } catch (caught) {
              fromError(caught)
              return
            } finally {
              setBusy(false)
            }
          }
          if (!live?.connected) {
            push('Connect your content system to continue, or choose Skip for now', 'warning')
            openConnector(cmsOption.slug, cmsOption.name)
            return
          }
        }
      }
    }
    // Step 1 (ad platforms) is optional, so Continue always advances.
    if (step === 2) {
      if (!state.guardrail) {
        push('Choose when agents should ask you before publishing', 'warning')
        return
      }
      // The pre-selected (recommended) option is a valid choice; persist it.
      // "full" is only ever pre-selected when it is already saved (the server
      // never recommends it), so it is not re-sent without a confirmation.
      if (!touchedGuardrail) {
        setTouchedGuardrail(true)
        if (state.guardrail !== 'full') {
          const saved = await persistGuardrail(state.guardrail)
          if (!saved) {
            setTouchedGuardrail(false)
            return
          }
        }
      }
    }
    const target = Math.min(STEPS.length - 1, step + 1)
    setStep(target)
    await save({ step: target })
  }

  const skip = async () => {
    if (!state || busy) return
    // Skip alone advances without requiring connectors or fields.
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
    if (!state) return
    if (state.completed) {
      patch({ onboarding_complete: true })
      setSetupOpen(false)
      goDashboard()
      return
    }
    if (readOnly) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusy(true)
    try {
      fromResult(await api.completeOnboarding())
      patch({ onboarding_complete: true })
      set({ ...state, completed: true })
      invalidateResourceCache('dashboard')
      setSetupOpen(false)
      goDashboard()
      void refresh()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const connectedAds = state?.ad_platforms.filter((platform) => platform.connected) ?? []
  const chosenGuardrail = state?.guardrail_options.find(
    (option) => option.value === state.guardrail,
  )

  return (
    <div className="onboard">
      {showWizard && !hubOpen ? (
        <div className="onboard-context">
          <span>
            Setting up <strong>{session.organization.name}</strong>
          </span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setHubOpen(true)}>
            All workspaces ({workspaces.length})
          </button>
        </div>
      ) : null}

      {!showWizard || hubOpen ? (
      <section className="workspace-cards" aria-label="Your workspaces">
        <div className="workspace-cards-head">
          <div>
            <h2 className="workspace-cards-title">Workspaces</h2>
            <p className="workspace-cards-lede">
              Each workspace is one site with its own domain, agents, integrations and data.
            </p>
          </div>
          {showWizard ? (
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setHubOpen(false)}>
              Back to setup
            </button>
          ) : null}
        </div>
        <div className="workspace-cards-grid">
          {workspaces.map((ws) => {
            const canDelete = ws.is_owner && workspaces.length > 1
            return (
              <div
                key={ws.id}
                className={`workspace-card${ws.is_current ? ' is-current' : ''}`}
              >
                <button
                  type="button"
                  className="workspace-card-main"
                  disabled={busy || ws.is_current}
                  onClick={() => void onSwitchWorkspace(ws.id)}
                >
                  <div className="workspace-card-kicker">
                    {ws.is_current ? 'Current' : ws.role_label}
                  </div>
                  <div className="workspace-card-name">{ws.name}</div>
                  <div className="workspace-card-meta">
                    {ws.primary_domain || ws.slug}
                  </div>
                  <div className="workspace-card-status">
                    {ws.onboarding_complete ? 'Live' : 'Setup in progress'}
                  </div>
                </button>
                {canDelete ? (
                  <button
                    type="button"
                    className="workspace-card-delete"
                    disabled={busy}
                    aria-label={`Delete ${ws.name}`}
                    title="Delete workspace"
                    onClick={(event) => {
                      event.stopPropagation()
                      setDeletingId(ws.id)
                    }}
                  >
                    <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
                      <path
                        fill="currentColor"
                        d="M6 2h4l.5 1H14v1.5H2V3h3.5L6 2zm1 4v6H6V6h1zm3 0v6H9V6h1zM3.5 5H12.5l-.7 9.2A1.5 1.5 0 0 1 10.3 15.5H5.7a1.5 1.5 0 0 1-1.5-1.3L3.5 5z"
                      />
                    </svg>
                  </button>
                ) : null}
              </div>
            )
          })}
          <button
            type="button"
            className="workspace-card workspace-card-create"
            disabled={busy}
            onClick={() => setCreating(true)}
            aria-label="Add workspace"
          >
            <span className="workspace-card-plus" aria-hidden="true">
              +
            </span>
            <span className="workspace-card-create-label">Add workspace</span>
          </button>
        </div>
      </section>
      ) : null}

      {!showWizard && state?.completed ? (
        <section className="onboard-live card">
          <h2 className="onboard-h">
            {session.organization.name} is live
          </h2>
          <p className="onboard-lede">
            Domain {state.domain || session.organization.primary_domain || '—'} ·{' '}
            {REVIEW_SETTING_NAME}: {reviewSettingName(state.guardrail)}.
          </p>
          <div className="onboard-live-actions">
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => {
                setSetupOpen(true)
                setStep(STEPS.length - 1)
              }}
            >
              Review setup
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={goDashboard}
            >
              Go to overview
            </button>
          </div>
        </section>
      ) : null}

      {showWizard ? (
        <>
          {loading && !state ? (
            <Loading label="Loading workspace setup…" />
          ) : error && !state ? (
            <ErrorState message={error} onRetry={reload} />
          ) : state ? (
            <>
              <header className="onboard-hero">
                <h1 className="onboard-title">
                  Set up {session.organization.name}
                </h1>
                <p className="onboard-sub">
                  Nothing touches your site until you launch, and then only as your
                  review setting allows.
                </p>
              </header>

              <nav className="onboard-rail" aria-label="Workspace setup progress">
                {STEPS.map(({ label, purpose }, index) => {
                  const done = isStepComplete(index)
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
                      title={done ? 'Complete' : 'Not complete yet — you can skip and come back'}
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
                      Everything in this workspace is scoped to this domain. Select your
                      content system to connect it.
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
                        onChange={(event) =>
                          set({ ...state, domain: event.target.value })
                        }
                        onBlur={(event) => void save({ domain: event.target.value })}
                      />
                      <div className="field-hint">
                        Just the domain - no https:// and no trailing path.
                      </div>
                    </div>

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
                            disabled={readOnly || busy}
                            onClick={() => pickCms(option)}
                          >
                            {option.slug ? (
                              <ConnectorIcon
                                slug={option.slug}
                                name={option.name}
                                size={26}
                              />
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
                        Selecting a system opens its connection form. Choose Skip to connect it later.
                      </div>
                    </div>
                  </>
                ) : null}

                {step === 1 ? (
                  <>
                    <h2 className="onboard-h">Ad platforms</h2>
                    <p className="onboard-lede">
                      Optional. Connect the platforms you advertise on. SEO agents work
                      without them.
                    </p>

                    <div className="onboard-choices is-wide">
                      {state.ad_platforms.map((platform) => (
                        <button
                          key={platform.slug}
                          type="button"
                          className={`onboard-choice${
                            platform.connected ? ' is-picked' : ''
                          }`}
                          disabled={readOnly || busy}
                          onClick={() => pickAdPlatform(platform)}
                        >
                          <ConnectorIcon
                            slug={platform.slug}
                            name={platform.name}
                            size={26}
                          />
                          <span className="onboard-choice-name">{platform.name}</span>
                          <span
                            className={`onboard-choice-state${
                              platform.connected ? ' is-on' : ''
                            }`}
                          >
                            {platform.connected ? 'Connected' : 'Connect'}
                          </span>
                        </button>
                      ))}
                    </div>

                    <p className="onboard-note">
                      {connectedAds.length === 0
                        ? 'None connected. You can add them any time from Integrations.'
                        : `${connectedAds.length} connected.`}
                    </p>
                  </>
                ) : null}

                {step === 2 ? (
                  <>
                    <h2 className="onboard-h">{REVIEW_SETTING_NAME}</h2>
                    <p className="onboard-lede">
                      Choose when agents ask you before a change goes live on your site.
                      Applies to every agent in this workspace. You can change it later.
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
                            onClick={() => void pickGuardrail(option.value)}
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
                      Launching applies your review setting and schedules configured agents.
                      Agents you have not set up stay paused.
                    </p>

                    <dl className="onboard-review">
                      <div className="onboard-review-row">
                        <dt>Domain</dt>
                        <dd>
                          {state.domain || <span className="muted">not set</span>}
                        </dd>
                      </div>
                      <div className="onboard-review-row">
                        <dt>Content system</dt>
                        <dd>
                          {state.cms || <span className="muted">not chosen</span>}
                        </dd>
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
                        <dt>{REVIEW_SETTING_NAME}</dt>
                        <dd>{chosenGuardrail?.name ?? reviewSettingName(state.guardrail)}</dd>
                      </div>
                    </dl>

                    {state.completed ? (
                      <p className="onboard-note">
                        This workspace is already live. Launching again re-applies
                        your review setting and schedules configured agents now.
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
                <div className="onboard-nav-end">
                  {step === 0 ? (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={busy}
                      onClick={() => void skip()}
                    >
                      Skip
                    </button>
                  ) : null}
                  {step === STEPS.length - 1 ? (
                    <button
                      type="button"
                      className="btn btn-primary onboard-launch"
                      disabled={busy || (readOnly && !state.completed)}
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
            </>
          ) : null}
        </>
      ) : null}

      {creating ? (
        <Dialog
          title="Add workspace"
          onClose={() => !busy && setCreating(false)}
          width={420}
          actions={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => setCreating(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void onCreateWorkspace()}
              >
                {busy ? 'Creating…' : 'Create & set up'}
              </button>
            </>
          }
        >
          <p className="small muted" style={{ marginTop: 0 }}>
            Starts a fresh workspace with its own agents, connectors, and
            onboarding. Your other workspaces stay untouched.
          </p>
          <label className="field">
            <span>Workspace name</span>
            <input
              className="input"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Acme Marketing"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') void onCreateWorkspace()
              }}
            />
          </label>
          <label className="field">
            <span>Primary domain (optional)</span>
            <input
              className="input"
              value={newDomain}
              onChange={(e) => setNewDomain(e.target.value)}
              placeholder="acme.com"
              onKeyDown={(e) => {
                if (e.key === 'Enter') void onCreateWorkspace()
              }}
            />
          </label>
        </Dialog>
      ) : null}

      {deletingId ? (
        <Dialog
          title="Delete workspace"
          onClose={() => !busy && setDeletingId(null)}
          width={400}
          actions={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => setDeletingId(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void onDeleteWorkspace(deletingId)}
              >
                {busy ? 'Deleting…' : 'Delete'}
              </button>
            </>
          }
        >
          <p className="small muted" style={{ marginTop: 0 }}>
            Remove{' '}
            <strong>
              {workspaces.find((ws) => ws.id === deletingId)?.name ?? 'this workspace'}
            </strong>
            ? It will disappear from your account. Other workspaces stay available.
          </p>
        </Dialog>
      ) : null}

      {configuring ? (
        <ConnectDialog
          key={configuring.slug}
          connector={configuring}
          onClose={() => setConfiguring(null)}
          onConnected={async () => {
            const name = configuring.name
            setConfiguring(null)
            invalidateResourceCache('connectors')
            try {
              connectors.set(await api.connectors())
            } catch {
              /* catalogue refresh is best-effort */
            }
            await reload()
            push(`${name} ready for this workspace`, 'success')
          }}
        />
      ) : null}
    </div>
  )
}
