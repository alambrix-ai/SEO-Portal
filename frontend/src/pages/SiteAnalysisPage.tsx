/**
 * Site analysis: run the full website analysis from inside the portal and
 * keep every run as history.
 *
 * Written by the workspace's own connected AI model (picked here, all equal).
 * Each run records what was connected at the time and is compared with the
 * previous run of the same site, so the history shows what changed and why.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api } from '@/api/client'
import type { SiteAnalysisDetail, SiteAnalysisSummary } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { AnalysisProgress, DownloadButtons, ReportView, ScoreRing, host, saveFile, type ReportFormat } from '@/components/AnalysisReport'
import { VersionHistory } from '@/components/AuditHistory'
import { useConfirm } from '@/components/Confirm'
import { Change, Delta, Trend } from '@/components/HistoryParts'
import { useToasts } from '@/components/Toasts'
import { EmptyState, ErrorState, Field, Loading, PageHeader } from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { when } from '@/lib/time'

const ACTIVE = ['queued', 'running']

export function SiteAnalysisPage() {
  const { id } = useParams()
  return id ? <AnalysisDetail id={id} /> : <AnalysisHome />
}

// ── History and run form ───────────────────────────────────────────────────
function AnalysisHome() {
  const { canWrite } = useAuth()
  const writable = canWrite('seo')
  const navigate = useNavigate()
  const { push, fromError } = useToasts()
  const confirm = useConfirm()
  const { data, loading, error, reload } = useResource((signal) => api.siteAnalyses(signal), [], 'site-analyses')
  const [website, setWebsite] = useState('')
  const [business, setBusiness] = useState('')
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState<SiteAnalysisDetail | null>(null)
  const timer = useRef<number>()

  useEffect(() => {
    if (!data) return
    setWebsite((w) => w || data.default_website)
    setModel((m) => (m && data.models.some((x) => x.slug === m) ? m : data.models[0]?.slug ?? ''))
    const running = data.analyses.find((a) => ACTIVE.includes(a.status))
    if (running && !live) follow(running.id)
  }, [data])

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const follow = (analysisId: string) => {
    window.clearTimeout(timer.current)
    const tick = async () => {
      try {
        const next = await api.siteAnalysis(analysisId)
        setLive(next)
        if (ACTIVE.includes(next.status)) {
          timer.current = window.setTimeout(tick, 3000)
        } else {
          setLive(null)
          await reload()
          if (next.status === 'done') push('Analysis finished', 'success')
          else push(next.error ?? 'The analysis failed', 'error')
        }
      } catch (caught) {
        fromError(caught)
      }
    }
    void tick()
  }

  const start = async () => {
    setBusy(true)
    try {
      const created = await api.startSiteAnalysis({ website_url: website.trim(), business_url: business.trim(), llm_connector: model })
      setLive(created)
      follow(created.id)
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (row: SiteAnalysisSummary) => {
    if (!(await confirm({ title: 'Delete this analysis?', message: `The ${when(row.created_at ?? '')} report for ${host(row.website)} is removed from the history.`, confirmLabel: 'Delete', tone: 'danger' })))
      return
    try {
      await api.deleteSiteAnalysis(row.id)
      await reload()
    } catch (caught) {
      fromError(caught)
    }
  }

  if (loading && !data) return <Loading label="Loading analyses…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const done = data.analyses.filter((a) => a.status === 'done' && a.score !== null)
  const latest = done[0]
  const trend = latest ? done.filter((a) => host(a.website) === host(latest.website)).reverse() : []

  return (
    <>
      <PageHeader
        title="Site analysis"
        description="A full audit of your website — SEO, speed, AI search, local, tracking — with a strategy and ready-to-use marketing. Every run is kept so you can see what changed."
      />

      <div className="sa-top">
        <section className="panel sa-run">
          <div className="panel-head">
            <div>
              <div className="panel-title">Run an analysis</div>
              <div className="panel-subtitle">Takes about three minutes. Written by your own connected AI model.</div>
            </div>
          </div>
          {live ? (
            <div className="sa-live">
              <AnalysisProgress analysis={live} />
            </div>
          ) : (
            <form
              className="sa-form"
              onSubmit={(event) => {
                event.preventDefault()
                void start()
              }}
            >
              <Field label="Website" htmlFor="sa-site">
                <input id="sa-site" className="input" value={website} placeholder="https://yourbusiness.com" onChange={(e) => setWebsite(e.target.value)} disabled={!writable} />
              </Field>
              <Field label="Business listing (optional)" htmlFor="sa-biz" hint="Google Maps, Facebook or Yelp link.">
                <input id="sa-biz" className="input" value={business} onChange={(e) => setBusiness(e.target.value)} disabled={!writable} />
              </Field>
              <Field label="AI model" htmlFor="sa-model" hint={data.models.length ? 'Writes the strategy, keywords, ads, posts and emails.' : undefined}>
                {data.models.length ? (
                  <select id="sa-model" className="input" value={model} onChange={(e) => setModel(e.target.value)} disabled={!writable}>
                    {data.models.map((m) => (
                      <option key={m.slug} value={m.slug}>
                        {m.name}
                      </option>
                    ))}
                  </select>
                ) : (
                  <div className="banner is-warning">
                    Connect an AI model first — <Link to="/connectors">OpenAI, Anthropic Claude, Google Gemini or Perplexity</Link>.
                  </div>
                )}
              </Field>
              {writable ? (
                <div className="sa-actions">
                  <button type="submit" className="btn btn-primary" disabled={busy || !data.models.length || !website.trim()}>
                    {busy ? 'Starting…' : 'Run analysis'}
                  </button>
                </div>
              ) : null}
            </form>
          )}
        </section>

        {latest ? (
          <section className="panel sa-latest">
            <div className="panel-head">
              <div>
                <div className="panel-title">Latest score</div>
                <div className="panel-subtitle">
                  {host(latest.website)} · {when(latest.created_at ?? '')}
                </div>
              </div>
            </div>
            <div className="sa-latest-body">
              <ScoreRing score={latest.score} grade={latest.grade ?? undefined} size={112} />
              <div>
                <Change value={latest.change} long />
                {trend.length > 1 ? <Trend points={trend.map((a) => a.score ?? 0)} /> : <p className="small muted">Run it again later to see a trend.</p>}
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate(`/analysis/${latest.id}`)}>
                  Open report
                </button>
              </div>
            </div>
          </section>
        ) : null}
      </div>

      <section className="panel sa-history">
        <div className="panel-head">
          <div>
            <div className="panel-title">History</div>
            <div className="panel-subtitle">Every analysis run in this workspace, newest first.</div>
          </div>
        </div>
        {data.analyses.length === 0 ? (
          <EmptyState title="No analyses yet" description="Run your first analysis above. Each run is kept here with its score and what was connected." />
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Website</th>
                  <th>Score</th>
                  <th>Change</th>
                  <th>To fix</th>
                  <th>AI model</th>
                  <th>Connected</th>
                  <th>Run by</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.analyses.map((row) => (
                  <tr key={row.id}>
                    <td>{when(row.created_at ?? '')}</td>
                    <td>
                      <strong>{host(row.website)}</strong>
                    </td>
                    <td>
                      {row.status === 'done' ? (
                        <span className={`sa-score is-${scoreTone(row.score)}`}>
                          {row.score ?? '–'} <span className="muted">{row.grade}</span>
                        </span>
                      ) : row.status === 'failed' ? (
                        <span className="sa-failed" title={row.error ?? ''}>
                          Failed
                        </span>
                      ) : (
                        <span className="muted">Running… {row.percent}%</span>
                      )}
                    </td>
                    <td>
                      <Change value={row.change} />
                    </td>
                    <td>{row.issues ?? '–'}</td>
                    <td>{row.model}</td>
                    <td>{row.connectors} integrations</td>
                    <td>{row.requested_by || '–'}</td>
                    <td className="table-actions">
                      {row.status === 'done' ? (
                        <>
                          <Link to={`/analysis/${row.id}`} className="btn btn-ghost btn-sm">
                            Open
                          </Link>
                          <DownloadButtons
                            compact
                            onDownload={(format) => api.downloadSiteAnalysis(row.id, format).then(saveFile).catch(fromError)}
                          />
                        </>
                      ) : null}
                      {writable && !ACTIVE.includes(row.status) ? (
                        <button type="button" className="btn btn-ghost btn-sm danger" onClick={() => void remove(row)}>
                          Delete
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <SiteVersions analyses={data.analyses} />
    </>
  )
}

// ── Compare runs of one site ───────────────────────────────────────────────
const SITE_COMPARE_HELP =
  "Compare this site's analysis with an earlier one. New: a check that started failing or needing attention. Fixed: a check that improved or now passes. Worse: a check that went from needing attention to failing."

/**
 * The website as the server groups its versions (`history.site_analysis.subject_for`):
 * lower-case host name without "www.". Null when the browser would spell the
 * host differently from the server (international or IPv6 addresses), so the
 * caller falls back to every site rather than showing an empty history.
 */
