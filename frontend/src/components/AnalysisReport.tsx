/**
 * The website analysis report, shared by the workspace's Site analysis page
 * and Portal Admin. (The public free analysis is rendered by the separate
 * marketing website, which calls /public/analysis directly.) Everything under
 * ``report.ai`` is model output, so every field is read defensively.
 */
import { useState, type ReactNode } from 'react'

export type ReportFormat = 'pdf' | 'html'

/** Save a downloaded file in the browser. */
export function saveFile({ blob, filename }: { blob: Blob; filename: string }) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 2000)
}

/** "Download PDF" and "HTML" buttons for a finished report. */
export function DownloadButtons({
  onDownload,
  compact = false,
}: {
  onDownload: (format: ReportFormat) => Promise<void>
  compact?: boolean
}) {
  const [busy, setBusy] = useState<ReportFormat | null>(null)
  const run = async (format: ReportFormat) => {
    setBusy(format)
    try {
      await onDownload(format)
    } finally {
      setBusy(null)
    }
  }
  return (
    <span className="report-downloads">
      <button type="button" className={`btn ${compact ? 'btn-ghost' : 'btn-secondary'} btn-sm`} disabled={busy !== null} title="Download a print-ready PDF report" onClick={() => void run('pdf')}>
        {busy === 'pdf' ? 'Preparing…' : compact ? 'PDF' : 'Download PDF report'}
      </button>
      <button type="button" className="btn btn-ghost btn-sm" disabled={busy !== null} title="Download an interactive report that opens in any browser" onClick={() => void run('html')}>
        {busy === 'html' ? 'Preparing…' : compact ? 'HTML' : 'Interactive report (HTML)'}
      </button>
    </span>
  )
}

export type Step = { key: string; label: string; state: 'pending' | 'running' | 'done' | 'skipped' | 'failed'; detail: string }
export type Check = {
  id: string
  category: string
  title: string
  status: 'pass' | 'warn' | 'fail' | 'info'
  evidence: string
  why: string
  fix: string
  priority?: string
  effort?: string
  agent?: string
  pages?: string[]
}
export type Category = { label: string; about: string; score: number | null; passed: number; total: number }
// The AI sections are model output: every field is treated as optional.
export type Any = Record<string, unknown>
export type Report = {
  website: string
  generated_at: string
  duration_s: number
  overall: { score: number | null; grade: string }
  categories: Record<string, Category>
  summary: { checks: number; passed: number; warnings: number; failed: number; pages_read: number; links_checked: number }
  checks: Check[]
  measured: Any & { pagespeed?: Any; pages?: Any[]; trackers?: string[]; platform?: string }
  ai: { model: string; strategy: Any; creative: Any }
  automation: { agent: string; name: string; what_it_does: string; addresses: string[] }[]
  notes: string[]
}

