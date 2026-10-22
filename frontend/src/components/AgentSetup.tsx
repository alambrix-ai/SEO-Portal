/**
 * Guided setup for one agent, written for people who are not technical.
 *
 * Three steps: what it needs (and connect it right here), its settings in
 * plain words, then how changes are approved. Everything the steps say about
 * the agent — what it does, what it changes, what it needs, what each
 * approval choice will do — comes from the API, which derives it from the
 * agent's own code, so the screen cannot promise something the agent won't do.
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type { AgentNeed, AgentOut, ConnectorOut, LlmConnectorOut, NeedUse, NotifyChannelOut } from '@/api/types'
import { AgentIcon } from '@/components/AgentIcon'
import { useConfirm } from '@/components/Confirm'
import { ConnectDialog } from '@/components/ConnectDialog'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { PracticeToggle } from '@/components/SafetyBits'
import { useToasts } from '@/components/Toasts'
import { CheckIcon } from '@/components/icons'
import { Drawer, Field } from '@/components/ui'
import {
  controlUses,
  initialSelections,
  joinNames,
  needsOne,
  toSaved,
  useStatement,
} from '@/lib/connectorUse'
import {
  NOTIFY_LABEL,
  PUBLISH_AUTOMATICALLY_CONFIRM,
  REVIEW_SETTING_NAME,
  reviewSettingName,
  scheduleLabel,
} from '@/lib/plain'

const STEPS = ['What it needs', 'Settings', 'Review & start'] as const

/** Integrations shown per need before "see all". */
const OPTIONS_SHOWN = 4