function siteKey(website: string): string | null {
  const raw = website.trim().toLowerCase()
  if (!raw || !/^[\x21-\x7e]+$/.test(raw)) return null
  let hostname: string
  try {
    hostname = new URL(raw.includes('://') ? raw : `https://${raw}`).hostname || raw
  } catch {
    return null
  }
  if (hostname.startsWith('[')) return null
  return hostname.startsWith('www.') ? hostname.slice(4) : hostname
}

/** The server's subject id for a site key: "site-" and the first 32 hex digits of its SHA-256. */
async function siteSubject(key: string): Promise<string | null> {
  // crypto.subtle only exists on https (and localhost); without it, show every site.
  if (!window.crypto?.subtle) return null
  const digest = await window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(key))
  const hex = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
  return `site-${hex.slice(0, 32)}`
}

/**
 * Numbered versions of one website's finished analyses, any two compared in
 * plain words. Analyses are read-only records: the server marks them not
 * restorable, so the kit shows no restore button.
 */
function SiteVersions({ analyses }: { analyses: SiteAnalysisSummary[] }) {
  const { canView } = useAuth()
  const sites = useMemo(() => {
    const seen = new Map<string, string>()
    for (const a of analyses) {
      if (a.status !== 'done') continue
      const key = siteKey(a.website)
      if (key && !seen.has(key)) seen.set(key, host(a.website))
    }
    return [...seen.entries()].map(([key, label]) => ({ key, label }))
  }, [analyses])
  const [picked, setPicked] = useState('')
  const site = sites.find((s) => s.key === picked) ?? sites[0]
  const [subject, setSubject] = useState<{ key: string; id: string | null } | null>(null)
  const refreshKey = analyses.filter((a) => a.status === 'done').length.toString()

  useEffect(() => {
    if (!site) return
    let live = true
    siteSubject(site.key).then(
      (id) => live && setSubject({ key: site.key, id }),
      // Hashing failed: fall back to every site; the compare picker still only pairs versions of one site.
      () => live && setSubject({ key: site.key, id: null }),
    )
    return () => {
      live = false
    }
  }, [site?.key])

  if (!canView('seo') || !site) return null
  const current = subject && subject.key === site.key ? subject : null
  return (
    <section className="sa-versions" aria-label="Compare analyses">
      {sites.length > 1 ? (
        <Field label="Website to compare" htmlFor="sa-version-site">
          <select id="sa-version-site" className="input" value={site.key} onChange={(e) => setPicked(e.target.value)}>
            {sites.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
        </Field>
      ) : null}
      {!current ? (
        <Loading label="Loading versions…" />
      ) : (
        <>
          {current.id === null && sites.length > 1 ? (
            <p className="small muted">Showing the versions of every website. Only two versions of the same website can be compared.</p>
          ) : null}
          <VersionHistory
            kind="site_analysis"
            subject={current.id}
            title={current.id ? `Compare analyses of ${site.label}` : 'Compare analyses'}
            description={SITE_COMPARE_HELP}
            refreshKey={refreshKey}
          />
        </>
      )}
    </section>
  )
}

function scoreTone(score: number | null) {
  if (score === null) return 'none'
  return score >= 80 ? 'good' : score >= 55 ? 'ok' : 'poor'
}

// ── One analysis ───────────────────────────────────────────────────────────
type DetailTab = 'report' | 'changes' | 'context'

function AnalysisDetail({ id }: { id: string }) {
  const { fromError } = useToasts()
  const { data, loading, error, reload } = useResource((signal) => api.siteAnalysis(id, signal), [id], `site-analysis:${id}`)
  return (
    <>
      <p className="sa-back">
        <Link to="/analysis">← All analyses</Link>
      </p>
      {loading && !data ? (
        <Loading label="Loading analysis…" />
      ) : error && !data ? (
        <ErrorState message={error} onRetry={reload} />
      ) : data ? (
        <AnalysisView
          analysis={data}
          inPortal
          onDownload={(format) => api.downloadSiteAnalysis(data.id, format).then(saveFile).catch(fromError)}
        />
      ) : null}
    </>
  )
}

/** Report, changes and context tabs — also used by Portal Admin. */
export function AnalysisView({
  analysis,
  inPortal = false,
  onDownload,
}: {
  analysis: SiteAnalysisDetail
  inPortal?: boolean
  /** Shows Download PDF / HTML when given. */
  onDownload?: (format: ReportFormat) => Promise<void>
}) {
  const [tab, setTab] = useState<DetailTab>('report')
  const ctx = analysis.context ?? {}
  const tabs = useMemo(
    () =>
      [
        { key: 'report' as const, label: 'Report' },
        ...(analysis.comparison !== undefined ? [{ key: 'changes' as const, label: 'What changed' }] : []),
        ...(analysis.context ? [{ key: 'context' as const, label: 'Connected at the time' }] : []),
      ],
    [analysis],
  )

  if (analysis.status !== 'done' || !analysis.report) {
    return analysis.status === 'failed' ? (
      <div className="banner is-danger">{analysis.error ?? 'This analysis failed.'}</div>
    ) : (
      <AnalysisProgress analysis={analysis} />
    )
  }

  return (
    <div className="sa-detail">
      <div className="sa-detail-bar">
        {tabs.length > 1 ? (
          <div className="tabs" role="tablist">
            {tabs.map((t) => (
              <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} className={`tab${tab === t.key ? ' is-active' : ''}`} onClick={() => setTab(t.key)}>
                {t.label}
              </button>
            ))}
          </div>
        ) : (
          <span />
        )}
        {onDownload ? <DownloadButtons onDownload={onDownload} /> : null}
      </div>

      {tab === 'report' ? <ReportView report={analysis.report} inPortal={inPortal} /> : null}

      {tab === 'changes' ? (
        analysis.comparison ? (
          <div className="sa-changes">
            <section className="panel sa-pad">
              <div className="panel-title">Since {when(analysis.comparison.previous_at ?? '')}</div>
              <div className="sa-delta-grid">
                <Delta label="Overall" value={analysis.comparison.overall} />
                {Object.entries(analysis.comparison.categories).map(([key, c]) => (
                  <Delta key={key} label={c.label} value={c} />
                ))}
              </div>
            </section>
            <div className="sa-two">
              <section className="panel sa-pad">
                <div className="panel-title">Fixed ({analysis.comparison.fixed.length})</div>
                {analysis.comparison.fixed.length ? (
                  <ul className="sa-list">
                    {analysis.comparison.fixed.map((f) => (
                      <li key={f.title}>
                        <strong>{f.title}</strong>
                        <span className="muted small">{f.after}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted small">Nothing newly fixed.</p>
                )}
              </section>
              <section className="panel sa-pad">
                <div className="panel-title">New issues ({analysis.comparison.new_issues.length})</div>
                {analysis.comparison.new_issues.length ? (
                  <ul className="sa-list">
                    {analysis.comparison.new_issues.map((f) => (
                      <li key={f.title}>
                        <strong>{f.title}</strong>
                        <span className="muted small">{f.evidence}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted small">No new issues.</p>
                )}
              </section>
            </div>
            {analysis.comparison.still_open.length ? (
              <p className="small muted">Still open: {analysis.comparison.still_open.join(' · ')}</p>
            ) : null}
            {analysis.comparison.connectors_added.length || analysis.comparison.connectors_removed.length ? (
              <p className="small">
                {analysis.comparison.connectors_added.length ? `Connected since: ${analysis.comparison.connectors_added.join(', ')}. ` : ''}
                {analysis.comparison.connectors_removed.length ? `Disconnected since: ${analysis.comparison.connectors_removed.join(', ')}.` : ''}
              </p>
            ) : null}
          </div>
        ) : (
          <EmptyState title="Nothing to compare yet" description="This is the first completed analysis of this site. Run it again later to see what changed." />
        )
      ) : null}

      {tab === 'context' ? (
        <div className="sa-two">
          <section className="panel sa-pad">
            <div className="panel-title">Integrations ({ctx.connectors?.length ?? 0})</div>
            <p className="small muted">
              Written by {ctx.llm?.name ?? analysis.model}. Speed measured with {ctx.pagespeed === 'Platform' || !ctx.pagespeed ? "the platform's PageSpeed access" : 'your PageSpeed Insights connection'}.
            </p>
            {ctx.connectors?.length ? (
              <ul className="sa-list">
                {ctx.connectors.map((c) => (
                  <li key={c.slug}>
                    <strong>{c.name}</strong>
                    <span className={`small ${c.health === 'failing' ? 'sa-down' : 'muted'}`}>
                      {c.category}
                      {c.health === 'failing' ? ` · problem: ${c.last_error || 'failing'}` : c.health === 'ok' ? ' · working' : ''}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted small">Nothing was connected.</p>
            )}
          </section>
          <section className="panel sa-pad">
            <div className="panel-title">Agents ({ctx.agents?.length ?? 0})</div>
            {ctx.agents?.length ? (
              <ul className="sa-list">
                {ctx.agents.map((a) => (
                  <li key={a.slug}>
                    <strong>{a.name}</strong>
                    <span className="small muted">
                      {a.status === 'running' ? 'On' : a.status === 'paused' ? 'Paused' : a.status === 'error' ? 'Problem' : a.status}
                      {a.configured ? '' : ' · not set up'}
                      {a.last_run_at ? ` · last ran ${when(a.last_run_at)}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted small">No agents.</p>
            )}
          </section>
        </div>
      ) : null}
    </div>
  )
}
