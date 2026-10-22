/**
 * History for the portal's audits, in the same shape as Site analysis: a
 * trend, a table of runs with the change since the one before, and a drawer
 * with what changed.
 *
 * - Technical SEO: a snapshot after each Technical SEO Auditor run.
 * - SEO & AEO: a snapshot of the page scores after each On-Page SEO Sync run.
 * - Local rankings: read from every check Local Search Tracker has stored.
 * - Brand profile: a version on each create, save with changes, and restore.
 *
 * Snapshot history begins when it was switched on; the first visit records
 * the state at that moment as a labelled baseline.
 *
 * The version kit at the bottom works for every history kind (brand kits,
 * products, agent settings, workspace policy, creative text, site analysis
 * and the analyses above): `VersionHistory` combines a numbered, paginated
 * `VersionList`, `CompareView` for any two versions in plain words, a detail
 * drawer and `RestoreButton`. `RetentionSettings` is the admin panel for how
 * long versions are kept. Nothing here ever shows raw JSON.
 */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'

import { api } from '@/api/client'
import {
  HISTORY_PAGE_DEFAULT,
  KIND_MODULE,
  KIND_NAMES,
  describeActor,
  historyApi,
  measureValue,
  needsConfirmation,
  retentionProblem,
  versionName,
  type ChangeRow,
  type Comparison,
  type Measure,
  type RestoreResult,
  type RetentionChange,
  type RetentionKind,
  type RetentionView,
  type VersionDetail,
  type VersionEntry,
  type VersionKind,
} from '@/api/history'
import type {
  BrandFieldDiff,
  BrandProfileState,
  BrandVersionDetail,
  HistoryEntry,
  HistoryIssue,
  HistoryKind,
  HistoryList,
  Listed,
  LocalCheckDetail,
  PhraseMove,
  RankedPlace,
  ScoredPage,
  ScoresComparison,
  SnapshotDetail,
  TechnicalComparison,
} from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useConfirm } from '@/components/Confirm'
import { Change, Delta, Trend } from '@/components/HistoryParts'
import { useToasts } from '@/components/Toasts'
import { Drawer, EmptyState, ErrorState, Loading, StatusDot, Tag } from '@/components/ui'
import { friendlyLoadError, invalidateResourceCache, useResource } from '@/hooks/useResource'
import { when } from '@/lib/time'
import '@/styles/history.css'

const SEVERITY_TONE: Record<string, 'danger' | 'warning' | 'neutral'> = { high: 'danger', medium: 'warning', low: 'neutral' }

function dateOnly(iso: string | null | undefined) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : ''
}

function useHistory(kind: HistoryKind, refreshKey = '') {
  return useResource((signal) => api.history(kind, signal), [kind, refreshKey], `history:${kind}:${refreshKey}`)
}

/** Says where the history starts, so an empty past is not mistaken for no activity. */
function StartNote({ data, what }: { data: HistoryList; what: string }) {
  if (!data.started_at) return null
  return (
    <p className="small muted hist-note">
      The earliest entry kept is from {dateOnly(data.started_at)}. History starts when it was switched on for this
      workspace, so {what} from before then are not included.
    </p>
  )
}

function SourceCell({ entry }: { entry: HistoryEntry }) {
  return (
    <span className="hist-source">
      {entry.source === 'baseline' ? <Tag tone="outline">Baseline</Tag> : null}
      <span>{entry.source === 'baseline' ? 'Current state when history started' : entry.label}</span>
    </span>
  )
}

function Summary({
  title,
  value,
  change,
  lowerIsBetter,
  unit = '',
  trend,
}: {
  title: string
  value: ReactNode
  change: number | null
  lowerIsBetter?: boolean
  unit?: string
  trend: ReactNode
}) {
  return (
    <section className="panel sa-pad hist-summary">
      <div>
        <div className="small muted">{title}</div>
        <div className="hist-big">{value}</div>
        <Change value={change} long lowerIsBetter={lowerIsBetter} unit={unit} first="Only one entry so far" />
      </div>
      <div className="hist-trend">{trend}</div>
    </section>
  )
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <section className="panel sa-history hist-section">
      <div className="panel-head">
        <div>
          <div className="panel-title">{title}</div>
          {subtitle ? <div className="panel-subtitle">{subtitle}</div> : null}
        </div>
      </div>
      {children}
    </section>
  )
}

function DetailDrawer<T>({
  kind,
  id,
  title,
  onClose,
  actions,
  children,
}: {
  kind: HistoryKind
  id: string
  title: string
  onClose: () => void
  actions?: (data: T) => ReactNode
  children: (data: T) => ReactNode
}) {
  const { data, loading, error, reload } = useResource((signal) => api.historyEntry<T>(kind, id, signal), [kind, id], `history-entry:${kind}:${id}`)
  return (
    <Drawer title={title} width={760} onClose={onClose} actions={data && actions ? actions(data) : undefined}>
      {loading && !data ? <Loading label="Loading…" /> : error && !data ? <ErrorState message={error} onRetry={reload} /> : data ? children(data) : null}
    </Drawer>
  )
}

function Compared({ previousAt, previousLabel }: { previousAt: string | null; previousLabel?: string }) {
  return (
    <p className="small muted hist-note">
      Compared with the entry before it{previousLabel ? ` (${previousLabel.toLowerCase()})` : ''}, {when(previousAt ?? '')}.
    </p>
  )
}

function FirstEntry() {
  return <EmptyState title="Nothing to compare yet" description="This is the first entry in the history. The next one will be compared with it." />
}

function Truncated({ show }: { show: boolean }) {
  return show ? (
    <div className="banner is-warning hist-note">
      This site has more than 5,000 items, and a history entry keeps the first 5,000 (the most serious first). The counts are complete; the lists below may leave some out.
    </div>
  ) : null
}

function More({ listed }: { listed: Listed<unknown> }) {
  return listed.count > listed.items.length ? (
    <p className="small muted">…and {listed.count - listed.items.length} more.</p>
  ) : null
}