// ── Helpers ────────────────────────────────────────────────────────────────
const arr = <T = Any,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : [])
const str = (v: unknown): string => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '')
const obj = (v: unknown): Any => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Any) : {})
export const host = (url: string) => url.replace(/^https?:\/\//, '').replace(/\/$/, '')

export function tone(score: number | null): 'good' | 'ok' | 'poor' | 'none' {
  if (score === null) return 'none'
  return score >= 80 ? 'good' : score >= 55 ? 'ok' : 'poor'
}

const STATUS_LABEL: Record<Check['status'], string> = { fail: 'Fix', warn: 'Improve', info: 'Note', pass: 'Good' }

export function AnalysisProgress({ analysis }: { analysis: { website: string; percent: number; progress: Step[] } }) {
  return (
    <div className="fa-progress" aria-live="polite">
      <div className="fa-progress-head">
        <strong>Analysing {host(analysis.website)}</strong>
        <span>{analysis.percent}%</span>
      </div>
      <div className="fa-bar">
        <div style={{ width: `${Math.max(4, analysis.percent)}%` }} />
      </div>
      <ol className="fa-steps">
        {analysis.progress.map((step) => (
          <li key={step.key} className={`is-${step.state}`}>
            <span className="fa-step-dot" aria-hidden="true" />
            <span className="fa-step-label">{step.label}</span>
            {step.detail ? <span className="fa-step-detail">{step.detail}</span> : null}
          </li>
        ))}
      </ol>
    </div>
  )
}


// ── Report ─────────────────────────────────────────────────────────────────
export function ScoreRing({ score, grade, size = 132 }: { score: number | null; grade?: string; size?: number }) {
  const r = 52
  const c = 2 * Math.PI * r
  const value = score ?? 0
  return (
    <svg className={`fa-ring is-${tone(score)}`} width={size} height={size} viewBox="0 0 120 120" role="img" aria-label={`Score ${score ?? 'not measured'}`}>
      <circle cx="60" cy="60" r={r} className="fa-ring-track" />
      <circle cx="60" cy="60" r={r} className="fa-ring-fill" strokeDasharray={`${(c * value) / 100} ${c}`} transform="rotate(-90 60 60)" />
      <text x="60" y={grade ? 58 : 66} textAnchor="middle" className="fa-ring-num">
        {score ?? '–'}
      </text>
      {grade ? (
        <text x="60" y="80" textAnchor="middle" className="fa-ring-sub">
          Grade {grade}
        </text>
      ) : null}
    </svg>
  )
}

function Section({ id, title, kicker, children }: { id: string; title: string; kicker?: string; children: ReactNode }) {
  return (
    <section className="fa-section" id={id}>
      <div className="fa-section-head">
        {kicker ? <span className="fa-kicker">{kicker}</span> : null}
        <h2>{title}</h2>
      </div>
      {children}
    </section>
  )
}

function Chip({ children, kind = '' }: { children: ReactNode; kind?: string }) {
  return <span className={`fa-chip ${kind}`}>{children}</span>
}

/** The full report. ``cta`` replaces the sign-up call to action (the portal
 *  passes none); ``inPortal`` words the automation section for a workspace. */
export function ReportView({
  report,
  cta,
  inPortal = false,
}: {
  report: Report
  cta?: ReactNode
  inPortal?: boolean
}) {
  const strategy = report.ai.strategy ?? {}
  const creative = report.ai.creative ?? {}
  const biz = obj(strategy.business)
  const messaging = obj(strategy.messaging)
  const roadmap = obj(strategy.roadmap)
  const campaign = obj(creative.search_campaign)
  const speed = obj(report.measured.pagespeed)
  const [filter, setFilter] = useState<string>('all')

  const checks = report.checks.filter((c) => filter === 'all' || c.category === filter)
  const nav = [
    ['scores', 'Scores'],
    ['priorities', 'Priorities'],
    ['checks', 'All checks'],
    ['keywords', 'Keywords'],
    ['content', 'Content plan'],
    ['ads', 'Search ads'],
    ['social', 'Social & email'],
    ['roadmap', '90-day plan'],
    ['automate', 'Automate it'],
  ]

  return (
    <div className="fa-report">
      <div className="fa-summary">
        <ScoreRing score={report.overall.score} grade={report.overall.grade} />
        <div className="fa-summary-text">
          <span className="fa-kicker">Growth analysis · {new Date(report.generated_at).toLocaleDateString()}</span>
          <h1>{str(biz.name) || host(report.website)}</h1>
          <p className="fa-site">
            <a href={report.website} target="_blank" rel="noreferrer noopener">
              {host(report.website)}
            </a>
            {str(biz.industry) ? ` · ${str(biz.industry)}` : ''}
            {report.measured.platform ? ` · built on ${report.measured.platform}` : ''}
          </p>
          <div className="fa-counts">
            <span className="is-fail">{report.summary.failed} to fix</span>
            <span className="is-warn">{report.summary.warnings} to improve</span>
            <span className="is-pass">{report.summary.passed} good</span>
            <span>
              {report.summary.pages_read} pages read · {report.summary.links_checked} links checked
            </span>
          </div>
        </div>
      </div>

      <nav className="fa-nav" aria-label="Report sections">
        {nav.map(([id, label]) => (
          <a key={id} href={`#${id}`}>
            {label}
          </a>
        ))}
      </nav>

      {str(strategy.executive_summary) ? (
        <div className="fa-exec">
          <span className="fa-kicker">Executive summary</span>
          <p>{str(strategy.executive_summary)}</p>
          {arr<string>(strategy.strengths).length ? (
            <ul className="fa-strengths">
              {arr<string>(strategy.strengths).map((s, i) => (
                <li key={i}>{str(s)}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <Section id="scores" title="Where you stand" kicker="Measured today">
        <div className="fa-cats">
          {Object.entries(report.categories).map(([key, cat]) => (
            <button
              type="button"
              key={key}
              className={`fa-cat is-${tone(cat.score)}`}
              onClick={() => {
                setFilter(key)
                document.getElementById('checks')?.scrollIntoView({ behavior: 'smooth' })
              }}
            >
              <ScoreRing score={cat.score} size={64} />
              <span className="fa-cat-text">
                <strong>{cat.label}</strong>
                <span>{cat.score === null ? 'Not measured' : `${cat.passed} of ${cat.total} checks passed`}</span>
              </span>
            </button>
          ))}
        </div>
        {speed.measured ? (
          <div className="fa-speed">
            {Object.entries(obj(speed.scores)).map(([k, v]) => (
              <div key={k}>
                <strong className={`is-${tone(typeof v === 'number' ? v : null)}`}>{str(v) || '–'}</strong>
                <span>{k.replace('_', ' ')}</span>
              </div>
            ))}
            <p className="fa-fine">Google Lighthouse, mobile, home page.</p>
          </div>
        ) : null}
      </Section>

      {arr(strategy.top_priorities).length ? (
        <Section id="priorities" title="Your top priorities" kicker="AI strategy, from the measured issues">
          <ol className="fa-priorities">
            {arr(strategy.top_priorities).map((p, i) => (
              <li key={i}>
                <div className="fa-priority-head">
                  <strong>{str(p.title)}</strong>
                  <span>
                    {str(p.impact) ? <Chip kind={`is-${str(p.impact)}`}>{str(p.impact)} impact</Chip> : null}
                    {str(p.effort) ? <Chip>{str(p.effort)}</Chip> : null}
                    {str(p.timeframe) ? <Chip>{str(p.timeframe)}</Chip> : null}
                  </span>
                </div>
                <p>{str(p.why)}</p>
                {str(p.how) ? <p className="fa-how">{str(p.how)}</p> : null}
              </li>
            ))}
          </ol>
        </Section>
      ) : null}

      <Section id="checks" title="Every check" kicker={`${report.summary.checks} measured checks`}>
        <div className="fa-filter" role="tablist">
          <button type="button" className={filter === 'all' ? 'is-active' : ''} onClick={() => setFilter('all')}>
            All
          </button>
          {Object.entries(report.categories).map(([key, cat]) => (
            <button type="button" key={key} className={filter === key ? 'is-active' : ''} onClick={() => setFilter(key)}>
              {cat.label}
            </button>
          ))}
        </div>
        <ul className="fa-checks">
          {checks.map((c) => (
            <li key={c.id} className={`is-${c.status}`}>
              <details open={c.status === 'fail'}>
                <summary>
                  <span className={`fa-status is-${c.status}`}>{STATUS_LABEL[c.status]}</span>
                  <span className="fa-check-title">{c.title}</span>
                  <span className="fa-check-evidence">{c.evidence}</span>
                </summary>
                <div className="fa-check-body">
                  <p>
                    <strong>Why it matters.</strong> {c.why}
                  </p>
                  {c.status !== 'pass' ? (
                    <p>
                      <strong>How to fix.</strong> {c.fix}
                    </p>
                  ) : null}
                  {c.pages?.length ? (
                    <ul className="fa-pages">
                      {c.pages.slice(0, 8).map((u) => (
                        <li key={u}>{host(u)}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              </details>
            </li>
          ))}
        </ul>
      </Section>

      {str(messaging.suggested_headline) ? (
        <Section id="messaging" title="Your home page message" kicker="AI review">
          <div className="fa-two">
            <div className="fa-card">
              <span className="fa-kicker">Today</span>
              <p className="fa-quote">{str(messaging.current_headline) || '—'}</p>
              <p className="fa-muted">{str(messaging.clarity_verdict)}</p>
            </div>
            <div className="fa-card is-accent">
              <span className="fa-kicker">Suggested</span>
              <p className="fa-quote">{str(messaging.suggested_headline)}</p>
              <p>{str(messaging.suggested_subheadline)}</p>
              {str(messaging.suggested_cta) ? <span className="btn btn-primary btn-sm fa-fake-btn">{str(messaging.suggested_cta)}</span> : null}
            </div>
          </div>
          {arr<string>(messaging.trust_gaps).length ? (
            <p className="fa-muted">
              <strong>Missing proof:</strong> {arr<string>(messaging.trust_gaps).map(str).join(' · ')}
            </p>
          ) : null}
        </Section>
      ) : null}

      {arr(strategy.keyword_opportunities).length ? (
        <Section id="keywords" title="Searches you should win" kicker="AI keyword opportunities">
          <div className="fa-table-wrap">
            <table className="fa-table">
              <thead>
                <tr>
                  <th>Search</th>
                  <th>Intent</th>
                  <th>Why it fits</th>
                  <th>Page</th>
                </tr>
              </thead>
              <tbody>
                {arr(strategy.keyword_opportunities).map((k, i) => (
                  <tr key={i}>
                    <td>
                      <strong>{str(k.keyword)}</strong>
                    </td>
                    <td>
                      <Chip>{str(k.intent)}</Chip>
                    </td>
                    <td>{str(k.why)}</td>
                    <td className="fa-muted">{host(str(k.target_page))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="fa-fine">Search volumes need Search Console or a keyword tool connected — not estimated here.</p>
        </Section>
      ) : null}

      {arr(strategy.page_fixes).length ? (
        <Section id="pagefixes" title="Ready-to-paste titles and descriptions" kicker="AI rewrites">
          <div className="fa-serps">
            {arr(strategy.page_fixes).map((f, i) => (
              <div key={i} className="fa-serp">
                <span className="fa-serp-url">{host(str(f.url))}</span>
                <span className="fa-serp-title">{str(f.suggested_title)}</span>
                <span className="fa-serp-desc">{str(f.suggested_description)}</span>
                {str(f.note) ? <span className="fa-fine">{str(f.note)}</span> : null}
              </div>
            ))}
          </div>
        </Section>
      ) : null}

      {arr(creative.content_plan).length ? (
        <Section id="content" title="Your content plan" kicker="8 articles to publish">
          <div className="fa-grid">
            {arr(creative.content_plan).map((a, i) => (
              <div key={i} className="fa-card">
                <Chip>{str(a.type)}</Chip>
                <h3>{str(a.title)}</h3>
                <p className="fa-muted">{str(a.angle)}</p>
                {arr<string>(a.outline).length ? (
                  <ul className="fa-outline">
                    {arr<string>(a.outline).map((h, j) => (
                      <li key={j}>{str(h)}</li>
                    ))}
                  </ul>
                ) : null}
                <span className="fa-fine">Target: {str(a.target_keyword)}</span>
              </div>
            ))}
          </div>
        </Section>
      ) : null}

      {arr(campaign.ad_groups).length ? (
        <Section id="ads" title={str(campaign.campaign_name) || 'Google search campaign'} kicker="Ready-to-launch search ads">
          {str(campaign.budget_note) ? <p className="fa-muted">{str(campaign.budget_note)}</p> : null}
          <div className="fa-grid">
            {arr(campaign.ad_groups).map((g, i) => (
              <div key={i} className="fa-card">
                <span className="fa-kicker">{str(g.name)}</span>
                <div className="fa-ad">
                  <span className="fa-ad-label">Sponsored · {host(str(g.landing_page) || report.website)}</span>
                  <span className="fa-ad-title">{arr<string>(g.headlines).slice(0, 3).map(str).join(' | ')}</span>
                  <span className="fa-ad-desc">{arr<string>(g.descriptions).map(str)[0]}</span>
                </div>
                <p className="fa-fine">Keywords: {arr<string>(g.keywords).map(str).join(', ')}</p>
                <details>
                  <summary className="fa-fine">All headlines and descriptions</summary>
                  <ul className="fa-outline">
                    {[...arr<string>(g.headlines), ...arr<string>(g.descriptions)].map((h, j) => (
                      <li key={j}>
                        {str(h)} <span className="fa-fine">({str(h).length})</span>
                      </li>
                    ))}
                  </ul>
                </details>
              </div>
            ))}
          </div>
          {arr<string>(campaign.negative_keywords).length ? (
            <p className="fa-fine">Negative keywords: {arr<string>(campaign.negative_keywords).map(str).join(', ')}</p>
          ) : null}
        </Section>
      ) : null}

      {arr(creative.social_posts).length || arr(creative.email_campaigns).length ? (
        <Section id="social" title="Posts and emails, written for you" kicker="Ready to use">
          <div className="fa-grid">
            {arr(creative.social_posts).map((p, i) => (
              <div key={i} className="fa-card fa-post">
                <span className="fa-kicker">{str(p.network)}</span>
                <p className="fa-post-text">{str(p.text)}</p>
                {arr<string>(p.hashtags).length ? (
                  <p className="fa-tags">{arr<string>(p.hashtags).map((t) => `#${str(t).replace(/^#/, '')}`).join(' ')}</p>
                ) : null}
                {str(p.visual_idea) ? <p className="fa-fine">Image: {str(p.visual_idea)}</p> : null}
              </div>
            ))}
            {arr(creative.email_campaigns).map((e, i) => (
              <div key={`e${i}`} className="fa-card">
                <span className="fa-kicker">Email · {str(e.name)}</span>
                <h3>{str(e.subject)}</h3>
                <p className="fa-muted">{str(e.preview_text)}</p>
                <ul className="fa-outline">
                  {arr<string>(e.body_outline).map((b, j) => (
                    <li key={j}>{str(b)}</li>
                  ))}
                </ul>
                {str(e.cta) ? <span className="btn btn-secondary btn-sm fa-fake-btn">{str(e.cta)}</span> : null}
              </div>
            ))}
          </div>
        </Section>
      ) : null}

      {arr(strategy.faq_suggestions).length || arr(strategy.competitors_to_research).length || arr<string>(strategy.local_actions).length ? (
        <Section id="more" title="Win AI answers and local search" kicker="AI suggestions">
          <div className="fa-two">
            {arr(strategy.faq_suggestions).length ? (
              <div className="fa-card">
                <h3>Questions to answer on your site</h3>
                <dl className="fa-faq">
                  {arr(strategy.faq_suggestions).map((f, i) => (
                    <div key={i}>
                      <dt>{str(f.q)}</dt>
                      <dd>{str(f.a)}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            ) : null}
            <div>
              {arr(strategy.competitors_to_research).length ? (
                <div className="fa-card">
                  <h3>Competitors to research</h3>
                  <ul className="fa-outline">
                    {arr(strategy.competitors_to_research).map((c, i) => (
                      <li key={i}>
                        <strong>{str(c.name)}</strong> — {str(c.why)}
                      </li>
                    ))}
                  </ul>
                  <p className="fa-fine">Suggested from your site's content, not measured.</p>
                </div>
              ) : null}
              {arr<string>(strategy.local_actions).length ? (
                <div className="fa-card">
                  <h3>Local search actions</h3>
                  <ul className="fa-outline">
                    {arr<string>(strategy.local_actions).map((a, i) => (
                      <li key={i}>{str(a)}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          </div>
        </Section>
      ) : null}

      {Object.keys(roadmap).length ? (
        <Section id="roadmap" title="Your 90-day plan" kicker="AI roadmap">
          <div className="fa-roadmap">
            {([
              ['first_30_days', 'Days 1–30'],
              ['days_31_60', 'Days 31–60'],
              ['days_61_90', 'Days 61–90'],
            ] as const).map(([key, label]) => (
              <div key={key} className="fa-card">
                <span className="fa-kicker">{label}</span>
                <ul className="fa-outline">
                  {arr<string>(roadmap[key]).map((a, i) => (
                    <li key={i}>{str(a)}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Section>
      ) : null}

      <Section
        id="automate"
        title={inPortal ? 'Agents that work on these findings' : 'Let Synaprank.ai do this for you'}
        kicker="Automation"
      >
        <p className="fa-muted">
          {inPortal
            ? `This report is a snapshot of ${report.summary.pages_read} pages. These agents work on the kinds of problems found, across the sites they are set to use.`
            : `This report is a snapshot of ${report.summary.pages_read} pages. Connected, these agents work on what was found — across your whole site, every week, with your approval.`}
        </p>
        <div className="fa-grid">
          {report.automation.map((a) => (
            <div key={a.agent} className="fa-card">
              <h3>{a.name}</h3>
              <p className="fa-muted">{a.what_it_does}</p>
              <p className="fa-fine">Works on: {a.addresses.join(' · ')}</p>
            </div>
          ))}
        </div>
        {cta ? <div className="fa-cta">{cta}</div> : null}
      </Section>

      <footer className="fa-notes">
        {report.notes.map((n, i) => (
          <p key={i}>{n}</p>
        ))}
        <p>Analysis took {Math.round(report.duration_s)} s to measure. AI sections written by {report.ai.model}.</p>
      </footer>
    </div>
  )
}
