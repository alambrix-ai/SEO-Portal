/**
 * Reports - the numbers, grouped, the trend behind them, the detailed results
 * per section, and (for roles that may see it) what the AI cost.
 *
 * Every rule here is the same one: a figure nobody measured is never shown as
 * zero. A workspace with nothing connected gets the server's plain hint and a
 * way to connect, not a page of zeros. A trend bucket with no measured day is
 * a gap in the line, not a dip. Backlinks nobody tracks say "Not tracked yet".
 *
 * The summary tiles come from `/reports` (7, 30 or 90 days only); the detailed
 * sections come from `/insights/overview` for the same dates, so the two
 * halves of the page always cover one period. The AI usage tab has its own
 * date range, because cost questions are usually "this month" or "last month".
 *
 * AI usage needs Agents or Admin view and is never shown to a Client Viewer.
 * The tab is only offered to roles that could see it, and a 403 from the
 * server hides it quietly: the server decides, the screen follows.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import {
  compactNumber,
  formatMetric,
  formatUsageCost,
  formatValue,
  insightsApi,
  isJobLines,
  USAGE_MAX_PAGE_SIZE,
  type InsightMetric,
  type InsightSectionBase,
  type InsightSeries,
  type InsightsOverview,
  type LlmUsageSummary,
  type SocialTopPost,
  type UsageGroup,
  type UsageLine,
} from '@/api/insights'
import type { ReportsOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { BarList } from '@/components/charts/BarList'
import { MetricDelta } from '@/components/charts/DeltaBadge'
import { LineChart } from '@/components/charts/LineChart'
import { DateRangePicker, describeRange, isoDay, rangeForPreset, type DateRange } from '@/components/DateRangePicker'
import { PageTabs } from '@/components/PageTabs'
import { EmptyState, ErrorState, Loading, SectionHeading, Segmented, StatTile } from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { moneyExact } from '@/lib/money'

type Range = '7d' | '30d' | '90d'
type Tab = 'results' | 'ai'

const RANGE_LABEL: Record<Range, string> = {
  '7d': 'the last 7 days',
  '30d': 'the last 30 days',
  '90d': 'the last 90 days',
}
const RANGE_DAYS: Record<Range, number> = { '7d': 7, '30d': 30, '90d': 90 }

const NOT_TRACKED = 'Not tracked yet'

/** Plain labels for a Creative Studio job's status; anything unknown reads as "Unknown". */
const JOB_STATUS_LABELS: ReadonlyMap<string, string> = new Map([
  ['queued', 'Waiting to start'],
  ['running', 'In progress'],
  ['succeeded', 'Finished'],
  ['failed', 'Could not finish'],
  ['cancelled', 'Cancelled'],
])
const NO_DATA_HINT = 'Nothing has been recorded for this period yet.'

// ── /reports, with the fields step 5.5 added ───────────────────────────────
/**
 * `/reports` as the server sends it today. The added fields are optional
 * here so an older server still renders (as "data present", the old
 * behaviour) rather than breaking the page.
 */
type AddedField = 'available' | 'connect_hint' | 'trend_with_gaps' | 'trend_days'

type NoteField = 'backlinks_won_note' | 'blended_cac_note' | 'ad_spend_note'

type ReportsPayload = Omit<ReportsOut, AddedField | NoteField> &
  Partial<Pick<ReportsOut, AddedField>> & {
    /** Empty or missing when there is nothing to explain. */
    backlinks_won_note?: string | null
    blended_cac_note?: string | null
    ad_spend_note?: string | null
  }

type Report = Omit<ReportsPayload, AddedField> & Pick<ReportsOut, AddedField>

function toReport(raw: ReportsPayload, range: Range): Report {
  const days = raw.trend_days
  return {
    ...raw,
    available: raw.available ?? true,
    connect_hint: raw.connect_hint ?? '',
    trend_with_gaps: raw.trend_with_gaps ?? raw.trend,
    trend_days: typeof days === 'number' && Number.isInteger(days) && days > 0 ? days : RANGE_DAYS[range],
    backlinks_won_note: raw.backlinks_won_note ?? '',
    blended_cac_note: raw.blended_cac_note ?? '',
    ad_spend_note: raw.ad_spend_note ?? '',
  }
}

