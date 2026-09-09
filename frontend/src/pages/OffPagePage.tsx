/**
 * Off-Page & PR workspace — discovered placements and competitor alerts.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { ActionResult } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  EmptyState,
  ErrorState,
  Loading,
  SectionHeading,
  Tag,
  statusTone,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

export function OffPagePage() {
  const { canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const { data, loading, error, reload } = useResource(() => api.offpage())
  const [busyId, setBusyId] = useState<string | null>(null)

  const writable = canWrite('offpage')

  const act = async (id: string, action: () => Promise<ActionResult>) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusyId(id)
    try {
      fromResult(await action())
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  if (loading && !data) return <Loading label="Loading placements…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <SectionHeading
        title="Backlink node discovery"
        description="Scored on authority and topical fit together — a well-matched mid-authority placement beats a mismatched large one."
      />

      {data.backlinks.length === 0 ? (
        <EmptyState
          title="No placement targets yet"
          description="Backlink Node Discovery builds its topic model from your synced pages and search queries. Sync a CMS, then let it run."
        />
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Domain</th>
                <th>Authority</th>
                <th>Relevance</th>
                <th>Placement Type</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.backlinks.map((target) => (
                <tr key={target.id}>
                  <td>{target.domain}</td>
                  <td>{target.authority}</td>
                  <td>{target.relevance.toFixed(2)}</td>
                  <td>{target.placement_type}</td>
                  <td>
                    <Tag tone={statusTone(target.status)}>{target.status_label}</Tag>
                  </td>
                  <td className="table-actions">
                    {target.show_launch ? (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={busyId === target.id}
                        onClick={() =>
                          void act(target.id, () => api.launchOutreach(target.id))
                        }
                      >
                        {busyId === target.id ? 'Drafting…' : 'Launch outreach'}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <SectionHeading
        title="Competitor link-poaching monitor"
        description="A publisher that just linked to a competitor has proven it covers this subject and will link out — the best moment to approach it."
        spaced
      />

      {data.alerts.length === 0 ? (
        <EmptyState
          title="No competitor activity detected"
          description="Set your competitors in the Competitor Link Monitor's Configure dialog, and connect Search Console or Bing Webmaster Tools so it has real backlink data to compare."
        />
      ) : (
        <div className="stack">
          {data.alerts.map((alert) => (
            <div
              key={alert.id}
              className="card"
              style={{
                flexDirection: 'row',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 16,
              }}
            >
              <div style={{ fontSize: 13 }}>{alert.text}</div>
              <button
                type="button"
                className="btn btn-secondary nowrap"
                disabled={alert.counter_launched || busyId === alert.id}
                onClick={() =>
                  void act(alert.id, () => api.launchCounterPitch(alert.id))
                }
              >
                {busyId === alert.id ? 'Drafting…' : alert.button_label}
              </button>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