// ── Technical SEO ──────────────────────────────────────────────────────────
export function TechnicalHistory() {
  const { data, loading, error, reload } = useHistory('technical_seo')
  const [open, setOpen] = useState<HistoryEntry | null>(null)

  if (loading && !data) return <Loading label="Loading history…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null
  if (!data.entries.length)
    return <EmptyState title="No history yet" description="An entry is kept after each successful run of the Technical SEO Auditor. Run it once to start the history." />

  const latest = data.entries[0]!
  const points = [...data.entries].reverse().map((e) => e.headline)
  return (
    <>
      <Summary
        title="Open issues"
        value={latest.headline ?? '–'}
        change={latest.change}
        lowerIsBetter
        trend={points.length > 1 ? <Trend points={points} max={Math.max(1, ...points.map((p) => p ?? 0))} label="Open issues over time" /> : null}
      />
      <Section title="Audit runs" subtitle="Each run of the Technical SEO Auditor, newest first. Open the run to see what was found and fixed.">
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Run</th>
                <th>Open issues</th>
                <th>Change</th>
                <th>High</th>
                <th>Pages audited</th>
                <th>Speed checked</th>
                <th>Run by</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.entries.map((e) => (
                <tr key={e.id}>
                  <td>{when(e.created_at)}</td>
                  <td><SourceCell entry={e} /></td>
                  <td><strong>{e.headline ?? '–'}</strong></td>
                  <td><Change value={e.change} lowerIsBetter /></td>
                  <td>{e.metrics.high ?? '–'}</td>
                  <td>{e.metrics.pages_audited ?? '–'}</td>
                  <td>{speedText(e.metrics.vitals_measured)}</td>
                  <td>{e.created_by || '–'}</td>
                  <td className="table-actions">
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(e)}>
                      What changed
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <StartNote data={data} what="audits" />
      </Section>
      {open ? (
        <DetailDrawer<SnapshotDetail> kind="technical_seo" id={open.id} title={`Audit · ${when(open.created_at)}`} onClose={() => setOpen(null)}>
          {(d) => <TechnicalDetail detail={d} />}
        </DetailDrawer>
      ) : null}
    </>
  )
}

function speedText(measured: number | null | undefined) {
  if (measured === null || measured === undefined) return <span className="muted">Not recorded</span>
  return measured === 0 ? <span className="muted">Not measured</span> : `${measured} pages`
}

function IssueList({ listed, empty }: { listed: Listed<HistoryIssue>; empty: string }) {
  if (!listed.count) return <p className="muted small">{empty}</p>
  return (
    <>
      <ul className="sa-list">
        {listed.items.map((i) => (
          <li key={i.id}>
            <span>
              <StatusDot tone={SEVERITY_TONE[i.severity] ?? 'neutral'}>{i.severity}</StatusDot> <strong>{i.label}</strong>
            </span>
            <span className="muted small hist-url">{i.url}{i.target ? ` → ${i.target}` : ''}</span>
          </li>
        ))}
      </ul>
      <More listed={listed} />
    </>
  )
}

function TechnicalDetail({ detail }: { detail: SnapshotDetail }) {
  const c = detail.comparison as TechnicalComparison | null
  if (!c) return <FirstEntry />
  const kinds = c.kinds.filter((k) => k.change)
  return (
    <div className="sa-changes">
      <Compared previousAt={c.previous_at} previousLabel={c.previous_label} />
      <Truncated show={c.truncated} />
      <div className="sa-delta-grid">
        <Delta label="Open issues" value={c.open} lowerIsBetter />
        <Delta label="High severity" value={c.severity.high} lowerIsBetter />
        <Delta label="Medium severity" value={c.severity.medium} lowerIsBetter />
        <Delta label="Low severity" value={c.severity.low} lowerIsBetter />
        <Delta label="Pages audited" value={{ ...c.pages_audited, change: null }} />
        <Delta label="Open mobile speed issues" value={{ before: c.speed.open_before, after: c.speed.open_after }} lowerIsBetter />
      </div>
      <p className="small muted">
        Pages measured for mobile speed in this run: {c.speed.measured_after === null ? 'not recorded' : c.speed.measured_after}.
        {' '}Speed is only measured when a page-speed service is connected, a few pages per run.
      </p>
      <div className="sa-two">
        <section className="panel sa-pad">
          <div className="panel-title">Newly found ({c.new_issues.count})</div>
          <IssueList listed={c.new_issues} empty="No new issues." />
        </section>
        <section className="panel sa-pad">
          <div className="panel-title">Fixed ({c.fixed.count})</div>
          <p className="small muted">Open last time and no longer found by the audit.</p>
          <IssueList listed={c.fixed} empty="Nothing newly fixed." />
        </section>
      </div>
      {c.ignored_since.count ? (
        <section className="panel sa-pad hist-section">
          <div className="panel-title">Ignored since ({c.ignored_since.count})</div>
          <IssueList listed={c.ignored_since} empty="" />
        </section>
      ) : null}
      {kinds.length ? (
        <section className="panel sa-pad hist-section">
          <div className="panel-title">By type</div>
          <table className="table hist-compact">
            <thead>
              <tr><th>Type</th><th>Before</th><th>Now</th><th>Change</th></tr>
            </thead>
            <tbody>
              {kinds.map((k) => (
                <tr key={k.kind}>
                  <td>{k.label}</td><td>{k.before}</td><td>{k.after}</td><td><Change value={k.change} lowerIsBetter /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
      {c.still_open.count ? (
        <details className="hist-details">
          <summary>Still open ({c.still_open.count})</summary>
          <IssueList listed={c.still_open} empty="" />
        </details>
      ) : null}
    </div>
  )
}

// ── SEO & AEO scores ───────────────────────────────────────────────────────
export function ScoreHistory() {
  const { data, loading, error, reload } = useHistory('seo_scores')
  const [open, setOpen] = useState<HistoryEntry | null>(null)

  if (loading && !data) return <Loading label="Loading history…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null
  if (!data.entries.length)
    return <EmptyState title="No history yet" description="An entry is kept after each successful run of On-Page SEO Sync. Run it once to start the history." />

  const latest = data.entries[0]!
  const points = [...data.entries].reverse().map((e) => e.headline)
  return (
    <>
      <Summary
        title="Average semantic gap (lower is better)"
        value={latest.headline === null ? '–' : `${latest.headline}%`}
        change={latest.change}
        lowerIsBetter
        unit="%"
        trend={points.length > 1 ? <Trend points={points} label="Average semantic gap over time" /> : null}
      />
      <Section
        title="Scoring runs"
        subtitle="The semantic gap is how much of a page's topic its copy leaves out. Only pages the agent has analysed are counted."
      >
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Run</th>
                <th>Average gap</th>
                <th>Change</th>
                <th>Pages scored</th>
                <th>Rewrites waiting</th>
                <th>Rewritten</th>
                <th>Run by</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.entries.map((e) => (
                <tr key={e.id}>
                  <td>{when(e.created_at)}</td>
                  <td><SourceCell entry={e} /></td>
                  <td><strong>{e.headline === null ? '–' : `${e.headline}%`}</strong></td>
                  <td><Change value={e.change} lowerIsBetter unit="%" /></td>
                  <td>{e.metrics.pages_scored ?? '–'} <span className="muted small">of {e.metrics.pages_total ?? '–'}</span></td>
                  <td>{e.metrics.rewrites_pending ?? '–'}</td>
                  <td>{e.metrics.rewritten ?? '–'}</td>
                  <td>{e.created_by || '–'}</td>
                  <td className="table-actions">
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(e)}>
                      What changed
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <StartNote data={data} what="scoring runs" />
      </Section>
      {open ? (
        <DetailDrawer<SnapshotDetail> kind="seo_scores" id={open.id} title={`Page scores · ${when(open.created_at)}`} onClose={() => setOpen(null)}>
          {(d) => <ScoresDetail detail={d} />}
        </DetailDrawer>
      ) : null}
    </>
  )
}

function PageList({ listed, empty, moves = false }: { listed: Listed<ScoredPage>; empty: string; moves?: boolean }) {
  if (!listed.count) return <p className="muted small">{empty}</p>
  return (
    <>
      <ul className="sa-list">
        {listed.items.map((p) => (
          <li key={p.url}>
            <strong>{p.title || p.url}</strong>
            <span className="small">
              {moves ? (
                <>
                  {p.before}% → {p.after}% <Change value={p.change ?? null} lowerIsBetter unit="%" />
                </>
              ) : (
                `Gap ${p.gap}%`
              )}
            </span>
            <span className="muted small hist-url">{p.url}</span>
          </li>
        ))}
      </ul>
      <More listed={listed} />
    </>
  )
}

function ScoresDetail({ detail }: { detail: SnapshotDetail }) {
  const c = detail.comparison as ScoresComparison | null
  if (!c) return <FirstEntry />
  return (
    <div className="sa-changes">
      <Compared previousAt={c.previous_at} previousLabel={c.previous_label} />
      <Truncated show={c.truncated} />
      <div className="sa-delta-grid">
        <Delta label="Average gap" value={c.average_gap} lowerIsBetter unit="%" />
        <Delta label="Pages scored" value={c.pages_scored} />
        <Delta label="Rewrites waiting" value={c.rewrites_pending} lowerIsBetter />
        <Delta label="Rewritten pages" value={c.rewritten} />
      </div>
      <section className="panel sa-pad hist-section">
        <div className="panel-title">Spread of scores</div>
        <table className="table hist-compact">
          <thead>
            <tr><th>Gap</th><th>Before</th><th>Now</th></tr>
          </thead>
          <tbody>
            {c.distribution.map((b) => (
              <tr key={b.key}><td>{b.label}</td><td>{b.before}</td><td>{b.after}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
      <div className="sa-two">
        <section className="panel sa-pad">
          <div className="panel-title">Improved ({c.improved.count})</div>
          <PageList listed={c.improved} empty="No page's gap went down." moves />
        </section>
        <section className="panel sa-pad">
          <div className="panel-title">Got worse ({c.worsened.count})</div>
          <PageList listed={c.worsened} empty="No page's gap went up." moves />
        </section>
      </div>
      <div className="sa-two">
        <section className="panel sa-pad">
          <div className="panel-title">Newly scored ({c.added.count})</div>
          <PageList listed={c.added} empty="No newly scored pages." />
        </section>
        <section className="panel sa-pad">
          <div className="panel-title">No longer listed ({c.removed.count})</div>
          <PageList listed={c.removed} empty="No pages removed." />
        </section>
      </div>
      <p className="small muted">
        {c.unchanged} pages kept the same score. When the agent publishes a rewrite, the page's gap is set just below
        the rewrite threshold (45%) until the page is analysed again, so it can show as improved before it is re-measured.
      </p>
    </div>
  )
}

// ── Local rankings ─────────────────────────────────────────────────────────
function rank(position: number | null | undefined, seen?: number) {
  return position === null || position === undefined ? `Not in the top ${seen || 20}` : `#${position}`
}

function rankTone(position: number | null): 'success' | 'info' | 'warning' {
  return position === null ? 'warning' : position <= 3 ? 'success' : position <= 10 ? 'info' : 'warning'
}

function Above({ places }: { places: RankedPlace[] }) {
  if (!places.length) return <span className="muted">Nobody</span>
  return (
    <span>
      {places.map((p, i) => (
        <span key={`${p.name}-${i}`}>
          {i ? ', ' : ''}
          {p.name}
          {p.rating !== null ? <span className="muted"> ({p.rating.toFixed(1)}★)</span> : null}
        </span>
      ))}
    </span>
  )
}

export function LocalRankHistory() {
  const { data, loading, error, reload } = useHistory('local_ranks')
  const [open, setOpen] = useState<HistoryEntry | null>(null)

  if (loading && !data) return <Loading label="Loading ranking history…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data || !data.entries.length) return null

  return (
    <div className="hist-local">
      <Section
        title="Position over time"
        subtitle="Each search phrase across the checks below. Higher on the chart is better; a gap means you were not in the results that day."
      >
        <div className="hist-phrases">
          {(data.trends ?? []).map((t) => {
            const seen = Math.max(20, ...t.points.map((p) => p.results_seen || 0))
            return (
              <div key={t.phrase} className="hist-phrase">
                <div className="row-between">
                  <strong>{t.phrase}</strong>
                  <StatusDot tone={rankTone(t.latest)}>{rank(t.latest, seen)}</StatusDot>
                </div>
                {t.points.length > 1 ? (
                  <Trend points={t.points.map((p) => p.position)} min={1} max={seen} invert label={`Position for ${t.phrase}`} width={220} height={40} />
                ) : (
                  <p className="small muted">One check so far.</p>
                )}
                <div className="small">
                  <span className="muted">{t.latest === null ? 'Top 3: ' : 'Above you: '}</span>
                  <Above places={t.above} />
                </div>
              </div>
            )
          })}
        </div>
      </Section>
      <Section title="Checks" subtitle="Every check Local Search Tracker has made, newest first. The top 3 are the map pack Google shows first.">
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Searches</th>
                <th>In the top 3</th>
                <th>Change</th>
                <th>Found in</th>
                <th>Best</th>
                <th>Average position</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.entries.map((e) => (
                <tr key={e.id}>
                  <td>{when(e.created_at)}</td>
                  <td>{e.metrics.phrases}</td>
                  <td><strong>{e.metrics.in_pack}</strong></td>
                  <td><Change value={e.change} /></td>
                  <td>{e.metrics.found_in} of {e.metrics.phrases}</td>
                  <td>{e.metrics.best ? `#${e.metrics.best}` : '–'}</td>
                  <td>{e.metrics.average_position ?? '–'}</td>
                  <td className="table-actions">
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(e)}>
                      What changed
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="small muted hist-note">Average position counts only the searches where you were found.</p>
      </Section>
      {open ? (
        <DetailDrawer<LocalCheckDetail> kind="local_ranks" id={open.id} title={`Ranking check · ${when(open.created_at)}`} onClose={() => setOpen(null)}>
          {(d) => <LocalDetail detail={d} />}
        </DetailDrawer>
      ) : null}
    </div>
  )
}

function Moves({ title, rows, empty }: { title: string; rows: PhraseMove[]; empty: string }) {
  return (
    <section className="panel sa-pad">
      <div className="panel-title">{title} ({rows.length})</div>
      {rows.length ? (
        <ul className="sa-list">
          {rows.map((r) => (
            <li key={r.phrase}>
              <strong>{r.phrase}</strong>
              <span className="small">{rank(r.before)} → {rank(r.after)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">{empty}</p>
      )}
    </section>
  )
}

function LocalDetail({ detail }: { detail: LocalCheckDetail }) {
  const c = detail.comparison
  return (
    <div className="sa-changes">
      <section className="panel sa-pad hist-section">
        <div className="panel-title">Positions in this check</div>
        <table className="table hist-compact">
          <thead>
            <tr><th>Search</th><th>Your position</th><th>Listed above you</th></tr>
          </thead>
          <tbody>
            {detail.phrases.map((p) => (
              <tr key={p.phrase}>
                <td>{p.phrase}</td>
                <td><StatusDot tone={rankTone(p.position)}>{rank(p.position, p.results_seen)}</StatusDot></td>
                <td className="small"><Above places={p.above} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {c ? (
        <>
          <Compared previousAt={c.previous_at} />
          <div className="sa-two">
            <Moves title="Moved up" rows={c.moved_up} empty="No search moved up." />
            <Moves title="Moved down" rows={c.moved_down} empty="No search moved down." />
          </div>
          <div className="sa-two">
            <Moves title="Entered the top 3" rows={c.entered_pack} empty="None entered the top 3." />
            <Moves title="Left the top 3" rows={c.left_pack} empty="None left the top 3." />
          </div>
          {c.entered_results.length || c.dropped_out.length ? (
            <div className="sa-two">
              <Moves title="Now in the results" rows={c.entered_results} empty="None." />
              <Moves title="Dropped out of the results" rows={c.dropped_out} empty="None." />
            </div>
          ) : null}
          {c.new_phrases.length || c.stopped_phrases.length ? (
            <p className="small muted">
              {c.new_phrases.length ? `Searches checked for the first time: ${c.new_phrases.join(', ')}. ` : ''}
              {c.stopped_phrases.length ? `No longer checked: ${c.stopped_phrases.join(', ')}.` : ''}
            </p>
          ) : null}
        </>
      ) : (
        <FirstEntry />
      )}
    </div>
  )
}

// ── Brand profile versions ─────────────────────────────────────────────────
export function BrandVersions({
  refreshKey,
  writable,
  onRestored,
}: {
  refreshKey: string
  writable: boolean
  onRestored: (state: BrandProfileState) => void
}) {
  const { data, loading, error, reload } = useHistory('brand_profile', refreshKey)
  const [open, setOpen] = useState<HistoryEntry | null>(null)

  if (loading && !data) return <Loading label="Loading versions…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null
  if (!data.entries.length)
    return <EmptyState title="No versions yet" description="A version is kept each time the profile is created from your website, saved with changes, or restored." />

  return (
    <>
      <Section
        title="Versions"
        subtitle="A version is kept each time the profile is created from your website, saved with changes, or restored. Open one to compare it with the current profile."
      >
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Date</th>
                <th>What happened</th>
                <th>By</th>
                <th>Changed from the version before</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.entries.map((e) => (
                <tr key={e.id}>
                  <td>{when(e.created_at)}</td>
                  <td>
                    <SourceCell entry={e} /> {e.is_current ? <Tag tone="accent">Current</Tag> : null}
                  </td>
                  <td>{e.created_by || '–'}</td>
                  <td className="small">
                    {e.changed_fields === null || e.changed_fields === undefined
                      ? <span className="muted">Earliest version kept</span>
                      : e.changed_fields.length
                        ? e.changed_fields.join(', ')
                        : <span className="muted">No field changes</span>}
                  </td>
                  <td className="table-actions">
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(e)}>
                      Compare
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <StartNote data={data} what="changes" />
      </Section>
      {open ? (
        <VersionDrawer
          entry={open}
          writable={writable}
          onClose={() => setOpen(null)}
          onRestored={async (state) => {
            setOpen(null)
            onRestored(state)
            await reload()
          }}
        />
      ) : null}
    </>
  )
}

function VersionDrawer({
  entry,
  writable,
  onClose,
  onRestored,
}: {
  entry: HistoryEntry
  writable: boolean
  onClose: () => void
  onRestored: (state: BrandProfileState) => Promise<void>
}) {
  const confirm = useConfirm()
  const { push, fromError } = useToasts()
  const [onlyChanges, setOnlyChanges] = useState(true)
  const [busy, setBusy] = useState(false)

  const restore = async (detail: BrandVersionDetail) => {
    const changed = detail.diff.filter((d) => d.changed).length
    const ok = await confirm({
      title: 'Restore this version?',
      message: `The brand profile goes back to the version from ${when(detail.created_at)}. ${changed} ${changed === 1 ? 'field changes' : 'fields change'}.`,
      detail: 'The current profile is kept in the version list, so you can restore it again later. The time zone is not changed.',
      confirmLabel: 'Restore this version',
    })
    if (!ok) return
    setBusy(true)
    try {
      const state = await api.restoreBrandVersion(detail.id)
      push('Brand profile restored', 'success')
      await onRestored(state)
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <DetailDrawer<BrandVersionDetail>
      kind="brand_profile"
      id={entry.id}
      title={`Version · ${when(entry.created_at)}`}
      onClose={onClose}
      actions={(d) =>
        writable && !d.is_current ? (
          <>
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              Close
            </button>
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void restore(d)}>
              {busy ? 'Restoring…' : 'Restore this version'}
            </button>
          </>
        ) : undefined
      }
    >
      {(d) => {
        const rows = onlyChanges ? d.diff.filter((r) => r.changed) : d.diff
        return (
          <div className="sa-changes">
            <p className="small muted hist-note">
              {d.label}
              {d.created_by ? ` by ${d.created_by}` : ''}.{' '}
              {d.is_current ? 'This version is the current profile.' : 'Shown against the current profile. Highlighted list items appear on one side only.'}
            </p>
            <label className="hist-toggle small">
              <input type="checkbox" checked={onlyChanges} onChange={(e) => setOnlyChanges(e.target.checked)} /> Only fields that differ
            </label>
            {rows.length ? (
              <table className="table hist-compact hist-diff">
                <thead>
                  <tr><th>Field</th><th>This version</th><th>Current</th></tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.field} className={r.changed ? 'is-changed' : undefined}>
                      <td><strong>{r.label}</strong></td>
                      <td><FieldValue row={r} side="version" /></td>
                      <td><FieldValue row={r} side="current" /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted small">This version is the same as the current profile.</p>
            )}
          </div>
        )
      }}
    </DetailDrawer>
  )
}

function FieldValue({ row, side }: { row: BrandFieldDiff; side: 'version' | 'current' }) {
  const value = row[side]
  if (row.type === 'text') return value ? <span>{value as string}</span> : <span className="muted">Empty</span>
  const items = value as string[]
  if (!items.length) return <span className="muted">Empty</span>
  const only = new Set((side === 'version' ? row.only_in_version : row.only_in_current) ?? [])
  return (
    <ul className="hist-values">
      {items.map((item) => (
        <li key={item} className={only.has(item) ? 'is-only' : undefined}>
          {item}
        </li>
      ))}
    </ul>
  )
}

// ── Version kit (every history kind) ────────────────────────────────────────
/** A full date and time for a tooltip and `dateTime`, next to the short `when` text. */
function fullDate(iso: string | null | undefined) {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: 'long', timeStyle: 'short' }) : ''
}

function VersionTime({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="muted">date not recorded</span>
  return (
    <time dateTime={iso} title={fullDate(iso)}>
      {when(iso)}
    </time>
  )
}

/** One page after another, newest first, with "Show older versions" from `next_cursor`. */
function usePagedVersions(kind: VersionKind, subject: string | null, refreshKey: string) {
  const first = useResource(
    (signal) => historyApi.list(kind, { subject, limit: HISTORY_PAGE_DEFAULT }, signal),
    [kind, subject ?? '', refreshKey],
    `version-history:${kind}:${subject ?? ''}:${refreshKey}`,
  )
  const [older, setOlder] = useState<{ entries: VersionEntry[]; cursor: string | null } | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)

  // A fresh first page (reload, another item) starts the list again.
  useEffect(() => {
    controller.current?.abort()
    setOlder(null)
    setMoreError(null)
    setLoadingMore(false)
  }, [first.data])
  useEffect(() => () => controller.current?.abort(), [])

  const cursor = older ? older.cursor : (first.data?.next_cursor ?? null)
  const entries = useMemo(() => {
    const seen = new Set<string>()
    return [...(first.data?.entries ?? []), ...(older?.entries ?? [])].filter((e) => {
      if (seen.has(e.id)) return false
      seen.add(e.id)
      return true
    })
  }, [first.data, older])

  const loadMore = async () => {
    if (!cursor || loadingMore) return
    controller.current?.abort()
    const next = new AbortController()
    controller.current = next
    setLoadingMore(true)
    setMoreError(null)
    try {
      const page = await historyApi.list(kind, { subject, cursor, limit: HISTORY_PAGE_DEFAULT }, next.signal)
      if (next.signal.aborted) return
      setOlder((prev) => ({ entries: [...(prev?.entries ?? []), ...page.entries], cursor: page.next_cursor }))
    } catch (caught) {
      if (!next.signal.aborted) setMoreError(friendlyLoadError(caught))
    } finally {
      if (!next.signal.aborted) setLoadingMore(false)
    }
  }

  return { first, entries, hasMore: Boolean(cursor), loadMore, loadingMore, moreError }
}

/** The summary line of a version: its label, and for settings which fields changed. */
function entrySummary(entry: VersionEntry): string {
  if (entry.changed_fields === undefined) return entry.label
  if (entry.changed_fields === null) return `${entry.label} · earliest version kept`
  if (!entry.changed_fields.length) return `${entry.label} · no field changes`
  return `${entry.label} · changed: ${entry.changed_fields.join(', ')}`
}

/**
 * Numbered versions, newest first, as a real list: each one says when, who
 * and what, with a button to open it. Older pages load on request.
 */
export function VersionList({
  entries,
  hasMore,
  loadingMore,
  moreError,
  onLoadMore,
  onOpen,
}: {
  entries: VersionEntry[]
  hasMore: boolean
  loadingMore: boolean
  moreError: string | null
  onLoadMore: () => void
  onOpen: (entry: VersionEntry) => void
}) {
  return (
    <div className="vh-list-wrap">
      <ol className="vh-list" aria-label="Versions, newest first">
        {entries.map((e) => (
          <li key={e.id} className="vh-item">
            <div className="vh-item-main">
              <div className="vh-item-head">
                <span className="vh-num">{versionName(e)}</span>
                {e.is_current ? <Tag tone="accent">Current</Tag> : null}
                {e.source === 'baseline' ? <Tag tone="outline">Starting point</Tag> : null}
                {e.source === 'restored' ? <Tag tone="outline">Restored</Tag> : null}
              </div>
              <div className="vh-meta small">
                <VersionTime iso={e.created_at} />
                <span aria-hidden="true"> · </span>
                <span className="sr-only">, by </span>
                <span>{describeActor(e)}</span>
              </div>
              <div className="vh-summary">{entrySummary(e)}</div>
            </div>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onOpen(e)}
              aria-label={`See details of ${versionName(e)}`}
            >
              See details
            </button>
          </li>
        ))}
      </ol>
      <div className="vh-more" aria-live="polite">
        {moreError ? (
          <div className="banner is-warning" role="alert">
            {moreError}
          </div>
        ) : null}
        {hasMore ? (
          <button type="button" className="btn btn-secondary btn-sm" disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore ? 'Loading older versions…' : 'Show older versions'}
          </button>
        ) : (
          <span className="small muted">
            {entries.length === 1 ? 'This is the only version kept.' : `All ${entries.length} versions kept are shown.`}
          </span>
        )}
      </div>
    </div>
  )
}

// ── Comparison, in words ───────────────────────────────────────────────────
const COUNT_CHIPS: { key: keyof Comparison['counts']; label: string; tone: string }[] = [
  { key: 'new', label: 'New', tone: 'is-new' },
  { key: 'fixed', label: 'Fixed', tone: 'is-good' },
  { key: 'worse', label: 'Worse', tone: 'is-bad' },
  { key: 'changed', label: 'Changed', tone: 'is-changed' },
  { key: 'unchanged', label: 'Unchanged', tone: 'is-same' },
]

const ROW_GROUPS: { key: 'new' | 'fixed' | 'worse' | 'changed'; title: string; tone: string }[] = [
  { key: 'worse', title: 'Got worse', tone: 'is-bad' },
  { key: 'new', title: 'New', tone: 'is-new' },
  { key: 'fixed', title: 'Fixed or better', tone: 'is-good' },
  { key: 'changed', title: 'Changed', tone: 'is-changed' },
]

/** ▲/▼ for the direction and a word for whether it is good news, so colour is never the only signal. */
function MeasureVerdict({ m }: { m: Measure }) {
  if (m.change === null || m.change === undefined) return <span className="muted small">Can't compare</span>
  if (m.change === 0 || m.verdict === 'same') return <span className="vh-verdict is-same">No change</span>
  const arrow = m.change > 0 ? '▲' : '▼'
  const amount = measureValue(Math.abs(m.change))
  const word = m.verdict === 'better' ? 'better' : m.verdict === 'worse' ? 'worse' : m.change > 0 ? 'higher' : 'lower'
  const tone = m.verdict === 'better' ? 'is-good' : m.verdict === 'worse' ? 'is-bad' : 'is-changed'
  return (
    <span className={`vh-verdict ${tone}`}>
      <span aria-hidden="true">{arrow}</span> {m.change > 0 ? 'Up' : 'Down'} {amount}, {word}
    </span>
  )
}

function ChangeLine({ row }: { row: ChangeRow }) {
  return (
    <li className="vh-row">
      <strong className="vh-row-label">{row.label}</strong>
      {row.where ? <span className="small muted vh-row-where">{row.where}</span> : null}
      {row.before !== null || row.after !== null ? (
        <span className="small vh-row-move">
          {row.before !== null && row.after !== null ? (
            <>
              {row.before} <span aria-hidden="true">→</span>
              <span className="sr-only"> became </span> {row.after}
            </>
          ) : row.after !== null ? (
            `Now: ${row.after}`
          ) : (
            `Was: ${row.before}`
          )}
        </span>
      ) : null}
    </li>
  )
}

/** The body every comparison shares: sentence, counts, headline numbers, then rows by status. */
export function ComparisonBody({ comparison }: { comparison: Comparison }) {
  const headingBase = useId()
  const c = comparison
  return (
    <div className="vh-compare-body">
      <p className="vh-sentence">{c.summary}</p>
      <ul className="vh-counts" aria-label="Changes by type">
        {COUNT_CHIPS.map((chip) => (
          <li key={chip.key} className={`vh-count ${chip.tone}`}>
            <span className="vh-count-num">{(c.counts?.[chip.key] ?? 0).toLocaleString()}</span> {chip.label}
          </li>
        ))}
      </ul>
      {c.truncated ? (
        <div className="banner is-warning">
          These versions hold more items than the history keeps in full. The counts are complete; the lists below may
          leave some out.
        </div>
      ) : null}
      {c.measures?.length ? (
        <ul className="vh-measures" aria-label="Headline numbers">
          {c.measures.map((m) => (
            <li key={m.label} className="vh-measure">
              <span className="vh-measure-label">{m.label}</span>
              <span className="vh-measure-values">
                {measureValue(m.before)} <span aria-hidden="true">→</span>
                <span className="sr-only"> to </span> <strong>{measureValue(m.after)}</strong>
              </span>
              <MeasureVerdict m={m} />
              {m.better ? (
                <span className="small muted">{m.better === 'lower' ? 'Lower is better' : 'Higher is better'}</span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {ROW_GROUPS.map((group) => {
        const rows = c[group.key] ?? []
        const total = c.counts?.[group.key] ?? rows.length
        if (!total) return null
        const headingId = `${headingBase}-${group.key}`
        return (
          <section key={group.key} className={`vh-group ${group.tone}`} aria-labelledby={headingId}>
            <h4 id={headingId} className="vh-group-title">
              {group.title} ({total.toLocaleString()})
            </h4>
            <ul className="vh-rows">
              {rows.map((row, i) => (
                <ChangeLine key={`${row.label}-${row.where ?? ''}-${i}`} row={row} />
              ))}
            </ul>
            {total > rows.length ? (
              <p className="small muted">…and {(total - rows.length).toLocaleString()} more not listed.</p>
            ) : null}
          </section>
        )
      })}
    </div>
  )
}

/** Any two versions of one item, from the earlier to the later. */
export function CompareView({ kind, a, b }: { kind: VersionKind; a: string; b: string }) {
  const { data, loading, error, reload } = useResource(
    (signal) => historyApi.compare(kind, a, b, signal),
    [kind, a, b],
    `version-compare:${kind}:${a}:${b}`,
  )
  if (loading && !data) return <Loading label="Comparing the two versions…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null
  return (
    <div className="vh-compare" aria-live="polite">
      <p className="small muted">
        From <strong>{versionName(data.before)}</strong> (<VersionTime iso={data.before.created_at} />) to{' '}
        <strong>{versionName(data.after)}</strong> (<VersionTime iso={data.after.created_at} />).
      </p>
      <ComparisonBody comparison={data} />
    </div>
  )
}

function ComparePicker({ kind, entries }: { kind: VersionKind; entries: VersionEntry[] }) {
  const id = useId()
  const [a, setA] = useState('')
  const [b, setB] = useState('')
  const [shown, setShown] = useState<{ a: string; b: string } | null>(null)

  // Default to the newest version against the one before it.
  useEffect(() => {
    const ids = new Set(entries.map((e) => e.id))
    setA((current) => (current && ids.has(current) ? current : (entries[1]?.id ?? '')))
    setB((current) => (current && ids.has(current) ? current : (entries[0]?.id ?? '')))
  }, [entries])

  if (entries.length < 2) return null
  const subjectOf = (entryId: string) => entries.find((e) => e.id === entryId)?.subject_id ?? ''
  // Only versions of the same item can be compared.
  const others = entries.filter((e) => (e.subject_id ?? '') === subjectOf(a))
  const same = a !== '' && a === b
  const option = (e: VersionEntry) => `${versionName(e)} · ${when(e.created_at)} · ${e.label}`

  return (
    <section className="panel sa-pad vh-picker" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="panel-title">
        Compare two versions
      </h3>
      <form
        className="vh-picker-row"
        onSubmit={(event) => {
          event.preventDefault()
          if (a && b && a !== b) setShown({ a, b })
        }}
      >
        <label className="vh-picker-field" htmlFor={`${id}-a`}>
          <span className="small">Version A</span>
          <select
            id={`${id}-a`}
            className="input"
            value={a}
            onChange={(event) => {
              const next = event.target.value
              setA(next)
              setShown(null)
              if (subjectOf(b) !== subjectOf(next)) {
                setB(entries.find((e) => e.id !== next && (e.subject_id ?? '') === subjectOf(next))?.id ?? '')
              }
            }}
          >
            {entries.map((e) => (
              <option key={e.id} value={e.id}>
                {option(e)}
              </option>
            ))}
          </select>
        </label>
        <label className="vh-picker-field" htmlFor={`${id}-b`}>
          <span className="small">Version B</span>
          <select
            id={`${id}-b`}
            className="input"
            value={b}
            onChange={(event) => {
              setB(event.target.value)
              setShown(null)
            }}
          >
            {others.map((e) => (
              <option key={e.id} value={e.id}>
                {option(e)}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!a || !b || same}>
          Compare
        </button>
      </form>
      {same ? <p className="small muted">Choose two different versions.</p> : null}
      {shown ? <CompareView kind={kind} a={shown.a} b={shown.b} /> : null}
    </section>
  )
}

// ── One version ────────────────────────────────────────────────────────────
const HEX_COLOUR = /^#[0-9A-F]{6}$/i

function FieldList({ fields }: { fields: VersionDetail['fields'] }) {
  if (!fields?.length) return null
  return (
    <dl className="vh-fields">
      {fields.map((f) => (
        <div key={f.field} className="vh-field">
          <dt>{f.label}</dt>
          <dd>
            {HEX_COLOUR.test(f.value) ? (
              <span className="vh-swatch" style={{ backgroundColor: f.value }} aria-hidden="true" />
            ) : null}
            {f.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function VersionDetailDrawer({
  kind,
  entry,
  onClose,
  onRestored,
}: {
  kind: VersionKind
  entry: VersionEntry
  onClose: () => void
  onRestored: (result: RestoreResult) => void
}) {
  const { canWrite } = useAuth()
  const { data, loading, error, reload } = useResource(
    (signal) => historyApi.detail(kind, entry.id, signal),
    [kind, entry.id],
    `version-detail:${kind}:${entry.id}`,
  )
  const canRestore = Boolean(data && data.restorable && !data.is_current && canWrite(KIND_MODULE[kind]))
  return (
    <Drawer
      title={`${versionName(entry)} · ${when(entry.created_at)}`}
      subtitle={describeActor(entry)}
      width={760}
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Close
          </button>
          {data && canRestore ? <RestoreButton kind={kind} detail={data} onRestored={onRestored} /> : null}
        </>
      }
    >
      {loading && !data ? (
        <Loading label="Loading this version…" />
      ) : error && !data ? (
        <ErrorState message={error} onRetry={reload} />
      ) : data ? (
        <div className="vh-detail">
          <p className="small muted">
            {data.label}
            {data.website ? ` · ${data.website}` : ''}.
            {data.is_current ? ` This is the ${KIND_NAMES[kind]} in use now.` : ''}
          </p>
          {data.fields?.length ? (
            <section className="vh-detail-section" aria-labelledby="vh-detail-holds">
              <h3 id="vh-detail-holds" className="panel-title">
                What this version holds
              </h3>
              <FieldList fields={data.fields} />
            </section>
          ) : null}
          <section className="vh-detail-section" aria-labelledby="vh-detail-changed">
            <h3 id="vh-detail-changed" className="panel-title">
              What changed
            </h3>
            {data.changes ? (
              <>
                <p className="small muted">
                  Compared with {versionName(data.changes.previous)}, <VersionTime iso={data.changes.previous.created_at} />.
                </p>
                <ComparisonBody comparison={data.changes} />
              </>
            ) : data.changes === null ? (
              <p className="small muted">This is the earliest version kept, so there is nothing before it to compare with.</p>
            ) : (
              <p className="small muted">Use "Compare two versions" to see what changed between this one and another.</p>
            )}
          </section>
        </div>
      ) : null}
    </Drawer>
  )
}

// ── Restore ────────────────────────────────────────────────────────────────
const PUBLISH_AUTOMATICALLY = 'Publish automatically'

/** True when this workspace policy version would turn on "Publish automatically". */
function restoresAutoPublish(kind: VersionKind, detail: VersionDetail): boolean {
  return (
    kind === 'workspace_policy' &&
    Boolean(detail.fields?.some((f) => f.field === 'guardrail' && f.value === PUBLISH_AUTOMATICALLY))
  )
}

/**
 * Make an old version current again, after a plain-words confirmation. A
 * workspace policy that publishes automatically gets a second, explicit
 * confirmation before `confirm=true` is sent; a 422 asking for it (the
 * policy changed meanwhile) asks the same question and tries once more.
 */
export function RestoreButton({
  kind,
  detail,
  onRestored,
}: {
  kind: VersionKind
  detail: VersionDetail
  onRestored: (result: RestoreResult) => void
}) {
  const confirm = useConfirm()
  const { push, fromError } = useToasts()
  const [busy, setBusy] = useState(false)
  const name = KIND_NAMES[kind]
  const label = versionName(detail).toLowerCase()

  const confirmAutoPublish = () =>
    confirm({
      title: 'Let agents publish without asking?',
      message: `This version uses "${PUBLISH_AUTOMATICALLY}". Once it is restored, agents publish changes to your website and channels without waiting for anyone to approve them.`,
      detail: 'You can switch back to reviewing changes at any time in Settings.',
      confirmLabel: 'Yes, publish automatically',
      tone: 'danger',
    })

  const run = async () => {
    const ok = await confirm({
      title: `Restore ${label}?`,
      message: `The ${name} goes back to how it was in ${label}${detail.created_at ? `, from ${when(detail.created_at)}` : ''}.`,
      detail: `Nothing is lost: the ${name} in use now stays in the list as a version, so you can switch back later.`,
      confirmLabel: 'Restore this version',
    })
    if (!ok) return
    let confirmed = false
    if (restoresAutoPublish(kind, detail)) {
      if (!(await confirmAutoPublish())) return
      confirmed = true
    }
    setBusy(true)
    try {
      let result: RestoreResult
      try {
        result = await historyApi.restore(kind, detail.id, { confirm: confirmed })
      } catch (caught) {
        if (confirmed || !needsConfirmation(caught)) throw caught
        if (!(await confirmAutoPublish())) return
        result = await historyApi.restore(kind, detail.id, { confirm: true })
      }
      push(`${versionName(detail)} restored`, 'success')
      onRestored(result)
    } catch (caught) {
      fromError(caught, 'The version could not be restored. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void run()}>
      {busy ? 'Restoring…' : 'Restore this version'}
    </button>
  )
}

function RestoreNotes({ result, onDismiss }: { result: RestoreResult; onDismiss: () => void }) {
  const notes = result.notes ?? []
  if (!notes.length) return null
  return (
    <div className="banner is-warning vh-notes" role="status">
      <div>
        <strong>Version {result.restored_version} was restored, with a few things to know:</strong>
        <ul className="vh-notes-list">
          {notes.map((note, i) => (
            <li key={`${i}-${note}`}>{note}</li>
          ))}
        </ul>
      </div>
      <button type="button" className="btn btn-ghost btn-sm" onClick={onDismiss}>
        Dismiss
      </button>
    </div>
  )
}

// ── The whole kit for one kind ─────────────────────────────────────────────
/**
 * Version history for one kind (and, for kits, products, agents and sites,
 * one item): the numbered list, compare any two, details and restore.
 */
export function VersionHistory({
  kind,
  subject = null,
  title = 'Version history',
  description,
  refreshKey = '',
  onRestored,
}: {
  kind: VersionKind
  /** The item (kit, product, agent record or site); omit for kinds with one subject. */
  subject?: string | null
  title?: string
  description?: string
  refreshKey?: string
  onRestored?: (result: RestoreResult) => void
}) {
  const { canView } = useAuth()
  const [open, setOpen] = useState<VersionEntry | null>(null)
  const [restored, setRestored] = useState<RestoreResult | null>(null)
  const paged = usePagedVersions(kind, subject, refreshKey)
  const { data, loading, error, reload } = paged.first

  if (!canView(KIND_MODULE[kind])) return null
  if (loading && !data) return <Loading label="Loading versions…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const handleRestored = (result: RestoreResult) => {
    setOpen(null)
    setRestored(result)
    invalidateResourceCache('version-')
    void reload()
    onRestored?.(result)
  }

  return (
    <div className="vh">
      {restored ? <RestoreNotes result={restored} onDismiss={() => setRestored(null)} /> : null}
      <Section
        title={title}
        subtitle={
          description ??
          `A numbered version of the ${KIND_NAMES[kind]} is kept each time it changes. Open one to see what it holds and what changed${data.restorable ? ', or to restore it' : ''}.`
        }
      >
        {paged.entries.length ? (
          <VersionList
            entries={paged.entries}
            hasMore={paged.hasMore}
            loadingMore={paged.loadingMore}
            moreError={paged.moreError}
            onLoadMore={() => void paged.loadMore()}
            onOpen={setOpen}
          />
        ) : (
          <EmptyState title="No versions yet" description={`A version is kept the next time the ${KIND_NAMES[kind]} changes.`} />
        )}
        {data.started_at ? (
          <p className="small muted hist-note">
            The earliest version kept is from {dateOnly(data.started_at)}. Older versions may have been removed by the
            workspace's history settings.
          </p>
        ) : null}
      </Section>
      <ComparePicker kind={kind} entries={paged.entries} />
      {open ? <VersionDetailDrawer kind={kind} entry={open} onClose={() => setOpen(null)} onRestored={handleRestored} /> : null}
    </div>
  )
}

// ── Retention settings (admins) ────────────────────────────────────────────
type RetentionDraft = Record<string, { versions: string; days: string }>

const EMPTY_DRAFT = { versions: '', days: '' }

function draftOf(view: RetentionView): RetentionDraft {
  return Object.fromEntries(
    view.kinds.map((k) => [
      k.kind,
      k.custom ? { versions: String(k.keep_versions), days: String(k.keep_days) } : { ...EMPTY_DRAFT },
    ]),
  )
}

function parseCount(text: string): number | null {
  const trimmed = text.trim()
  return trimmed === '' ? null : Number(trimmed)
}

function keepSentence(versions: number, days: number): string {
  const count = `the last ${versions.toLocaleString()} ${versions === 1 ? 'version' : 'versions'}`
  return days > 0 ? `${count}, each for up to ${days.toLocaleString()} ${days === 1 ? 'day' : 'days'}` : `${count}, with no time limit`
}

/**
 * How long old versions are kept, per kind, in plain words. Admins only:
 * people without admin access see nothing, and view-only admins see the
 * settings without the means to change them. The server checks both.
 */
export function RetentionSettings() {
  const { canView } = useAuth()
  if (!canView('admin')) return null
  return <RetentionPanel />
}

function RetentionPanel() {
  const { canWrite } = useAuth()
  const writable = canWrite('admin')
  const { push, fromError } = useToasts()
  const baseId = useId()
  const { data, loading, error, reload, set } = useResource((signal) => historyApi.retention(signal), [], 'version-retention')
  const [draft, setDraft] = useState<RetentionDraft>({})
  const [dirty, setDirty] = useState<Set<string>>(new Set())
  const [problems, setProblems] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)

  const reset = (view: RetentionView) => {
    setDraft(draftOf(view))
    setDirty(new Set())
    setProblems({})
  }

  useEffect(() => {
    if (data) reset(data)
  }, [data])

  if (loading && !data) return <Loading label="Loading history settings…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const edit = (kind: string, value: { versions?: string; days?: string }) => {
    setDraft((prev) => ({ ...prev, [kind]: { ...EMPTY_DRAFT, ...prev[kind], ...value } }))
    setDirty((prev) => new Set(prev).add(kind))
    setProblems((prev) => {
      const next = { ...prev }
      delete next[kind]
      return next
    })
  }

  const save = async () => {
    const changes: Partial<Record<VersionKind, RetentionChange>> = {}
    const found: Record<string, string> = {}
    for (const kind of dirty) {
      const row = draft[kind] ?? EMPTY_DRAFT
      const change: RetentionChange = { keep_versions: parseCount(row.versions), keep_days: parseCount(row.days) }
      const problem = retentionProblem(change, data)
      if (problem) found[kind] = problem
      else changes[kind as VersionKind] = change
    }
    setProblems(found)
    if (Object.keys(found).length || !Object.keys(changes).length) return
    setSaving(true)
    try {
      const next = await historyApi.setRetention(changes)
      set(next)
      push('History settings saved. They apply the next time a version is kept.', 'success')
    } catch (caught) {
      fromError(caught, 'The history settings could not be saved. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  /** What a kind will keep with the boxes as typed (empty boxes use the default). */
  const effective = (k: RetentionKind) => {
    const row = draft[k.kind] ?? EMPTY_DRAFT
    const versions = parseCount(row.versions)
    const days = parseCount(row.days)
    return {
      versions:
        versions !== null && Number.isFinite(versions) ? Math.max(versions, data.min_versions_kept) : data.defaults.keep_versions,
      days: days !== null && Number.isFinite(days) && days >= 0 ? days : data.defaults.keep_days,
    }
  }

  return (
    <Section
      title="How long versions are kept"
      subtitle={`Old versions are removed automatically once they go past either limit. The newest ${data.min_versions_kept} versions of each item are always kept.`}
    >
      <form
        className="vh-retention"
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <p className="small muted">
          Unless you choose otherwise, the workspace keeps {keepSentence(data.defaults.keep_versions, data.defaults.keep_days)}.
          Leave a box empty to use that. Enter 0 days to keep versions with no time limit.
        </p>
        <ul className="vh-retention-list">
          {data.kinds.map((k) => {
            const row = draft[k.kind] ?? EMPTY_DRAFT
            const shown = effective(k)
            const versionsId = `${baseId}-${k.kind}-versions`
            const daysId = `${baseId}-${k.kind}-days`
            const errorId = `${baseId}-${k.kind}-error`
            const problem = problems[k.kind]
            const usesDefault = row.versions === '' && row.days === ''
            return (
              <li key={k.kind} className="vh-retention-item">
                <fieldset className="vh-retention-set" aria-describedby={problem ? errorId : undefined}>
                  <legend className="vh-retention-name">{k.name}</legend>
                  <div className="vh-retention-inputs">
                    <label htmlFor={versionsId} className="vh-retention-field">
                      <span className="small">Keep the last</span>
                      <input
                        id={versionsId}
                        className="input input-compact vh-number"
                        type="number"
                        inputMode="numeric"
                        min={data.min_versions_kept}
                        max={data.limits.keep_versions}
                        step={1}
                        placeholder={String(data.defaults.keep_versions)}
                        value={row.versions}
                        disabled={!writable || saving}
                        aria-invalid={problem ? true : undefined}
                        onChange={(event) => edit(k.kind, { versions: event.target.value })}
                      />
                      <span className="small">versions</span>
                    </label>
                    <label htmlFor={daysId} className="vh-retention-field">
                      <span className="small">Keep versions for</span>
                      <input
                        id={daysId}
                        className="input input-compact vh-number"
                        type="number"
                        inputMode="numeric"
                        min={0}
                        max={data.limits.keep_days}
                        step={1}
                        placeholder={String(data.defaults.keep_days)}
                        value={row.days}
                        disabled={!writable || saving}
                        aria-invalid={problem ? true : undefined}
                        onChange={(event) => edit(k.kind, { days: event.target.value })}
                      />
                      <span className="small">days</span>
                    </label>
                    {writable && !usesDefault ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        disabled={saving}
                        onClick={() => edit(k.kind, { ...EMPTY_DRAFT })}
                      >
                        Use the default
                      </button>
                    ) : null}
                  </div>
                  <p className="small muted">
                    Keeps {keepSentence(shown.versions, shown.days)}
                    {usesDefault ? ' (the workspace default).' : '.'}
                  </p>
                  {problem ? (
                    <p id={errorId} className="field-error" role="alert">
                      {problem}
                    </p>
                  ) : null}
                </fieldset>
              </li>
            )
          })}
        </ul>
        {writable ? (
          <div className="vh-retention-actions">
            <button type="submit" className="btn btn-primary" disabled={saving || !dirty.size}>
              {saving ? 'Saving…' : 'Save history settings'}
            </button>
            {dirty.size ? (
              <button type="button" className="btn btn-secondary" disabled={saving} onClick={() => reset(data)}>
                Undo changes
              </button>
            ) : null}
          </div>
        ) : (
          <p className="small muted">Only workspace admins with full access can change these settings.</p>
        )}
      </form>
    </Section>
  )
}