/**
 * The trend buckets as a chart series. The server averages `trend_days`
 * calendar days into a fixed number of buckets (oldest first, the last one
 * ends today); each point is dated with the first day of its bucket.
 */
function trendSeries(report: Report, today: Date = new Date()): InsightSeries {
  const buckets = report.trend_with_gaps
  const days = report.trend_days
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - (days - 1))
  const dates = buckets.map((_, index) => {
    const offset = Math.floor((index * days) / buckets.length)
    return isoDay(new Date(start.getFullYear(), start.getMonth(), start.getDate() + offset))
  })
  // A range shorter than the bucket count would repeat a date; number the
  // points instead so none is merged away.
  const distinct = new Set(dates).size === dates.length
  return {
    key: 'organic_sessions',
    label: 'Organic sessions',
    unit: 'count',
    points: buckets.map((value, index) => ({
      date: distinct ? (dates[index] ?? String(index + 1)) : String(index + 1),
      value: value === null || !Number.isFinite(value) ? null : value,
    })),
  }
}

export function ReportsPage() {
  const { canView } = useAuth()
  // Agents or Admin view is what the server asks for; a Client Viewer has
  // neither, so for them the usage endpoint is never even called.
  const [usageAllowed, setUsageAllowed] = useState(() => canView('agents') || canView('admin'))
  const [tab, setTab] = useState<Tab>('results')

  const hideUsage = () => {
    setUsageAllowed(false)
    setTab('results')
  }

  const tabs: { value: Tab; label: string }[] = [{ value: 'results', label: 'Results' }]
  if (usageAllowed) tabs.push({ value: 'ai', label: 'AI usage' })

  return (
    <>
      {tabs.length > 1 ? <PageTabs<Tab> value={tab} options={tabs} onChange={setTab} /> : null}
      {tab === 'ai' && usageAllowed ? <AiUsagePanel onForbidden={hideUsage} /> : <ResultsPanel />}
    </>
  )
}

// ── Results ────────────────────────────────────────────────────────────────
function ResultsPanel() {
  const [range, setRange] = useState<Range>('30d')
  const reports = useResource(
    async (signal) => toReport(await api.reports(range, signal), range),
    [range],
    `reports:${range}`,
  )
  const dates = useMemo(() => rangeForPreset(range), [range])
  const overview = useResource(
    (signal) => insightsApi.overview(dates.from, dates.to, signal),
    [dates.from, dates.to],
    `insights-overview:${dates.from}:${dates.to}`,
  )

  const report = reports.data

  return (
    <>
      <div className="reports-head">
        <div>
          <h2 className="reports-title">Performance</h2>
          <p className="reports-period">Everything on this page covers {RANGE_LABEL[range]}.</p>
        </div>
        <Segmented<Range>
          name="range"
          value={range}
          options={[
            { value: '7d', label: '7 days' },
            { value: '30d', label: '30 days' },
            { value: '90d', label: '90 days' },
          ]}
          onChange={setRange}
        />
      </div>

      {reports.loading && !report ? <Loading label="Loading reports…" /> : null}
      {reports.error && !report ? <ErrorState message={reports.error} onRetry={reports.reload} /> : null}

      {report ? (report.available ? <SummaryTiles report={report} /> : <ConnectPrompt hint={report.connect_hint} />) : null}

      {overview.loading && !overview.data ? <Loading label="Loading detailed results…" /> : null}
      {overview.error && !overview.data ? (
        <ErrorState message={overview.error} onRetry={overview.reload} />
      ) : null}
      {overview.data ? <DetailedResults overview={overview.data} /> : null}
    </>
  )
}

