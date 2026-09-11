/**
 * SEO Assistant — describe a use case; get Ask guidance or Action setup.
 *
 * Ask mode explains which connectors and agents fit. Action mode walks the
 * operator through connecting and configuring them with the same APIs the
 * Connectors / Agents screens use.
 */
import { useMemo, useState } from 'react'
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
import { AssistantBot } from '@/components/AssistantBot'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { Blueprint, Field, Loading, Segmented, Tag } from '@/components/ui'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  payload?: AssistantChatResponse
}

function newId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export function AssistantPage() {
  const { canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const [mode, setMode] = useState<AssistantMode>('ask')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content:
        'Tell me what you want to achieve — organic growth, AEO answers, backlinks, ads protection, or something more specific. I will map it to the right connectors and agents on this platform.',
    },
  ])
  const [plan, setPlan] = useState<ActionPlan | null>(null)
  const [stepIndex, setStepIndex] = useState(0)
  const [stepValues, setStepValues] = useState<Record<string, string>>({})
  const [stepBusy, setStepBusy] = useState(false)

  const writableConnectors = canWrite('connectors')
  const writableAgents = canWrite('agents')

  const history = useMemo(
    () =>
      messages
        .filter((m) => m.id !== 'welcome')
        .map((m) => ({ role: m.role, content: m.content })),
    [messages],
  )

  const send = async () => {
    const text = draft.trim()
    if (!text || busy) return
    setDraft('')
    const userMsg: ChatMessage = { id: newId(), role: 'user', content: text }
    setMessages((current) => [...current, userMsg])
    setBusy(true)
    setPlan(null)
    setStepIndex(0)
    setStepValues({})
    try {
      const response = await api.assistantChat({
        message: text,
        mode,
        history,
      })
      setMessages((current) => [
        ...current,
        {
          id: newId(),
          role: 'assistant',
          content: response.reply,
          payload: response,
        },
      ])
      if (mode === 'action' && response.action_plan?.steps?.length) {
        setPlan(response.action_plan)
        setStepIndex(0)
      }
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const currentStep: ActionStep | null =
    plan && plan.steps[stepIndex] ? plan.steps[stepIndex] : null

  const runStep = async () => {
    if (!currentStep) return
    if (currentStep.type === 'connect_connector' && !writableConnectors) {
      push('View-only access — ask an admin to connect integrations', 'warning')
      return
    }
    if (
      (currentStep.type === 'configure_agent' || currentStep.type === 'resume_agent') &&
      !writableAgents
    ) {
      push('View-only access — ask an admin to configure agents', 'warning')
      return
    }

    setStepBusy(true)
    try {
      if (currentStep.type === 'connect_connector') {
        const missing = (currentStep.fields || [])
          .filter((field) => field.required && !field.is_oauth)
          .filter((field) => !(stepValues[field.key] || '').trim())
        if (missing.length) {
          const first = missing[0]!
          push(`Fill in ${first.label || first.key}`, 'warning')
          return
        }
        await api.connect(currentStep.slug, stepValues)
        push(`${currentStep.title || currentStep.slug} connected`, 'success')
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
      } else if (currentStep.type === 'resume_agent') {
        await api.resumeAgent(currentStep.slug)
        push(`${currentStep.title || currentStep.slug} started`, 'success')
      }

      const next = stepIndex + 1
      if (plan && next < plan.steps.length) {
        setStepIndex(next)
        setStepValues({})
      } else {
        push('Action plan complete', 'success')
        setPlan(null)
        setStepIndex(0)
        setStepValues({})
      }
    } catch (caught) {
      fromError(caught)
    } finally {
      setStepBusy(false)
    }
  }

  return (
    <div className="assistant-page">
      <div className="assistant-hero">
        <Blueprint className="assistant-hero-card elev-sm">
          <div className="assistant-hero-copy">
            <div className="assistant-eyebrow">Smart setup</div>
            <h2 className="assistant-title">SEO Assistant</h2>
            <p className="assistant-lede">
              Describe your use case. In Ask mode I explain which connectors and
              agents you need. In Action mode I walk you through connecting and
              configuring them.
            </p>
            <div className="assistant-mode">
              <Segmented
                name="assistant-mode"
                value={mode}
                options={[
                  { value: 'ask', label: 'Ask' },
                  { value: 'action', label: 'Action' },
                ]}
                onChange={(next) => setMode(next as AssistantMode)}
              />
              <span className="small muted">
                {mode === 'ask'
                  ? 'Guidance only — nothing is connected for you.'
                  : 'I will collect what each connector/agent needs, then apply it.'}
              </span>
            </div>
          </div>
          <AssistantBot />
        </Blueprint>
      </div>

      <div className="assistant-layout">
        <Blueprint className="card elev-sm assistant-chat">
          <div className="assistant-chat-log" role="log" aria-live="polite">
            {messages.map((message) => (
              <div
                key={message.id}
                className={`assistant-bubble assistant-bubble-${message.role}`}
              >
                <div className="assistant-bubble-role">
                  {message.role === 'user' ? 'You' : 'Assistant'}
                </div>
                <div className="assistant-bubble-body">{message.content}</div>
                {message.payload?.recommendations ? (
                  <RecommendationsBlock payload={message.payload} />
                ) : null}
              </div>
            ))}
            {busy ? (
              <div className="assistant-bubble assistant-bubble-assistant">
                <Loading label="Analysing your use case…" />
              </div>
            ) : null}
          </div>

          <div className="assistant-composer">
            <textarea
              className="input assistant-input"
              rows={3}
              placeholder="e.g. We sell industrial equipment online and want AI search citations plus healthier technical SEO…"
              value={draft}
              disabled={busy}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault()
                  void send()
                }
              }}
            />
            <div className="assistant-composer-actions">
              <span className="small muted">Ctrl/⌘ + Enter to send</span>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy || !draft.trim()}
                onClick={() => void send()}
              >
                {busy ? 'Thinking…' : mode === 'action' ? 'Plan setup' : 'Ask'}
              </button>
            </div>
          </div>
        </Blueprint>

        <aside className="assistant-side">
          {currentStep ? (
            <Blueprint className="card elev-sm assistant-action-card">
              <div className="card-kicker">
                Action step {stepIndex + 1} of {plan?.steps.length ?? 0}
              </div>
              <div className="card-title" style={{ fontSize: 18 }}>
                {currentStep.title}
              </div>
              {currentStep.reason ? (
                <p className="card-meta" style={{ marginTop: 6 }}>
                  {currentStep.reason}
                </p>
              ) : null}

              <div className="assistant-step-mark">
                {currentStep.type === 'connect_connector' ? (
                  <ConnectorIcon
                    slug={currentStep.slug}
                    name={currentStep.title || currentStep.slug}
                    size={40}
                  />
                ) : (
                  <AgentIcon slug={currentStep.slug} size={40} />
                )}
                <Tag tone="outline">{currentStep.type.replace(/_/g, ' ')}</Tag>
              </div>

              {currentStep.type === 'connect_connector' ? (
                <div className="assistant-step-fields">
                  {(currentStep.fields || [])
                    .filter((field) => !field.is_oauth)
                    .map((field) => (
                      <Field
                        key={field.key}
                        label={field.label || field.key}
                        required={field.required}
                        hint={field.help || undefined}
                        htmlFor={`asst-${field.key}`}
                      >
                        <input
                          id={`asst-${field.key}`}
                          className="input"
                          type={field.secret ? 'password' : 'text'}
                          placeholder={field.placeholder}
                          value={stepValues[field.key] || ''}
                          onChange={(event) =>
                            setStepValues((current) => ({
                              ...current,
                              [field.key]: event.target.value,
                            }))
                          }
                        />
                      </Field>
                    ))}
                  {(currentStep.fields || []).some((field) => field.is_oauth) ? (
                    <p className="muted small">
                      This connector uses OAuth. Open{' '}
                      <Link to="/connectors">Connectors</Link> to finish the
                      vendor sign-in, then return here.
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
                  onClick={() => {
                    const next = stepIndex + 1
                    if (plan && next < plan.steps.length) {
                      setStepIndex(next)
                      setStepValues({})
                    } else {
                      setPlan(null)
                    }
                  }}
                >
                  Skip
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={stepBusy}
                  onClick={() => void runStep()}
                >
                  {stepBusy
                    ? 'Working…'
                    : currentStep.type === 'connect_connector'
                      ? 'Connect'
                      : currentStep.type === 'configure_agent'
                        ? 'Save configuration'
                        : 'Start agent'}
                </button>
              </div>
            </Blueprint>
          ) : (
            <Blueprint className="card elev-sm assistant-tips">
              <div className="card-kicker">How it works</div>
              <ul className="assistant-tips-list">
                <li>
                  <strong>Ask</strong> — get a clear map of connectors and agents
                  without changing your workspace.
                </li>
                <li>
                  <strong>Action</strong> — I propose steps; you supply secrets
                  and confirm each connection or agent setup.
                </li>
                <li>
                  Powered by the server .env LLM (
                  <span className="mono-sub">ANTHROPIC_API_KEY</span>
                  ), not workspace AI connectors.
                </li>
              </ul>
            </Blueprint>
          )}
        </aside>
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
                <ConnectorIcon slug={item.slug} name={item.name || item.slug} size={28} />
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
                <AgentIcon slug={item.slug} size={28} />
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
