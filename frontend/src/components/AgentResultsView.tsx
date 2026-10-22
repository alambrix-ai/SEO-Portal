/**
 * What an agent has produced — its pages, problems, pitches, ads, blocks —
 * rendered from the generic table the API builds per agent.
 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type { AgentResults } from '@/api/types'
import { StatusDot } from '@/components/ui'

type Tone = 'success' | 'warning' | 'danger' | 'neutral' | 'info'
const TONES = new Set(['success', 'warning', 'danger', 'neutral', 'info'])

export function AgentResultsView({ slug, refreshKey }: { slug: string; refreshKey: string }) {
  const [data, setData] = useState<AgentResults | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      setData(await api.agentResults(slug))
      setError('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load results')
    }
  }, [slug])

  // Reloaded whenever a run finishes (refreshKey changes), so new output shows up.
  useEffect(() => {
    void load()
  }, [load, refreshKey])

  if (error && !data) {
    return (
      <div className="empty-panel">
        <div className="empty-panel-title">Could not load results</div>
        <p className="empty-panel-text">{error}</p>
        <button type="button" className="btn btn-secondary" onClick={() => void load()}>
          Try again
        </button>
      </div>
    )
  }
  if (!data) return <p className="guide-text muted">Loading results…</p>

  return (
    <section className="results">
      <div className="results-head">
        <div>
          <h2 className="results-title">{data.title}</h2>
          {data.description ? <p className="results-desc">{data.description}</p> : null}
        </div>
        {data.link ? (
          <Link to={data.link} className="btn btn-secondary btn-sm">
            {data.link_label}
          </Link>
        ) : null}
      </div>

      {data.metrics.length ? (
        <div className="kpi-grid results-metrics">
          {data.metrics.map((metric) => (
            <div key={metric.label} className="kpi">
              <div className="kpi-label">{metric.label}</div>
              <div className="kpi-value">{metric.value}</div>
            </div>
          ))}
        </div>
      ) : null}

      {data.rows.length === 0 ? (
        <div className="empty-panel">
          <div className="empty-panel-title">Nothing yet</div>
          <p className="empty-panel-text">{data.empty}</p>
        </div>
      ) : (
        <div className="panel">
          <div className="table-wrap">
            <table className="table data-table results-table">
              <thead>
                <tr>
                  {data.columns.map((column) => (
                    <th key={column.key} className={column.kind === 'number' ? 'is-num' : undefined}>
                      {column.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row, index) => (
                  <tr key={index}>
                    {data.columns.map((column, ci) => {
                      const value = row.cells[column.key] ?? ''
                      const sub = row.cells[`${column.key}_sub`]
                      const tone = row.tones[column.key]
                      return (
                        <td
                          key={column.key}
                          data-label={column.label}
                          className={[
                            column.kind === 'number' ? 'is-num' : '',
                            column.kind === 'muted' ? 'muted nowrap' : '',
                            ci === 0 ? 'cell-wide' : '',
                          ]
                            .filter(Boolean)
                            .join(' ')}
                        >
                          {column.kind === 'status' ? (
                            <StatusDot tone={(TONES.has(tone ?? '') ? tone : 'neutral') as Tone}>{value}</StatusDot>
                          ) : (
                            <>
                              <div className={ci === 0 ? 'results-primary' : undefined}>{value || '—'}</div>
                              {sub ? <div className="cell-sub" title={sub}>{sub}</div> : null}
                            </>
                          )}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.total > data.rows.length ? (
            <div className="results-foot">
              Showing the latest {data.rows.length} of {data.total}.
              {data.link ? (
                <>
                  {' '}
                  <Link to={data.link}>{data.link_label} for everything</Link>
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </section>
  )
}