/** "No data yet" with the server's hint, and a way to connect for roles that can. */
function ConnectPrompt({ hint }: { hint: string }) {
  const { canView } = useAuth()
  const canConnect = canView('connectors')
  return (
    <EmptyState
      title="No results to show yet"
      description={
        (hint || NO_DATA_HINT) +
        (canConnect ? '' : ' Ask an administrator in your team to connect it.')
      }
      action={
        canConnect ? (
          <Link to="/connectors" className="btn btn-primary">
            Open Integrations
          </Link>
        ) : undefined
      }
    />
  )
}

function SummaryTiles({ report }: { report: Report }) {
  const series = useMemo(() => trendSeries(report), [report])
  const measured = report.trend_with_gaps.filter((v): v is number => v !== null && Number.isFinite(v))
  const peak = measured.length ? Math.max(...measured) : null

  return (
    <>
      {/* Grouped, because six tiles in a row is a row of six numbers. Three
          headings turn it into three answers: how is search going, is
          authority growing, what is paid costing. */}
      <SectionHeading title="Search & answers" />
      <div className="grid-tiles-sm">
        <StatTile kicker="Organic sessions" value={report.organic_sessions} size={26} />
        <StatTile kicker="AEO citations" value={report.aeo_citations} size={26} />
        <StatTile kicker="Pages optimised" value={report.pages_optimised} size={26} />
      </div>

      <SectionHeading title="Authority" spaced />
      <div className="grid-tiles-sm">
        <StatTile
          kicker="Backlinks won"
          value={
            report.backlinks_won === null ? (
              <span className="muted">{report.backlinks_won_note || NOT_TRACKED}</span>
            ) : (
              report.backlinks_won
            )
          }
          size={report.backlinks_won === null ? 18 : 26}
        />
        <StatTile
          kicker="Spam referrers blocked"
          value={report.referral_spam_blocked.toLocaleString('en-IN')}
          size={26}
        />
      </div>

      <SectionHeading title="Paid & protection" spaced />
      <div className="grid-tiles-sm">
        <StatTile
          kicker="Ad spend"
          value={
            report.ad_spend === null ? (
              <span className="muted">{report.ad_spend_note || NOT_TRACKED}</span>
            ) : (
              report.ad_spend
            )
          }
          size={report.ad_spend === null ? 18 : 26}
        />
        <StatTile
          kicker="Blended CAC"
          value={
            report.blended_cac === null ? (
              <span className="muted">{report.blended_cac_note || NOT_TRACKED}</span>
            ) : (
              moneyExact(report.blended_cac)
            )
          }
          size={report.blended_cac === null ? 18 : 26}
        />
        <StatTile kicker="Fraudulent clicks blocked" value={report.fraud_blocked} size={26} />
      </div>

      <SectionHeading
        title="Organic sessions"
        description={
          `Last ${report.trend_days} days, averaged into ${report.trend_with_gaps.length} points` +
          (peak !== null ? ` · peak ${peak.toLocaleString('en-IN')}` : '')
        }
        spaced
      />
      <div className="card trend-card">
        <LineChart
          title="Organic sessions"
          series={[series]}
          emptyText="No organic sessions recorded in this period. That is missing data, not a zero: the trend appears once an analytics connection has reported a day of traffic."
        />
        <p className="small muted">Gaps in the line are days nothing was recorded, not days with zero visits.</p>
      </div>
    </>
  )
}

