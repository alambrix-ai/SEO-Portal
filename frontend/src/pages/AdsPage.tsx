/**
 * Ads & Programmatic workspace - budgets, creatives, audiences, fraud log.
 *
 * Moving a slider locks that channel, because a human's number should not be
 * silently undone by the predictive engine's next pass. The lock is visible
 * and reversible.
 */
import { useEffect, useState } from 'react'

import { api } from '@/api/client'
import type { ActionResult } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  EmptyState,
  ErrorState,
  Loading,
  SectionHeading,
  Tag,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { money, moneyExact } from '@/lib/money'

export function AdsPage() {
  const { canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const { data, loading, error, reload, set } = useResource(() => api.ads())

  // Slider positions are held locally while dragging, then committed on
  // release - otherwise every pixel of movement would be a request.
  const [draft, setDraft] = useState<Record<string, number>>({})
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (data) {
      setDraft(Object.fromEntries(data.budgets.map((b) => [b.channel, b.percent])))
    }
  }, [data])

  const writable = canWrite('ads')

  const guard = (): boolean => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return false
    }
    return true
  }

  const commit = async (channel: string, percent: number) => {
    if (!guard()) return
    setBusy(true)
    try {
      set(await api.setBudget(channel, percent))
    } catch (caught) {
      fromError(caught)
      await reload()
    } finally {
      setBusy(false)
    }
  }

  const run = async (action: () => Promise<ActionResult>) => {
    if (!guard()) return
    setBusy(true)
    try {
      fromResult(await action())
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  if (loading && !data) return <Loading label="Loading channels…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const totalTone = data.budget_total === 100 ? 'accent' : 'outline'

  return (
    <>
      <div className="split-11-10">
        <div>
          <div className="row-baseline">
            <h3 style={{ margin: 0 }}>Predictive budget allocation</h3>
            <Tag tone={totalTone}>{data.budget_total}% allocated</Tag>
          </div>

          <div>
            {data.budgets.map((channel) => (
              <div className="budget-row" key={channel.channel}>
                <div className="budget-labels">
                  <span>{channel.label}</span>
                  <span className="nowrap">
                    {draft[channel.channel] ?? channel.percent}% · CAC{' '}
                    {moneyExact(channel.cac)}
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={draft[channel.channel] ?? channel.percent}
                  disabled={!writable || busy}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      [channel.channel]: Number(event.target.value),
                    }))
                  }
                  onMouseUp={(event) =>
                    void commit(channel.channel, Number(event.currentTarget.value))
                  }
                  onTouchEnd={(event) =>
                    void commit(channel.channel, Number(event.currentTarget.value))
                  }
                  onKeyUp={(event) =>
                    void commit(channel.channel, Number(event.currentTarget.value))
                  }
                  aria-label={`${channel.label} budget share`}
                />
                <div
                  className="budget-bar"
                  style={{ width: `${draft[channel.channel] ?? channel.percent}%` }}
                />
                <div className="budget-meta">
                  <span>
                    {money(channel.spend)} spend ·{' '}
                    {channel.conversions.toLocaleString()} conversions
                  </span>
                  {channel.locked ? (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      style={{ padding: '0 4px' }}
                      disabled={!writable || busy}
                      onClick={() =>
                        void run(() => api.unlockChannel(channel.channel))
                      }
                    >
                      Locked - unlock
                    </button>
                  ) : null}
                  {!channel.connected ? (
                    <span className="muted">not connected</span>
                  ) : null}
                </div>
              </div>
            ))}
          </div>

          <div className="row" style={{ marginTop: 14 }}>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={!writable || busy}
              onClick={() => void run(() => api.rebalanceBudget())}
            >
              Rebalance to lowest CAC
            </button>
            <span className="small muted">
              Blended CAC {moneyExact(data.blended_cac)}
            </span>
          </div>
        </div>

        <div>
          <div className="row-baseline">
            <h3 style={{ margin: 0 }}>Dynamic Creative Optimization</h3>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={!writable || busy}
              onClick={() => void run(() => api.generateCreatives())}
            >
              Generate variants
            </button>
          </div>

          {data.creatives.length === 0 ? (
            <EmptyState
              title="No variants yet"
              description="The optimizer builds creative from your synced product pages. Connect an ad platform and a CMS, then generate."
            />
          ) : (
            <div className="grid-cards-xs">
              {data.creatives.map((creative) => (
                <Blueprint key={creative.id} className="creative-tile">
                  <div className="creative-platform">{creative.platform}</div>
                  <div style={{ marginTop: 6 }}>{creative.dimensions}</div>
                  {creative.headline ? (
                    <div className="creative-headline">{creative.headline}</div>
                  ) : null}
                  <div style={{ marginTop: 6 }}>{creative.status}</div>
                </Blueprint>
              ))}
            </div>
          )}

          {data.audiences.length > 0 ? (
            <>
              <SectionHeading title="First-party audiences" spaced />
              <div className="stack-sm">
                {data.audiences.map((audience) => (
                  <div
                    className="card"
                    key={audience.id}
                  >
                    <div className="row-between">
                      <span style={{ fontSize: 13 }}>{audience.label}</span>
                      <Tag tone={audience.is_live ? 'accent' : 'neutral'}>
                        {audience.is_live ? 'Live' : 'Modelled'}
                      </Tag>
                    </div>
                    <div className="card-meta">
                      {audience.size.toLocaleString()} profiles · cohesion{' '}
                      {audience.cohesion.toFixed(2)} ·{' '}
                      {audience.top_signals.join(', ') || 'no dominant signal'}
                    </div>
                  </div>
                ))}
              </div>
            </>
          ) : null}
        </div>
      </div>

      <SectionHeading
        title="Click-fraud & ad-waste controller"
        description="Detection is heuristic-first and explainable: every block names the signal that triggered it."
        spaced
      />

      {data.fraud_log.length === 0 ? (
        <EmptyState
          title="No blocked placements yet"
          description="Connect an ad platform or DSP seat, and the controller will start sampling traffic quality on its next run."
        />
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Source</th>
                <th>Channel</th>
                <th>Flag</th>
                <th>Saved</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {data.fraud_log.map((event, index) => (
                <tr key={`${event.source}-${index}`}>
                  <td>{event.time}</td>
                  <td>{event.source}</td>
                  <td className="small">{event.channel || '-'}</td>
                  <td>{event.reason}</td>
                  <td>{money(event.spend_saved)}</td>
                  <td>
                    <Tag tone="accent">{event.action}</Tag>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
