/**
 * Anomaly alert rules and recent events.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  SectionHeading,
  StatTile,
  Tag,
  statusTone,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

export function AlertsPage() {
  const { canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const writable = canWrite('seo')
  const { data, loading, error, reload } = useResource(
    (signal) => api.alerts(signal),
    [],
    'alerts',
  )

  const [name, setName] = useState('')
  const [metric, setMetric] = useState('ai_brand_share')
  const [threshold, setThreshold] = useState('0.3')
  const [channel, setChannel] = useState('email')
  const [busy, setBusy] = useState(false)

  if (loading && !data) return <Loading label="Loading anomaly alerts…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const createRule = async () => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    const trimmed = name.trim()
    if (trimmed.length < 2) {
      push('Enter a rule name', 'warning')
      return
    }
    setBusy(true)
    try {
      fromResult(
        await api.createAlertRule({
          name: trimmed,
          metric: metric.trim() || 'ai_brand_share',
          threshold: Number(threshold) || 0.3,
          channel: channel.trim() || 'email',
        }),
      )
      setName('')
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const disableRule = async (ruleId: string) => {
    if (!writable) return
    setBusy(true)
    try {
      fromResult(await api.disableAlertRule(ruleId))
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Anomaly alerts"
        description="Rules that watch citation share, traffic and spend for unusual moves, then notify the channel you choose."
      />

      <div className="grid-tiles">
        <StatTile kicker="Active rules" value={String(data.rules.filter((r) => r.enabled === 'yes').length)} />
        <StatTile kicker="All rules" value={String(data.rules.length)} />
        <StatTile kicker="Recent events" value={String(data.events.length)} />
      </div>

      <div className="split-1-1">
        <Blueprint className="card elev-sm">
          <SectionHeading title="Rules" description="Disable a rule to stop new events without deleting history." />
          {data.rules.length === 0 ? (
            <EmptyState
              title="No rules yet"
              description="Add a metric and threshold. Events appear when the detector fires."
            />
          ) : (
            <ul className="stack-sm">
              {data.rules.map((rule) => (
                <li key={rule.id} className="list-row">
                  <div className="list-row-main">
                    <div className="list-row-title">{rule.name}</div>
                    <div className="list-row-meta muted">
                      {rule.metric} · threshold {rule.threshold} · {rule.channel}
                    </div>
                  </div>
                  <div className="list-row-side">
                    <Tag tone={rule.enabled === 'yes' ? 'accent' : 'neutral'}>
                      {rule.enabled === 'yes' ? 'On' : 'Off'}
                    </Tag>
                    {writable && rule.enabled === 'yes' ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        disabled={busy}
                        onClick={() => void disableRule(rule.id)}
                      >
                        Disable
                      </button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Blueprint>

        <Blueprint className="card elev-sm">
          <SectionHeading title="Add a rule" />
          {writable ? (
            <div className="stack">
              <Field label="Name" htmlFor="alert-name" required>
                <input
                  id="alert-name"
                  className="input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Citation share drop"
                />
              </Field>
              <Field label="Metric" htmlFor="alert-metric">
                <input
                  id="alert-metric"
                  className="input"
                  value={metric}
                  onChange={(e) => setMetric(e.target.value)}
                />
              </Field>
              <Field label="Threshold" htmlFor="alert-threshold">
                <input
                  id="alert-threshold"
                  className="input"
                  value={threshold}
                  onChange={(e) => setThreshold(e.target.value)}
                  inputMode="decimal"
                />
              </Field>
              <Field label="Channel" htmlFor="alert-channel">
                <input
                  id="alert-channel"
                  className="input"
                  value={channel}
                  onChange={(e) => setChannel(e.target.value)}
                  placeholder="email"
                />
              </Field>
              <div className="row">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => void createRule()}
                >
                  Add rule
                </button>
              </div>
            </div>
          ) : (
            <p className="muted">View-only access for your role.</p>
          )}
        </Blueprint>
      </div>

      <SectionHeading title="Recent events" spaced />
      {data.events.length === 0 ? (
        <EmptyState
          title="No events yet"
          description="When a rule fires, the summary appears here."
        />
      ) : (
        <Blueprint className="card elev-sm">
          <ul className="stack-sm">
            {data.events.map((event) => (
              <li key={event.id} className="list-row">
                <div className="list-row-main">
                  <div className="list-row-title">{event.summary}</div>
                  <div className="list-row-meta muted">{event.metric}</div>
                </div>
                <div className="list-row-side">
                  <Tag tone={statusTone(event.status)}>{event.status}</Tag>
                </div>
              </li>
            ))}
          </ul>
        </Blueprint>
      )}
    </>
  )
}