// ── Detailed results (/insights/overview) ──────────────────────────────────
function DetailedResults({ overview }: { overview: InsightsOverview }) {
  const { seo, ads, social, content, agents } = overview.sections
  return (
    <>
      <SectionHeading
        title="Detailed results"
        description="Each figure is compared with the period of the same length just before it."
        spaced
      />
      {seo ? (
        <SectionBlock title="Search (SEO)" section={seo}>
          {seo.top_queries.length ? (
            <BarList
              label="Top searches by clicks"
              items={seo.top_queries.map((q) => ({ key: q.query, label: q.query, value: q.clicks, unit: 'count' }))}
            />
          ) : null}
        </SectionBlock>
      ) : null}
      {ads ? (
        <SectionBlock title="Ads" section={ads}>
          {ads.channels.length ? (
            <div className="table-scroll">
              <table className="table data-table">
                <caption className="sr-only">Ads by platform</caption>
                <thead>
                  <tr>
                    <th scope="col">Platform</th>
                    <th scope="col">Spend</th>
                    <th scope="col">Clicks</th>
                    <th scope="col">Conversions</th>
                    <th scope="col">Cost per conversion</th>
                  </tr>
                </thead>
                <tbody>
                  {ads.channels.map((channel) => (
                    <tr key={channel.channel}>
                      <th scope="row">{channel.label}</th>
                      <td>{formatValue(channel.spend, 'currency', { note: channel.note })}</td>
                      <td>{formatValue(channel.clicks, 'count', { note: channel.note })}</td>
                      <td>{formatValue(channel.conversions, 'count', { note: channel.note })}</td>
                      <td>{formatValue(channel.cac, 'currency', { note: channel.note })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </SectionBlock>
      ) : null}
      {social ? (
        <SectionBlock title="Social" section={social}>
          {social.platforms.length ? (
            <div className="table-scroll">
              <table className="table data-table">
                <caption className="sr-only">Social results by platform</caption>
                <thead>
                  <tr>
                    <th scope="col">Platform</th>
                    <th scope="col">Impressions</th>
                    <th scope="col">Reach</th>
                    <th scope="col">Engagement</th>
                    <th scope="col">Clicks</th>
                  </tr>
                </thead>
                <tbody>
                  {social.platforms.map((platform) => (
                    <tr key={platform.platform}>
                      <th scope="row">
                        {platform.label}
                        {platform.note ? <div className="small muted">{platform.note}</div> : null}
                      </th>
                      <td>{formatValue(platform.impressions, 'count')}</td>
                      <td>{formatValue(platform.reach, 'count')}</td>
                      <td>{formatValue(platform.engagement, 'count')}</td>
                      <td>{formatValue(platform.clicks, 'count')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <TopPosts posts={social.top_posts} labels={new Map(social.platforms.map((p) => [p.platform, p.label]))} />
        </SectionBlock>
      ) : null}
      {content ? (
        <SectionBlock title="Content" section={content}>
          {content.by_kind.length ? <BarList label="Made in this period, by kind" items={content.by_kind} /> : null}
        </SectionBlock>
      ) : null}
      {agents ? <SectionBlock title="Agents and approvals" section={agents} /> : null}
    </>
  )
}

function SectionBlock({
  title,
  section,
  children,
}: {
  title: string
  section: InsightSectionBase
  children?: ReactNode
}) {
  const { canView } = useAuth()
  return (
    <section className="card trend-card" aria-label={title}>
      <SectionHeading title={title} />
      {!section.available ? (
        <div className="panel-empty">
          <p>{section.connect_hint || NO_DATA_HINT}</p>
          {canView('connectors') ? (
            <p>
              <Link to="/connectors" className="btn btn-secondary btn-sm">
                Open Integrations
              </Link>
            </p>
          ) : null}
        </div>
      ) : null}
      {section.metrics.length ? (
        <div className="grid-tiles-sm">
          {section.metrics.map((metric) => (
            <MetricTile key={metric.key} metric={metric} />
          ))}
        </div>
      ) : null}
      {section.available
        ? section.series.map((series) => (
            <LineChart
              key={series.key}
              title={series.label}
              series={[series]}
              invert={series.unit === 'position'}
              height={160}
            />
          ))
        : null}
      {children}
    </section>
  )
}

function MetricTile({ metric }: { metric: InsightMetric }) {
  const missing = metric.value === null
  return (
    <StatTile
      kicker={metric.label}
      value={
        missing ? (
          <span className="muted">{formatMetric(metric)}</span>
        ) : (
          <>
            {formatMetric(metric)} <MetricDelta metric={metric} />
          </>
        )
      }
      size={missing ? 16 : 22}
    />
  )
}

/** Only server-built https links are made clickable; anything else is plain text. */
function safeHref(url: string | null): string | null {
  if (!url) return null
  try {
    return new URL(url).protocol === 'https:' ? url : null
  } catch {
    return null
  }
}

function TopPosts({ posts, labels }: { posts: SocialTopPost[]; labels: Map<string, string> }) {
  if (!posts.length) return null
  const byPlatform = new Map<string, SocialTopPost[]>()
  for (const post of posts) byPlatform.set(post.platform, [...(byPlatform.get(post.platform) ?? []), post])

  return (
    <>
      <SectionHeading title="Top posts, by platform" spaced />
      {[...byPlatform].map(([platform, items]) => (
        <div key={platform}>
          <h4>{labels.get(platform) ?? platform}</h4>
          <ul className="list">
            {items.map((post) => {
              const href = safeHref(post.url)
              const title = post.title || 'Untitled post'
              return (
                <li key={post.id} className="list-row">
                  {href ? (
                    <a href={href} target="_blank" rel="noopener noreferrer">
                      {title}
                    </a>
                  ) : (
                    <span>{title}</span>
                  )}
                  <span className="small muted">
                    {' '}
                    · {formatValue(post.impressions, 'count')} impressions · {formatValue(post.engagement, 'count')}{' '}
                    engagements
                  </span>
                </li>
              )
            })}
          </ul>
        </div>
      ))}
    </>
  )
}

// ── AI usage (/insights/llm-usage) ─────────────────────────────────────────
const GROUPS: { value: UsageGroup; label: string }[] = [
  { value: 'agent', label: 'Agent' },
  { value: 'model', label: 'Model' },
  { value: 'provider', label: 'Provider' },
  { value: 'day', label: 'Day' },
  { value: 'month', label: 'Month' },
  { value: 'job', label: 'Creative job' },
]
const PAGE_SIZE = Math.min(25, USAGE_MAX_PAGE_SIZE)

function tokens(value: number | null): string {
  return value === null ? 'unknown' : compactNumber(value)
}

function AiUsagePanel({ onForbidden }: { onForbidden: () => void }) {
  const [range, setRange] = useState<DateRange>(() => rangeForPreset('30d'))
  const [group, setGroup] = useState<UsageGroup>('agent')
  // Cursors of the pages before this one, for "Previous page".
  const [cursors, setCursors] = useState<(string | null)[]>([])
  const [cursor, setCursor] = useState<string | null>(null)

  const usage = useResource(
    (signal) => insightsApi.llmUsage({ from: range.from, to: range.to, group, cursor, limit: PAGE_SIZE }, signal),
    [range.from, range.to, group, cursor],
    `llm-usage:${range.from}:${range.to}:${group}:${cursor ?? ''}`,
  )

  useEffect(() => {
    if (usage.forbidden) onForbidden()
  }, [usage.forbidden, onForbidden])

  // A new question starts again at page one.
  const restart = () => {
    setCursors([])
    setCursor(null)
  }

  if (usage.forbidden) return null
  const data = usage.data

  return (
    <>
      <div className="reports-head">
        <div>
          <h2 className="reports-title">AI usage</h2>
          <p className="reports-period">Everything on this tab covers {describeRange(range)}.</p>
        </div>
        <DateRangePicker
          value={range}
          onChange={(next) => {
            setRange(next)
            restart()
          }}
        />
      </div>

      <p className="small muted">
        Costs are estimates, not invoices. They are worked out from the tokens each AI provider reported and the
        price table your administrator set up. Where no price is set, the cost says &quot;No price set&quot;; where a
        provider did not report its tokens, they show as &quot;unknown&quot;.
      </p>

      <SectionHeading
        title="Group by"
        action={
          <Segmented<UsageGroup>
            name="usage-group"
            value={group}
            options={GROUPS}
            onChange={(next) => {
              setGroup(next)
              restart()
            }}
          />
        }
        spaced
      />

      {usage.loading && !data ? <Loading label="Loading AI usage…" /> : null}
      {usage.error && !data ? <ErrorState message={usage.error} onRetry={usage.reload} /> : null}
      {data ? <UsageTable summary={data} /> : null}

      {data && (cursors.length > 0 || data.next_cursor) ? (
        <div className="table-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={cursors.length === 0 || usage.loading}
            onClick={() => {
              setCursor(cursors[cursors.length - 1] ?? null)
              setCursors(cursors.slice(0, -1))
            }}
          >
            Previous page
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={!data.next_cursor || usage.loading}
            onClick={() => {
              if (!data.next_cursor) return
              setCursors([...cursors, cursor])
              setCursor(data.next_cursor)
            }}
          >
            Next page
          </button>
        </div>
      ) : null}
    </>
  )
}

function UsageTable({ summary }: { summary: LlmUsageSummary }) {
  const { totals } = summary
  const totalCost = formatUsageCost(totals.cost, totals.cost_status, totals.cost_label)

  return (
    <>
      <div className="grid-tiles-sm">
        <StatTile kicker="AI runs" value={totals.runs.toLocaleString('en-IN')} size={22} />
        <StatTile kicker="Tokens in" value={tokens(totals.tokens_in)} size={22} />
        <StatTile kicker="Tokens out" value={tokens(totals.tokens_out)} size={22} />
        <StatTile kicker="Estimated cost" value={totalCost} size={22} />
      </div>
      {totals.unknown_token_runs > 0 ? (
        <p className="small muted">
          {totals.unknown_token_runs.toLocaleString('en-IN')} run{totals.unknown_token_runs === 1 ? '' : 's'} did not
          report tokens, so the totals leave them out.
        </p>
      ) : null}
      {summary.note ? <p className="small muted">{summary.note}</p> : null}

      {summary.lines.length === 0 ? (
        <div className="panel-empty">
          <p>No AI usage was recorded in this period.</p>
        </div>
      ) : isJobLines(summary) ? (
        <div className="table-scroll">
          <table className="table data-table">
            <caption className="sr-only">AI usage by Creative Studio job</caption>
            <thead>
              <tr>
                <th scope="col">Started</th>
                <th scope="col">Model</th>
                <th scope="col">Status</th>
                <th scope="col">Images</th>
                <th scope="col">Tokens in</th>
                <th scope="col">Tokens out</th>
                <th scope="col">Estimated cost</th>
              </tr>
            </thead>
            <tbody>
              {summary.lines.map((line) => (
                <tr key={line.job_id}>
                  <th scope="row">{new Date(line.created_at).toLocaleDateString()}</th>
                  <td>{line.model_label || line.text_model}</td>
                  <td>{JOB_STATUS_LABELS.get(line.status) ?? 'Unknown'}</td>
                  <td>{line.images_generated.toLocaleString('en-IN')}</td>
                  <td>{tokens(line.tokens_in)}</td>
                  <td>{tokens(line.tokens_out)}</td>
                  <td>{formatUsageCost(line.cost, line.cost_status, line.cost_label)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="table-scroll">
          <table className="table data-table">
            <caption className="sr-only">AI usage by {summary.group}</caption>
            <thead>
              <tr>
                <th scope="col">{GROUPS.find((g) => g.value === summary.group)?.label ?? 'Group'}</th>
                <th scope="col">Runs</th>
                <th scope="col">Tokens in</th>
                <th scope="col">Tokens out</th>
                <th scope="col">Estimated cost</th>
              </tr>
            </thead>
            <tbody>
              {(summary.lines as UsageLine[]).map((line) => (
                <tr key={line.key}>
                  <th scope="row">{line.label}</th>
                  <td>{line.runs.toLocaleString('en-IN')}</td>
                  <td>{tokens(line.tokens_in)}</td>
                  <td>{tokens(line.tokens_out)}</td>
                  <td>{formatUsageCost(line.cost, line.cost_status, line.cost_label)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
