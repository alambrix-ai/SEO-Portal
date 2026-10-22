/**
 * Portal Admin → Analyses: every website analysis on the platform.
 *
 * "Website" rows are the free analyses visitors ran from the marketing site
 * (no workspace; no personal data is stored). "Workspace" rows are analyses
 * run inside a customer workspace, with what was connected at the time.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { PortalAnalysisRow, SiteAnalysisDetail } from '@/api/types'
import { DownloadButtons, host, saveFile } from '@/components/AnalysisReport'
import { useToasts } from '@/components/Toasts'
import { Drawer, EmptyState, ErrorState, Loading } from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { when } from '@/lib/time'
import { AnalysisView } from '@/pages/SiteAnalysisPage'

const SOURCES = [
  { key: '', label: 'All' },
  { key: 'website', label: 'Website visitors' },
  { key: 'workspace', label: 'Workspaces' },
]

const STATUSES = [
  { key: '', label: 'Any status' },
  { key: 'done', label: 'Finished' },
  { key: 'running', label: 'Running' },
  { key: 'failed', label: 'Failed' },
]

export function PortalAnalyses() {
  const { fromError } = useToasts()
  const [source, setSource] = useState('')
  const [status, setStatus] = useState('')
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [open, setOpen] = useState<{ row: PortalAnalysisRow; detail: SiteAnalysisDetail | null } | null>(null)
  const { data, loading, error, reload } = useResource(
    (signal) => api.portalAnalyses({ source, status, q: search }, signal),
    [source, status, search],
    `portal:analyses:${source}:${status}:${search}`,
  )

  const openRow = async (row: PortalAnalysisRow) => {
    setOpen({ row, detail: null })
    try {
      setOpen({ row, detail: await api.portalAnalysis(row) })
    } catch (caught) {
      setOpen(null)
      fromError(caught)
    }
  }

  return (
    <section className="portal-panel" aria-label="Analyses">
      <div className="pa-head">
        <div>
          <h2 className="pa-title">Website analyses</h2>
          <p className="muted small">
            Free analyses run from your marketing website, and analyses run inside customer workspaces. Website visitors are counted by
            device only — no names or contact details are stored.
          </p>
        </div>
      </div>

      {data ? (
        <div className="pa-totals">
          <div>
            <strong>{data.totals.website}</strong>
            <span>From the website</span>
          </div>
          <div>
            <strong>{data.totals.workspace}</strong>
            <span>In workspaces</span>
          </div>
          <div>
            <strong>{data.totals.last_24h}</strong>
            <span>Last 24 hours</span>
          </div>
          <div>
            <strong>{data.totals.failed}</strong>
            <span>Failed</span>
          </div>
        </div>
      ) : null}

      <form
        className="pa-filters"
        onSubmit={(event) => {
          event.preventDefault()
          setSearch(query.trim())
        }}
      >
        <div className="pa-seg" role="tablist">
          {SOURCES.map((s) => (
            <button key={s.key || 'all'} type="button" className={source === s.key ? 'is-active' : ''} onClick={() => setSource(s.key)}>
              {s.label}
            </button>
          ))}
        </div>
        <select className="input input-narrow" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          {STATUSES.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
        <input className="input pa-search" placeholder="Search website or listing" value={query} onChange={(e) => setQuery(e.target.value)} />
        <button type="submit" className="btn btn-secondary btn-sm">
          Search
        </button>
      </form>

      {loading && !data ? (
        <Loading label="Loading analyses…" />
      ) : error && !data ? (
        <ErrorState message={error} onRetry={reload} />
      ) : !data || data.items.length === 0 ? (
        <EmptyState title="No analyses" description="Nothing matches these filters yet." />
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Date</th>
                <th>From</th>
                <th>Website</th>
                <th>Business</th>
                <th>Score</th>
                <th>AI model</th>
                <th>Took</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.items.map((row) => (
                <tr key={`${row.source}:${row.id}`}>
                  <td>{when(row.created_at ?? '')}</td>
                  <td>
                    {row.source === 'website' ? (
                      <span className="pa-tag">Website visitor</span>
                    ) : (
                      <span>
                        {row.workspace}
                        {row.requested_by ? <span className="muted small"> · {row.requested_by}</span> : null}
                      </span>
                    )}
                  </td>
                  <td>
                    <strong>{host(row.website)}</strong>
                    {row.business_url ? <div className="muted small pa-ellipsis">{row.business_url}</div> : null}
                  </td>
                  <td>{row.business_name || '–'}</td>
                  <td>
                    {row.status === 'done' ? (
                      <span>
                        {row.score ?? '–'} <span className="muted">{row.grade}</span>
                      </span>
                    ) : row.status === 'failed' ? (
                      <span className="sa-failed" title={row.error ?? ''}>
                        Failed
                      </span>
                    ) : (
                      <span className="muted">Running</span>
                    )}
                  </td>
                  <td>{row.model || '–'}</td>
                  <td>{row.seconds !== null ? `${Math.round(row.seconds / 60) || '<1'} min` : '–'}</td>
                  <td className="table-actions">
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => void openRow(row)}>
                      Open
                    </button>
                    {row.status === 'done' ? (
                      <DownloadButtons
                        compact
                        onDownload={(format) => api.downloadPortalAnalysis(row, format).then(saveFile).catch(fromError)}
                      />
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open ? (
        <Drawer
          title={host(open.row.website)}
          subtitle={`${open.row.source === 'website' ? 'Website visitor' : open.row.workspace} · ${when(open.row.created_at ?? '')}`}
          width={1040}
          onClose={() => setOpen(null)}
        >
          {open.detail ? (
            <AnalysisView
              analysis={open.detail}
              onDownload={(format) => api.downloadPortalAnalysis(open.row, format).then(saveFile).catch(fromError)}
            />
          ) : (
            <Loading label="Loading report…" />
          )}
          {open.row.error ? <div className="banner is-danger">{open.row.error}</div> : null}
        </Drawer>
      ) : null}
    </section>
  )
}
