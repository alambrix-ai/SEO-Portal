/**
 * Floating Willy - compact animated bot bottom-right on every page.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type {
  ActionPlan,
  ActionStep,
  AssistantChatResponse,
  AssistantMode,
} from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { AgentIcon } from '@/components/AgentIcon'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { WillyBot } from '@/components/WillyBot'
import { useToasts } from '@/components/Toasts'
import { Field, Loading, Tag } from '@/components/ui'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  payload?: AssistantChatResponse
}

function newId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export function AssistantDock() {
  const { session, access, canView, canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<AssistantMode>('ask')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content: 'Hi - I’m Willy. What do you want to get done?',
    },
  ])
  const [plan, setPlan] = useState<ActionPlan | null>(null)
  const [stepIndex, setStepIndex] = useState(0)
  const [stepValues, setStepValues] = useState<Record<string, string>>({})
  const [stepBusy, setStepBusy] = useState(false)
  const chatLogRef = useRef<HTMLDivElement>(null)
  const stepPanelRef = useRef<HTMLDivElement>(null)

  // Always show the bot when signed in; lock the panel when the workspace /
  // role cannot use Onboarding (the module that gates the assistant).
  const moduleEnabled = !!session?.enabled_modules.includes('onboarding')
  const roleAllowed = canView('onboarding')
  const allowed = moduleEnabled && roleAllowed

  const accessBlock = useMemo(() => {
    if (!session) return null
    if (!moduleEnabled) {
      return {
        title: 'Willy is not available for this workspace',
        body:
          'Willy is turned off for this workspace (Onboarding is disabled). Ask a platform administrator to enable it, or join a workspace where it is available.',
      }
    }
    if (access('onboarding') === 'none' || !roleAllowed) {
      return {
        title: 'You do not have access',
        body:
          'Your workspace admin has not granted you permission to use Willy. Ask an admin to give your role access to Onboarding (view or full), then sign in again.',
      }
    }
    return null
  }, [session, moduleEnabled, roleAllowed, access])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  useEffect(() => {
    const el = chatLogRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages, busy, open])

  useEffect(() => {
    if (!plan) return
    stepPanelRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [plan, stepIndex])

  const writableConnectors = canWrite('connectors')
  const writableAgents = canWrite('agents')

  if (!session) return null

  const appendAssistantNote = (content: string) => {
    setMessages((current) => [
      ...current,
      { id: newId(), role: 'assistant', content },
    ])
  }

  const send = async () => {
    if (!allowed) {
      push(
        accessBlock?.body ||
          'You do not have access to Willy. Ask your workspace admin.',
        'warning',
      )
      return
    }
    const text = draft.trim()
    if (!text || busy) return
    setDraft('')
    const priorHistory = messages
      .filter((m) => m.id !== 'welcome')
      .slice(-40)
      .map((m) => ({ role: m.role, content: m.content }))
    const userMsg: ChatMessage = { id: newId(), role: 'user', content: text }
    const assistantId = newId()
    setMessages((current) => [
      ...current,
      userMsg,
      { id: assistantId, role: 'assistant', content: '' },
    ])
    setBusy(true)
    setPlan(null)
    setStepIndex(0)
    setStepValues({})
    try {
      const response = await api.assistantChatStream(
        {
          message: text,
          mode,
          history: priorHistory,
        },
        {
          onDelta: (chunk) => {
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, content: message.content + chunk }
                  : message,
              ),
            )
          },
        },
      )
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                content: response.reply || message.content,
                payload: response,
              }
            : message,
        ),
      )
      if (mode === 'action' && response.action_plan?.steps?.length) {
        setPlan(response.action_plan)
        setStepIndex(0)
        setStepValues({})
      } else if (mode === 'action') {
        push(
          'No actionable steps yet - try naming the connectors or agents you want set up.',
          'info',
        )
      }
    } catch (caught) {
      setMessages((current) =>
        current.filter(
          (message) => !(message.id === assistantId && !message.content.trim()),
        ),
      )
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const currentStep: ActionStep | null =
    plan && plan.steps[stepIndex] ? plan.steps[stepIndex] : null

  const credentialFields = (currentStep?.fields || []).filter((field) => !field.is_oauth)
  const isOauthOnly =
    currentStep?.type === 'connect_connector' &&
    (currentStep.fields || []).some((field) => field.is_oauth) &&
    credentialFields.length === 0

  const advancePlan = (note?: string) => {
    if (note) appendAssistantNote(note)
    const next = stepIndex + 1
    if (plan && next < plan.steps.length) {
      setStepIndex(next)
      setStepValues({})
    } else {
      push('Action plan complete', 'success')
      appendAssistantNote('All set - the planned connections and agent steps are done.')
      setPlan(null)
      setStepIndex(0)
      setStepValues({})
    }
  }

  const runStep = async () => {
    if (!currentStep) return
    if (currentStep.type === 'connect_connector' && !writableConnectors) {
      push('View-only access - ask an admin to connect integrations', 'warning')
      return
    }
    if (
      (currentStep.type === 'configure_agent' || currentStep.type === 'resume_agent') &&
      !writableAgents
    ) {
      push('View-only access - ask an admin to configure agents', 'warning')
      return
    }

    if (isOauthOnly) {
      push('This connector needs OAuth - finish it under Connectors, then continue.', 'warning')
      return
    }

    setStepBusy(true)
    try {
      if (currentStep.type === 'connect_connector') {
        const missing = credentialFields
          .filter((field) => field.required)
          .filter((field) => !(stepValues[field.key] || '').trim())
        if (missing.length) {
          const first = missing[0]!
          push(`Fill in ${first.label || first.key}`, 'warning')
          return
        }
        await api.connect(currentStep.slug, stepValues)
        push(`${currentStep.title || currentStep.slug} connected`, 'success')
        advancePlan(`Connected **${currentStep.title || currentStep.slug}**.`)
      } else if (currentStep.type === 'configure_agent') {
        const config = currentStep.config || {}
        await api.configureAgent(currentStep.slug, {
          schedule: String(config.schedule || 'Daily'),
          scope: String(config.scope || '/'),
          notify_channel: String(config.notify_channel || 'None'),
          llm_connector: String(config.llm_connector || ''),
          max_actions_per_day: Number(config.max_actions_per_day || 20),
        })
        push(`${currentStep.title || currentStep.slug} configured`, 'success')
        advancePlan(`Configured **${currentStep.title || currentStep.slug}**.`)
      } else if (currentStep.type === 'resume_agent') {
        await api.resumeAgent(currentStep.slug)
        push(`${currentStep.title || currentStep.slug} started`, 'success')
        advancePlan(`Started **${currentStep.title || currentStep.slug}**.`)
      }
    } catch (caught) {
      fromError(caught)
    } finally {
      setStepBusy(false)
    }
  }

  return (
    <div
      className={`assistant-dock${open ? ' is-open' : ''}${accessBlock ? ' is-locked' : ''}`}
    >
      {open ? (
        <section
          className="assistant-dock-panel"
          role="dialog"
          aria-label="Willy"
          aria-modal="false"
        >
          <header className="assistant-dock-header">
            <div className="assistant-dock-brand">
              <div className="assistant-dock-heading">
                <strong>Willy</strong>
                {plan ? (
                  <span className="assistant-dock-sub">
                    Action {stepIndex + 1}/{plan.steps.length}
                    {plan.summary ? ` - ${plan.summary}` : ''}
                  </span>
                ) : null}
              </div>
            </div>
            <button
              type="button"
              className="assistant-dock-close"
              aria-label="Close Willy"
              onClick={() => setOpen(false)}
            >
              <span aria-hidden="true">×</span>
            </button>
          </header>

          {accessBlock ? (
            <div className="assistant-dock-locked" role="status">
              <div className="assistant-dock-locked-mark" aria-hidden="true">
                !
              </div>
              <h3 className="assistant-dock-locked-title">{accessBlock.title}</h3>
              <p className="assistant-dock-locked-body">{accessBlock.body}</p>
              <p className="small muted" style={{ marginTop: 12 }}>
                Signed in as <strong>{session.user.email}</strong> (
                {session.user.role_label}) in {session.organization.name}.
              </p>
            </div>
          ) : (
            <>
              <div
                ref={chatLogRef}
                className="assistant-chat-log"
                role="log"
                aria-live="polite"
              >
                {messages.map((message) => {
                  const isPending =
                    busy &&
                    message.role === 'assistant' &&
                    message.id === messages[messages.length - 1]?.id &&
                    !message.content
                  return (
                  <div
                    key={message.id}
                    className={`assistant-bubble assistant-bubble-${message.role}${
                      busy &&
                      message.role === 'assistant' &&
                      message.id === messages[messages.length - 1]?.id
                        ? ' is-streaming'
                        : ''
                    }${isPending ? ' assistant-bubble-pending' : ''}`}
                  >
                    <div className="assistant-bubble-role">
                      {message.role === 'user' ? 'You' : 'Willy'}
                    </div>
                    <div className="assistant-bubble-body">
                      {message.role === 'assistant' ? (
                        message.content ? (
                          <div className="assistant-md">
                            <ReactMarkdown>{message.content}</ReactMarkdown>
                          </div>
                        ) : busy ? (
                          <Loading
                            label={
                              mode === 'action'
                                ? 'Building your action plan…'
                                : 'Analysing your use case…'
                            }
                          />
                        ) : null
                      ) : (
                        message.content
                      )}
                    </div>
                    {message.payload?.recommendations ? (
                      <RecommendationsBlock payload={message.payload} />
                    ) : null}
                  </div>
                  )
                })}
              </div>

              {plan && currentStep ? (
                <>
                  <div className="assistant-plan-banner" role="status">
                    Enter credentials below - Willy will connect and configure each step
                    for you.
                  </div>
                  <div ref={stepPanelRef} className="assistant-dock-step">
                    <div className="card-kicker">
                      Step {stepIndex + 1} of {plan.steps.length}
                    </div>
                    <div className="cell-title">{currentStep.title}</div>
                    {currentStep.reason ? (
                      <p className="small muted" style={{ marginTop: 4 }}>
                        {currentStep.reason}
                      </p>
                    ) : null}
                    <div className="assistant-step-mark">
                      {currentStep.type === 'connect_connector' ? (
                        <ConnectorIcon
                          slug={currentStep.slug}
                          name={currentStep.title || currentStep.slug}
                          size={32}
                        />
                      ) : (
                        <AgentIcon slug={currentStep.slug} size={32} />
                      )}
                      <Tag tone="outline">{currentStep.type.replace(/_/g, ' ')}</Tag>
                    </div>
                    {currentStep.type === 'connect_connector' ? (
                      <div className="assistant-step-fields">
                        {credentialFields.map((field) => (
                          <Field
                            key={field.key}
                            label={field.label || field.key}
                            required={field.required}
                            hint={field.help || undefined}
                            htmlFor={`dock-${field.key}`}
                          >
                            <input
                              id={`dock-${field.key}`}
                              className="input"
                              type={field.secret ? 'password' : 'text'}
                              autoComplete="off"
                              placeholder={field.placeholder || `Enter ${field.label || field.key}`}
                              value={stepValues[field.key] || ''}
                              onChange={(event) =>
                                setStepValues((current) => ({
                                  ...current,
                                  [field.key]: event.target.value,
                                }))
                              }
                              onKeyDown={(event) => {
                                if (event.key === 'Enter') {
                                  event.preventDefault()
                                  void runStep()
                                }
                              }}
                            />
                          </Field>
                        ))}
                        {isOauthOnly ? (
                          <p className="muted small">
                            This connector uses OAuth - finish signing in under{' '}
                            <Link to="/connectors" onClick={() => setOpen(false)}>
                              Connectors
                            </Link>
                            , then skip this step.
                          </p>
                        ) : null}
                        {(currentStep.fields || []).some((field) => field.is_oauth) &&
                        credentialFields.length > 0 ? (
                          <p className="muted small">
                            Also needs OAuth - complete that in{' '}
                            <Link to="/connectors" onClick={() => setOpen(false)}>
                              Connectors
                            </Link>{' '}
                            after saving these fields.
                          </p>
                        ) : null}
                      </div>
                    ) : null}
                    {currentStep.type === 'configure_agent' && currentStep.config ? (
                      <ul className="assistant-config-preview">
                        {Object.entries(currentStep.config).map(([key, value]) => (
                          <li key={key}>
                            <span className="muted">{key}</span>
                            <strong>{String(value)}</strong>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    <div className="assistant-step-actions">
                      <button
                        type="button"
                        className="btn btn-ghost"
                        disabled={stepBusy}
                        onClick={() => advancePlan()}
                      >
                        Skip
                      </button>
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={stepBusy || isOauthOnly}
                        onClick={() => void runStep()}
                      >
                        {stepBusy
                          ? 'Working…'
                          : currentStep.type === 'connect_connector'
                            ? 'Connect'
                            : currentStep.type === 'configure_agent'
                              ? 'Save'
                              : 'Start'}
                      </button>
                    </div>
                  </div>
                </>
              ) : null}

              <div className="assistant-query">
                <div
                  className="assistant-query-modes"
                  role="radiogroup"
                  aria-label="Willy mode"
                >
                  <button
                    type="button"
                    role="radio"
                    aria-checked={mode === 'ask'}
                    className={`assistant-capsule${mode === 'ask' ? ' is-active' : ''}`}
                    onClick={() => setMode('ask')}
                  >
                    Ask
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={mode === 'action'}
                    className={`assistant-capsule${mode === 'action' ? ' is-active' : ''}`}
                    onClick={() => setMode('action')}
                  >
                    Action
                  </button>
                </div>
                <input
                  id="assistant-dock-input"
                  className="assistant-query-input"
                  type="text"
                  placeholder={
                    mode === 'action'
                      ? 'What should Willy set up?'
                      : 'Ask Willy…'
                  }
                  value={draft}
                  disabled={busy}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      void send()
                    }
                  }}
                />
                <button
                  type="button"
                  className="btn btn-primary assistant-query-send"
                  disabled={busy || !draft.trim()}
                  onClick={() => void send()}
                >
                  {busy ? '…' : mode === 'action' ? 'Plan' : 'Ask'}
                </button>
              </div>
            </>
          )}
        </section>
      ) : null}

      <div className="assistant-dock-anchor">
        {!open ? (
          <button
            type="button"
            className="assistant-ask-me"
            onClick={() => setOpen(true)}
            aria-label="Ask Willy"
          >
            <span className="assistant-ask-me-dot" aria-hidden="true" />
            <span className="assistant-ask-me-text">Ask Willy</span>
          </button>
        ) : null}
        <button
          type="button"
          className="assistant-dock-launcher"
          aria-label={open ? 'Hide Willy' : 'Open Willy'}
          aria-expanded={open}
          onClick={() => setOpen((current) => !current)}
        >
          <WillyBot compact />
        </button>
      </div>
    </div>
  )
}

function RecommendationsBlock({ payload }: { payload: AssistantChatResponse }) {
  const connectors = payload.recommendations?.connectors ?? []
  const agents = payload.recommendations?.agents ?? []
  if (!connectors.length && !agents.length) return null
  return (
    <div className="assistant-recs">
      {connectors.length ? (
        <div>
          <div className="assistant-recs-label">Connectors</div>
          <ul className="assistant-recs-list">
            {connectors.map((item) => (
              <li key={`c-${item.slug}`}>
                <ConnectorIcon slug={item.slug} name={item.name || item.slug} size={24} />
                <div>
                  <div className="cell-title">{item.name || item.slug}</div>
                  <div className="small muted">{item.reason}</div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {agents.length ? (
        <div>
          <div className="assistant-recs-label">Agents</div>
          <ul className="assistant-recs-list">
            {agents.map((item) => (
              <li key={`a-${item.slug}`}>
                <AgentIcon slug={item.slug} size={24} />
                <div>
                  <div className="cell-title">{item.name || item.slug}</div>
                  <div className="small muted">{item.reason}</div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}