export function AgentSetup({
  agent: initial,
  schedules,
  channels,
  llmConnectors: initialModels,
  onClose,
  onSaved,
  onOpenAgent,
}: {
  agent: AgentOut
  schedules: string[]
  channels: NotifyChannelOut[]
  llmConnectors: LlmConnectorOut[]
  onClose: () => void
  onSaved: (started: boolean) => Promise<void>
  /** Switch the panel to another agent (a prerequisite). */
  onOpenAgent: (slug: string) => void
}) {
  const { push, fromError } = useToasts()
  const confirm = useConfirm()

  const [agent, setAgent] = useState(initial)
  const [models, setModels] = useState(initialModels)
  const [notifyOptions, setNotifyOptions] = useState(channels)
  const [step, setStep] = useState(0)
  const [connecting, setConnecting] = useState<ConnectorOut | null>(null)
  const [opening, setOpening] = useState<string | null>(null)

  const [schedule, setSchedule] = useState(agent.schedule)
  const [scope, setScope] = useState(agent.scope)
  const deadChannel = channels.find((c) => c.value === agent.notify_channel && !c.available)
  const [notify, setNotify] = useState(deadChannel ? 'None' : agent.notify_channel)
  const firstModel = initialModels.find((m) => m.available)?.slug ?? ''
  const chosenModelOk = initialModels.some((m) => m.slug === agent.llm_connector && m.available)
  const [model, setModel] = useState(chosenModelOk ? agent.llm_connector : firstModel)
  const [maxActions, setMaxActions] = useState(String(agent.max_actions_per_day))
  const [autonomy, setAutonomy] = useState(agent.autonomy)
  // Spending agents: a new configuration starts in practice mode.
  const [practice, setPractice] = useState(agent.configured ? Boolean(agent.practice_mode) : true)
  /** "Always use the model I picked": a run is skipped rather than written with another model. */
  const [strictModel, setStrictModel] = useState(Boolean(agent.strict_model))
  const [extra, setExtra] = useState<Record<string, string>>(() =>
    Object.fromEntries((agent.settings_fields ?? []).map((field) => [field.key, field.value])),
  )
  /** Which connectors to use, per capability, as in the form ([] = automatic). */
  const [selections, setSelections] = useState<Record<string, string[]>>(() => initialSelections(initial))
  const choose = (capability: string, value: string[]) =>
    setSelections((current) => ({ ...current, [capability]: value }))
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState(false)

  const running = agent.status === 'running'
  const required = agent.needs.filter((n) => !n.optional)
  const scopeNeed = agent.needs.find((n) => n.kind === 'setting')
  // The setting need is met by what is typed in step 2, not by connecting.
  const scopeFilled = !scopeNeed || scopeNeed.satisfied || scope.trim().length > 0
  const missing = required.filter((n) => n.kind === 'integration' && !n.satisfied)
  // Data another agent collects is not a reason to block starting: the run
  // simply waits until those pages exist. It is said, not enforced.
  const waitingOn = required.find((n) => n.kind === 'agent' && !n.satisfied)
  const canStart = missing.length === 0 && scopeFilled
  /** Everything still to do before it can start, in plain words. */
  const todo = [
    ...missing.map((n) => `connect something that can ${n.label.charAt(0).toLowerCase()}${n.label.slice(1)}`),
    ...(scopeNeed && !scopeFilled ? [`enter the ${scopeNeed.label.toLowerCase()} in Settings`] : []),
  ]
  const connectedModels = models.filter((m) => m.available)
  const modelMissing = agent.requires_llm && !connectedModels.some((m) => m.slug === model)
  const modelName = models.find((m) => m.slug === model)?.name ?? ''
  /** Every connector control on the needs, once each. */
  const controls = controlUses(agent)
  /** A "which ones" control with nothing ticked. */
  const emptyControl = controls.find((use) => needsOne(use, selections[use.capability] ?? []))

  /** After connecting something, re-read what the agent needs. */
  const refresh = async () => {
    try {
      const [fresh, options] = await Promise.all([api.agent(agent.slug), api.agentOptions()])
      setAgent(fresh)
      // Keep what has been picked in the form; add controls that just appeared.
      setSelections((current) => ({ ...initialSelections(fresh), ...current }))
      setModels(options.llm_connectors)
      setNotifyOptions(options.notify_channels)
      if (agent.requires_llm && !options.llm_connectors.some((m) => m.slug === model && m.available)) {
        setModel(options.llm_connectors.find((m) => m.available)?.slug ?? '')
      }
    } catch (caught) {
      fromError(caught)
    }
  }

  // A sign-in finished while this was open (see OAuthReturn): refresh.
  useEffect(() => {
    const onChange = () => void refresh()
    window.addEventListener('connectors:changed', onChange)
    return () => window.removeEventListener('connectors:changed', onChange)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const openConnector = async (slug: string) => {
    setOpening(slug)
    try {
      const list = await api.connectors()
      const match = list.find((c) => c.slug === slug)
      if (match) setConnecting(match)
    } catch (caught) {
      fromError(caught)
    } finally {
      setOpening(null)
    }
  }

  const remove = async () => {
    const ok = await confirm({
      title: `Remove ${agent.name} settings?`,
      message: 'This clears its settings and stops the agent.',
      detail: 'You can set it up again at any time. Its history is kept.',
      confirmLabel: 'Remove settings',
      tone: 'danger',
      icon: <AgentIcon slug={agent.slug} size={40} />,
    })
    if (!ok) return
    setRemoving(true)
    try {
      await api.resetAgentConfig(agent.slug)
      push(`${agent.name} settings removed`, 'info')
      await onSaved(false)
    } catch (caught) {
      fromError(caught)
    } finally {
      setRemoving(false)
    }
  }

  const validSettings = (): boolean => {
    if (modelMissing) {
      push('Choose which AI service this agent should write with.', 'warning')
      setStep(0)
      return false
    }
    if (emptyControl) {
      push(`Tick at least one: ${emptyControl.question}`, 'warning')
      setStep(0)
      return false
    }
    if (scopeNeed && !scopeFilled) {
      push(`Enter the ${scopeNeed.label.toLowerCase()} — this agent will not run without it.`, 'warning')
      setStep(1)
      return false
    }
    const parsed = Number(maxActions)
    if (agent.makes_changes && (!Number.isFinite(parsed) || parsed < 1)) {
      push('The daily limit must be at least 1.', 'warning')
      setStep(1)
      return false
    }
    return true
  }

  const save = async (andStart: boolean) => {
    if (!validSettings()) return
    setBusy(true)
    try {
      const saved = await api.configureAgent(agent.slug, {
        schedule,
        scope,
        notify_channel: notify,
        llm_connector: agent.requires_llm ? model : '',
        max_actions_per_day: Math.max(1, Math.floor(Number(maxActions) || agent.max_actions_per_day)),
        settings: extra,
        ...(agent.spending ? { practice_mode: practice } : {}),
        ...(agent.requires_llm ? { strict_model: strictModel } : {}),
        connectors: Object.fromEntries(
          controls.map((use) => [use.capability, toSaved(use, selections[use.capability] ?? [])]),
        ),
      })
      if (agent.makes_changes && autonomy !== agent.autonomy) {
        await api.setAgentAutonomy(agent.slug, autonomy)
      }
      if (andStart && !running) {
        await api.resumeAgent(agent.slug)
        push(`${agent.name} is set up and running`, 'success')
      } else {
        push('Settings saved', 'success')
      }
      for (const warning of saved.warnings ?? []) push(warning, 'warning')
      await onSaved(andStart)
    } catch (caught) {
      // Most likely an invalid value in the scope box; the message says why.
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  /** Under "Publish automatically", following the workspace means publishing unseen: confirm it. */
  const followWorkspace = async () => {
    if (autonomy) return
    if (agent.guardrail === 'full') {
      const ok = await confirm(PUBLISH_AUTOMATICALLY_CONFIRM)
      if (!ok) return
    }
    setAutonomy(true)
  }

  const isLast = step === STEPS.length - 1

  return (
    <>
      <Drawer
        title={agent.configured ? `${agent.name} settings` : `Set up ${agent.name}`}
        subtitle={`Step ${step + 1} of ${STEPS.length} · ${STEPS[step]}`}
        icon={<AgentIcon slug={agent.slug} size={36} />}
        width={560}
        onClose={onClose}
        actions={
          <>
            {step > 0 ? (
              <button type="button" className="btn btn-secondary" onClick={() => setStep(step - 1)}>
                Back
              </button>
            ) : agent.configured ? (
              <button
                type="button"
                className="btn btn-ghost danger"
                disabled={busy || removing}
                onClick={() => void remove()}
              >
                {removing ? 'Removing…' : 'Remove settings'}
              </button>
            ) : null}
            <div className="dialog-actions-spacer" />
            {!isLast ? (
              <button type="button" className="btn btn-primary" onClick={() => setStep(step + 1)}>
                Continue
              </button>
            ) : running ? (
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void save(false)}>
                {busy ? 'Saving…' : 'Save changes'}
              </button>
            ) : (
              <>
                <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void save(false)}>
                  Save for later
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy || !canStart}
                  title={canStart ? undefined : 'Connect everything in step 1 first'}
                  onClick={() => void save(true)}
                >
                  {busy ? 'Saving…' : 'Save and start'}
                </button>
              </>
            )}
          </>
        }
      >
        <ol className="setup-progress" aria-label="Setup steps">
          {STEPS.map((label, index) => (
            <li key={label}>
              <button
                type="button"
                className={`setup-progress-step${index === step ? ' is-current' : ''}${
                  index < step ? ' is-done' : ''
                }`}
                onClick={() => setStep(index)}
                aria-current={index === step ? 'step' : undefined}
              >
                <span className="setup-progress-mark">{index < step ? <CheckIcon size={12} /> : index + 1}</span>
                {label}
              </button>
            </li>
          ))}
        </ol>

        {step === 0 ? (
          <>
            <section className="guide-block">
              <div className="guide-title">What it does</div>
              <p className="guide-text">{agent.summary || agent.description}</p>
              {agent.effect ? (
                <>
                  <div className="guide-title">{agent.makes_changes ? 'What it changes' : 'Changes to your site'}</div>
                  <p className="guide-text">{agent.effect}</p>
                </>
              ) : null}
            </section>

            <section className="guide-block">
              <div className="guide-title">What it needs</div>
              {missing.length === 0 && !waitingOn ? (
                <div className="banner is-success">Everything this agent needs is connected.</div>
              ) : missing.length === 0 && waitingOn ? (
                <div className="banner is-info">
                  Its integrations are connected. It also works on pages that{' '}
                  {waitingOn.options[0]?.name ?? 'another agent'} collects — see below.
                </div>
              ) : (
                <p className="guide-text muted">
                  {todo.length === 1 ? 'One thing is' : `${todo.length} things are`} still needed, marked below.
                  You can connect integrations here without leaving setup.
                </p>
              )}
              <ul className="needs-list">
                {agent.needs.map((need) => (
                  <NeedRow
                    key={need.key}
                    need={need}
                    selections={selections}
                    onChoose={choose}
                    models={models}
                    model={model}
                    onModel={setModel}
                    scopeFilled={scopeFilled}
                    opening={opening}
                    onConnect={(slug) => void openConnector(slug)}
                    onOpenAgent={onOpenAgent}
                    onGoToSettings={() => setStep(1)}
                  />
                ))}
              </ul>
              {agent.requires_llm ? (
                <div className="choice-list" style={{ marginTop: 'var(--space-3)' }}>
                  <StrictModelToggle checked={strictModel} onChange={setStrictModel} modelName={modelName} />
                </div>
              ) : null}
            </section>
          </>
        ) : null}

        {step === 1 ? (
          <>
            <fieldset className="choice-group">
              <legend className="guide-title">How often should it run?</legend>
              <div className="choice-row">
                {(schedules.length ? schedules : [agent.schedule]).map((option) => (
                  <label key={option} className={`choice-pill${schedule === option ? ' is-picked' : ''}`}>
                    <input
                      type="radio"
                      name="schedule"
                      value={option}
                      checked={schedule === option}
                      onChange={() => setSchedule(option)}
                    />
                    {scheduleLabel(option)}
                  </label>
                ))}
              </div>
            </fieldset>

            {agent.uses_scope !== false ? (
            <Field
              label={agent.scope_label || 'Limit to (optional)'}
              htmlFor="setup-scope"
              required={Boolean(scopeNeed)}
              hint={agent.scope_help || undefined}
            >
              <input
                id="setup-scope"
                className="input"
                value={scope}
                placeholder={agent.scope_placeholder}
                onChange={(event) => setScope(event.target.value)}
              />
            </Field>
            ) : null}

            {(agent.settings_fields ?? []).map((field) => (
              <Field
                key={field.key}
                label={field.label}
                htmlFor={`setup-setting-${field.key}`}
                hint={[field.help, field.default ? `Default: ${field.default}.` : ''].filter(Boolean).join(' ')}
              >
                <input
                  id={`setup-setting-${field.key}`}
                  className={`input${field.kind === 'text' ? '' : ' input-narrow'}`}
                  inputMode={field.kind === 'integer' ? 'numeric' : field.kind === 'number' ? 'decimal' : undefined}
                  placeholder={field.default}
                  value={extra[field.key] ?? ''}
                  onChange={(event) => setExtra((current) => ({ ...current, [field.key]: event.target.value }))}
                />
              </Field>
            ))}

            <Field
              label="Tell me when it does something"
              htmlFor="setup-notify"
              hint={
                notifyOptions.find((c) => c.value === notify)?.reason ||
                (notifyOptions.some((c) => !c.available)
                  ? 'Greyed-out options need a notification integration first.'
                  : undefined)
              }
            >
              <select
                id="setup-notify"
                className="input"
                value={notify}
                onChange={(event) => setNotify(event.target.value)}
              >
                {(notifyOptions.length ? notifyOptions : [{ value: agent.notify_channel, available: true, reason: '' }]).map(
                  (option) => (
                    <option key={option.value} value={option.value} disabled={!option.available}>
                      {NOTIFY_LABEL[option.value] ?? option.value}
                      {option.available ? '' : ' (not connected)'}
                    </option>
                  ),
                )}
              </select>
            </Field>

            {agent.makes_changes ? (
              <Field
                label="Most automatic changes per day"
                htmlFor="setup-max"
                hint="Only changes that go live automatically count. Changes waiting for your approval do not. Extra changes are tried again on the next run."
              >
                <input
                  id="setup-max"
                  className="input input-narrow"
                  type="number"
                  min={1}
                  value={maxActions}
                  onChange={(event) => setMaxActions(event.target.value)}
                />
              </Field>
            ) : null}
          </>
        ) : null}

        {step === 2 ? (
          <>
            {agent.makes_changes ? (
              <fieldset className="choice-group">
                <legend className="guide-title">{REVIEW_SETTING_NAME}</legend>
                <div className="choice-list">
                  <label className={`choice-card is-tall${!autonomy ? ' is-picked' : ''}`}>
                    <input type="radio" name="autonomy" checked={!autonomy} onChange={() => setAutonomy(false)} />
                    <span className="choice-card-body">
                      <span className="choice-card-title">Always ask me</span>
                      <span className="choice-card-text">{agent.approval_if_review}</span>
                    </span>
                  </label>
                  <label className={`choice-card is-tall${autonomy ? ' is-picked' : ''}`}>
                    <input type="radio" name="autonomy" checked={autonomy} onChange={() => void followWorkspace()} />
                    <span className="choice-card-body">
                      <span className="choice-card-title">
                        {agent.guardrail
                          ? `Use the workspace setting (${reviewSettingName(agent.guardrail)})`
                          : 'Use the workspace setting'}
                      </span>
                      <span className="choice-card-text">{agent.approval_if_auto}</span>
                    </span>
                  </label>
                </div>
                {agent.automatic_note ? (
                  <div className="banner is-info">
                    {agent.automatic_note} Until then, every change it makes waits for your approval.
                  </div>
                ) : null}
                {agent.spending ? (
                  <div className="choice-list" style={{ marginTop: 'var(--space-3)' }}>
                    <PracticeToggle checked={practice} onChange={setPractice} source={agent.practice_source} />
                  </div>
                ) : null}
                {agent.guardrail ? (
                  <p className="guide-text muted small">
                    Workspace setting: {REVIEW_SETTING_NAME} is "{reviewSettingName(agent.guardrail)}". Changes
                    waiting for your approval appear on the <Link to="/approvals">Approvals</Link> screen.
                  </p>
                ) : null}
              </fieldset>
            ) : (
              <div className="banner is-success">
                This agent only reports. It never changes your website, so there is nothing to approve.
              </div>
            )}

            <section className="guide-block">
              <div className="guide-title">Summary</div>
              <dl className="summary-list">
                <div>
                  <dt>Runs</dt>
                  <dd>{scheduleLabel(schedule)}</dd>
                </div>
                {agent.requires_llm ? (
                  <div>
                    <dt>Writes with</dt>
                    <dd>
                      {modelName || 'Not chosen'}
                      {modelName && strictModel ? ' (only this one)' : ''}
                    </dd>
                  </div>
                ) : null}
                {controls.map((use) => (
                  <div key={use.capability}>
                    <dt>{use.question}</dt>
                    <dd>{summaryOf(use, selections[use.capability] ?? [])}</dd>
                  </div>
                ))}
                {agent.uses_scope !== false ? (
                  <div>
                    <dt>{(agent.scope_label || 'Limit to').replace(/ \((optional|required)\)$/, '')}</dt>
                    <dd>{scope.trim() || (scopeNeed ? 'Not entered' : 'Everything')}</dd>
                  </div>
                ) : null}
                <div>
                  <dt>Notifications</dt>
                  <dd>{NOTIFY_LABEL[notify] ?? notify}</dd>
                </div>
              </dl>
            </section>

            {canStart && waitingOn && !running ? (
              <div className="banner is-info">
                You can start it now. It will begin working once {waitingOn.options[0]?.name ?? 'the other agent'} has
                collected your pages.
              </div>
            ) : null}

            {!canStart && !running ? (
              <div className="banner is-warning">
                Before it can start: {todo.join('; ')}. You can save now and start later.
              </div>
            ) : null}
          </>
        ) : null}
      </Drawer>

      {connecting ? (
        <ConnectDialog
          key={connecting.slug}
          connector={connecting}
          onClose={() => setConnecting(null)}
          onConnected={async () => {
            setConnecting(null)
            await refresh()
          }}
        />
      ) : null}
    </>
  )
}

/** The "don't switch models" choice, in plain words. */
function StrictModelToggle({
  checked,
  onChange,
  modelName,
}: {
  checked: boolean
  onChange: (on: boolean) => void
  modelName: string
}) {
  const chosen = modelName || 'the AI service you picked'
  return (
    <label className={`choice-card is-tall${checked ? ' is-picked' : ''}`}>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span className="choice-card-body">
        <span className="choice-card-title">Always use the model I picked (don&rsquo;t switch automatically)</span>
        <span className="choice-card-text">
          {checked
            ? `This agent only writes with ${chosen}. If it isn't connected when a run is due, the run is skipped and you're told why.`
            : `If ${chosen} isn't available, the agent uses another connected AI service so its work still gets done.`}
        </span>
      </span>
    </label>
  )
}

/** The summary line for one control: the names in use, in a few words. */
function summaryOf(use: NeedUse, value: string[]): string {
  const names = value.map((slug) => use.options.find((o) => o.slug === slug)?.name ?? slug)
  const connected = use.options.filter((o) => o.connected && !o.locked)
  if (use.mode === 'one') return names.length ? joinNames(names) : 'Automatic'
  if (connected.length > 0 && names.length === connected.length) return `All connected (${joinNames(names)})`
  return joinNames(names) || 'None'
}

function NeedRow({
  need,
  selections,
  onChoose,
  models,
  model,
  onModel,
  scopeFilled,
  opening,
  onConnect,
  onOpenAgent,
  onGoToSettings,
}: {
  need: AgentNeed
  /** Connector selections as they stand in the form. */
  selections: Record<string, string[]>
  onChoose: (capability: string, value: string[]) => void
  /** AI model options and the one picked in the form, for the AI need. */
  models: LlmConnectorOut[]
  model: string
  onModel: (slug: string) => void
  scopeFilled: boolean
  opening: string | null
  onConnect: (slug: string) => void
  onOpenAgent: (slug: string) => void
  onGoToSettings: () => void
}) {
  const [showAll, setShowAll] = useState(false)
  const done = need.kind === 'setting' ? need.satisfied || scopeFilled : need.satisfied
  const connected = need.options.filter((o) => o.connected)
  const others = need.options.filter((o) => !o.connected)
  const visible = showAll ? others : others.slice(0, OPTIONS_SHOWN)
  const uses = need.uses ?? []
  const hasControl = uses.some((use) => use.primary && use.control)
  const isModel = need.key === 'ai_model'

  return (
    <li className={`need-row${done ? ' is-done' : need.optional ? ' is-optional' : ' is-missing'}`}>
      <span className="need-mark" aria-hidden="true">
        {done ? <CheckIcon size={12} /> : need.optional ? '+' : '!'}
      </span>
      <div className="need-body">
        <div className="need-label">
          {need.label}
          {need.optional ? <span className="need-tag">Optional</span> : null}
        </div>

        {need.kind === 'integration' ? (
          done ? (
            <>
              {isModel ? (
                <ModelControl models={models} model={model} onModel={onModel} />
              ) : null}
              {uses.map((use, index) =>
                use.primary && use.control ? (
                  <UseControl
                    key={`${use.capability}:${index}`}
                    use={use}
                    value={selections[use.capability] ?? use.selected}
                    onChange={(value) => onChoose(use.capability, value)}
                  />
                ) : use.statement || use.note ? (
                  <div key={`${use.capability}:${index}`} className="need-detail">
                    {[use.statement, use.note].filter(Boolean).join(' ')}
                  </div>
                ) : null,
              )}
              {!isModel && !hasControl && !uses.some((u) => u.statement || u.note) ? (
                <div className="need-detail">Connected: {connected.map((o) => o.name).join(', ')}</div>
              ) : null}
            </>
          ) : (
            <>
              {need.note && !isModel ? <div className="need-detail">{need.note}</div> : null}
              <div className="need-detail">Connect any one of these:</div>
              <div className="need-options">
                {visible.map((option) => (
                  <button
                    key={option.slug}
                    type="button"
                    className="need-option"
                    disabled={opening !== null}
                    onClick={() => onConnect(option.slug)}
                  >
                    <ConnectorIcon slug={option.slug} name={option.name} size={18} />
                    {opening === option.slug ? 'Opening…' : option.name}
                  </button>
                ))}
                {!showAll && others.length > OPTIONS_SHOWN ? (
                  <button type="button" className="need-more" onClick={() => setShowAll(true)}>
                    +{others.length - OPTIONS_SHOWN} more
                  </button>
                ) : null}
              </div>
            </>
          )
        ) : null}

        {need.kind === 'agent' ? (
          done ? (
            <div className="need-detail">Pages have been collected.</div>
          ) : (
            <>
              <div className="need-detail">{need.note}</div>
              {need.options[0] ? (
                <button
                  type="button"
                  className="btn btn-secondary btn-sm need-action"
                  onClick={() => onOpenAgent(need.options[0]!.slug)}
                >
                  Set up {need.options[0].name}
                </button>
              ) : null}
            </>
          )
        ) : null}

        {need.kind === 'setting' ? (
          done ? (
            <div className="need-detail">Entered.</div>
          ) : (
            <button type="button" className="btn btn-secondary btn-sm need-action" onClick={onGoToSettings}>
              Enter it in Settings
            </button>
          )
        ) : null}
      </div>
    </li>
  )
}

/** The AI model this agent writes with: always a visible choice of the connected ones. */
function ModelControl({
  models,
  model,
  onModel,
}: {
  models: LlmConnectorOut[]
  model: string
  onModel: (slug: string) => void
}) {
  const shown = models.filter((m) => m.available || m.locked)
  const current = models.find((m) => m.slug === model)
  return (
    <div className="need-pick">
      <div className="need-pick-row">
        {current ? <ConnectorIcon slug={current.slug} name={current.name} size={20} /> : null}
        <select
          className="input input-compact"
          aria-label="Which AI should it write with?"
          value={model}
          onChange={(event) => onModel(event.target.value)}
        >
          {shown.map((option) => (
            <option key={option.slug} value={option.slug} disabled={!option.available}>
              {option.name}
              {option.locked ? ` — ${option.locked_reason || 'not in your plan'}` : ''}
            </option>
          ))}
        </select>
      </div>
      <div className="need-detail">{current ? `Uses ${current.name}.` : 'Choose one.'}</div>
    </div>
  )
}

/** One connector control: a select for "which one", checkboxes for "which ones". */
function UseControl({
  use,
  value,
  onChange,
}: {
  use: NeedUse
  value: string[]
  onChange: (value: string[]) => void
}) {
  const sentence = useStatement(use, value)
  const empty = needsOne(use, value)
  const stale = use.stale && sentence === use.statement

  if (use.control === 'single') {
    const current = use.options.find((o) => o.slug === value[0])
    return (
      <div className="need-pick">
        <div className="need-pick-question">{use.question}</div>
        <div className="need-pick-row">
          {current ? <ConnectorIcon slug={current.slug} name={current.name} size={20} /> : null}
          <select
            className="input input-compact"
            aria-label={use.question}
            value={value[0] ?? ''}
            onChange={(event) => onChange(event.target.value ? [event.target.value] : [])}
          >
            {use.automatic_option ? <option value="">Automatic</option> : null}
            {use.options.map((option) => (
              <option key={option.slug} value={option.slug} disabled={option.locked || !option.connected}>
                {option.name}
                {option.locked ? ` — ${option.locked_reason || 'not in your plan'}` : ''}
              </option>
            ))}
          </select>
        </div>
        {sentence || use.note ? (
          <div className={`need-detail${stale ? ' is-attention' : ''}`}>
            {[sentence, use.note].filter(Boolean).join(' ')}
          </div>
        ) : null}
      </div>
    )
  }

  const toggle = (slug: string) =>
    onChange(value.includes(slug) ? value.filter((s) => s !== slug) : [...value, slug])
  return (
    <div className="need-pick">
      <div className="need-pick-question">{use.question}</div>
      <div className="need-checks" role="group" aria-label={use.question}>
        {use.options.map((option) => {
          const on = value.includes(option.slug)
          return (
            <label
              key={option.slug}
              className={`need-check${on ? ' is-on' : ''}${option.locked ? ' is-locked' : ''}`}
              title={option.locked ? option.locked_reason || 'Not included in your plan.' : undefined}
            >
              <input
                type="checkbox"
                checked={on}
                disabled={option.locked}
                onChange={() => toggle(option.slug)}
              />
              <ConnectorIcon slug={option.slug} name={option.name} size={18} />
              {option.name}
              {!option.connected ? <span className="need-check-sub">not connected</span> : null}
              {option.locked ? <span className="need-check-sub">{option.locked_reason || 'Not in your plan'}</span> : null}
            </label>
          )
        })}
      </div>
      {empty ? (
        <div className="need-detail is-attention">Tick at least one.</div>
      ) : sentence || use.note ? (
        <div className={`need-detail${stale ? ' is-attention' : ''}`}>
          {[sentence, use.note].filter(Boolean).join(' ')}
        </div>
      ) : null}
    </div>
  )
}
