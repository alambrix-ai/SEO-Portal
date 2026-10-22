/**
 * Overview - the enterprise dashboard: headline figures for a chosen period
 * (compared with the period before it), one section per area (search, social,
 * ads, content, AI usage), the get-started checklist until the workspace is
 * live, agent activity, the approval queue and integration health.
 *
 * The figures come from `/insights/overview`. The rule that runs through the
 * whole screen: a figure nobody measured is shown in words ("Not tracked
 * yet", "Not reported by Instagram"), never as 0. A section with no data
 * source says how to connect one instead of drawing an empty chart.
 *
 * Only agents that are working or configured are listed; a workspace where
 * nothing has started shows the checklist that gets it there instead of a
 * grid of paused rows.
 */
import { useId, useState } from 'react'
import type { MouseEvent, ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '@/api/client'
import {
  formatMetric,
  insightsApi,
  notReportedBy,
  type InsightBreakdownItem,
  type InsightMetric,
  type InsightSectionBase,
  type InsightSeries,
  type InsightsOverview,
} from '@/api/insights'
import { useAuth } from '@/auth/AuthContext'
import { AgentIcon } from '@/components/AgentIcon'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { BarList } from '@/components/charts/BarList'
import { MetricDelta } from '@/components/charts/DeltaBadge'
import { DonutChart } from '@/components/charts/DonutChart'
import { LineChart } from '@/components/charts/LineChart'
import { Sparkline } from '@/components/charts/Sparkline'
import { DateRangePicker, describeRange, rangeForPreset, type DateRange } from '@/components/DateRangePicker'
import {
  AdsIcon,
  ApprovalsIcon,
  CheckIcon,
  ChevronRightIcon,
  CreativeIcon,
  SeoIcon,
} from '@/components/icons'
import { useResource } from '@/hooks/useResource'
import { agentState, nextRunText } from '@/lib/agentState'
import { EmptyState, ErrorState, PageHeader, StatusDot } from '@/components/ui'
import type { ModuleKey } from '@/api/types'

function greeting(): string {
  const hour = new Date().getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 18) return 'Good afternoon'
  return 'Good evening'
}

// ── Wording ────────────────────────────────────────────────────────────────
/** Shown in a tile whose data source is not connected. Never a 0. */
const NOT_CONNECTED = 'Not connected yet'
const NOT_TRACKED = 'Not tracked yet'
/** Advice for a section the server left out (older server, or a hidden module). */
const FALLBACK_HINTS: Record<SectionKey, string> = {
  seo: 'Connect Google Search Console to see search clicks.',
  social: 'Connect a social account and publish a post to see reach and engagement.',
  ads: 'Connect an ad account, such as Google Ads or Meta Ads, to see spend, clicks and conversions.',
  content: 'Nothing has been made yet. Ask a content agent or use Creative Studio to create something.',
  agents: 'Start an agent to see its work here.',
  llm: 'AI usage appears here once an agent or Creative Studio has run.',
}
/** Plainer names than the server's for a few figures. */
const LABELS: Record<string, string> = {
  pages_optimised: 'Pages improved',
  issues_fixed: 'Issues fixed',
  position: 'Average position',
  engagement: 'Engagement',
  pending_approvals: 'Waiting for your review',
}

type SectionKey = keyof InsightsOverview['sections']

/** Which module decides whether the caller's role may see a section at all. */
function roleCanSee(section: SectionKey, canView: (module: ModuleKey) => boolean): boolean {
  switch (section) {
    case 'seo':
      return canView('seo')
    case 'ads':
      return canView('ads')
    case 'social':
    case 'content':
      return canView('content')
    case 'agents':
      return canView('agents') || canView('approvals')
    case 'llm':
      // Only shown when the server sends it.
      return false
  }
}

function findMetric(section: InsightSectionBase | undefined, key: string): InsightMetric | undefined {
  return section?.metrics.find((metric) => metric.key === key)
}

/** The daily series drawn beside a metric; social calls it "engagements". */
function findSeries(section: InsightSectionBase | undefined, key: string): InsightSeries | undefined {
  return section?.series.find((series) => series.key === key || series.key === `${key}s`)
}

function hasPoints(series: InsightSeries | undefined): series is InsightSeries {
  return Boolean(series?.points.some((point) => point.value !== null && Number.isFinite(point.value)))
}

function metricLabel(metric: InsightMetric): string {
  return LABELS[metric.key] ?? metric.label
}

function isKnown(value: number | null | undefined): value is number {
  return value !== null && value !== undefined && Number.isFinite(value)
}

/** "1 Oct 2026" from `YYYY-MM-DD`. */
function longDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return value
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

/** Two counts added, but only when both were measured. */
function addKnown(a: number | null | undefined, b: number | null | undefined): number | null {
  return isKnown(a) && isKnown(b) ? a + b : null
}

// ── Tiles ──────────────────────────────────────────────────────────────────
interface TileProps {
  label: string
  /** The figure; missing when the section is not there. */
  metric?: InsightMetric
  /** Shown instead of the value (no data source, role cannot see it). */
  text?: string
  meta?: string
  /** A daily series for a small trend line. */
  series?: InsightSeries
  /** Makes the whole tile a link (keyboard-accessible). */
  to?: string
  attention?: boolean
  /** For a live count or a fixed figure, where "vs previous period" means nothing. */
  noDelta?: boolean
  icon?: ReactNode
  iconTone?: 'copper' | 'forest' | 'warn' | 'alert'
}

function Tile({
  label,
  metric,
  text,
  meta,
  series,
  to,
  attention = false,
  noDelta = false,
  icon,
  iconTone = 'copper',
}: TileProps) {
  const known = !text && metric !== undefined && isKnown(metric.value)
  const shown = text ?? (metric ? formatMetric(metric) : NOT_TRACKED)
  const lowerIsBetter = metric?.better === 'down' && metric.unit === 'position'
  const iconClass =
    iconTone === 'forest' ? ' is-forest' : iconTone === 'warn' ? ' is-warn' : iconTone === 'alert' ? ' is-alert' : ''
  const trend = known && metric && hasPoints(series)
  const body = (
    <>
      <div className="dash-stat-top">
        <div className="kpi-label">
          {label}
          {lowerIsBetter ? <span className="kpi-total"> (lower is better)</span> : null}
        </div>
        {icon ? (
          <span className={`dash-stat-icon${iconClass}`} aria-hidden="true">
            {icon}
          </span>
        ) : null}
      </div>
      <div className={known ? 'kpi-value' : 'kpi-value ins-missing'} style={known ? undefined : { fontSize: 15 }}>
        {shown}
      </div>
      {known && metric && !noDelta && metric.key !== 'pending_approvals' ? <MetricDelta metric={metric} /> : null}
      {meta ? <div className="kpi-meta">{meta}</div> : null}
      {trend && metric && series ? (
        <div className="dash-stat-chart">
          <Sparkline label={label} points={series.points} unit={series.unit} invert={metric.better === 'down'} />
        </div>
      ) : null}
    </>
  )
  const className = `kpi dash-stat${attention ? ' is-attention' : ''}`
  return to ? (
    <Link to={to} className={`${className} kpi-link`}>
      {body}
    </Link>
  ) : (
    <div className={className}>{body}</div>
  )
}

/** Every metric of a section as tiles, each with its delta and trend. */
function MetricTiles({ section }: { section: InsightSectionBase }) {
  if (!section.metrics.length) return null
  return (
    <div className="kpi-grid">
      {section.metrics.map((metric) => (
        <Tile
          key={metric.key}
          label={metricLabel(metric)}
          metric={metric}
          series={findSeries(section, metric.key)}
          meta={metric.key === 'pending_approvals' ? 'Right now' : undefined}
        />
      ))}
    </div>
  )
}

// ── Sections ───────────────────────────────────────────────────────────────
interface InsightPanelProps {
  title: string
  description?: string
  section: InsightSectionBase | undefined
  sectionKey: SectionKey
  /** Where the "fix this" button goes when there is no data. */
  action?: ReactNode
  /** Link in the panel head (e.g. to Reports). */
  headLink?: ReactNode
  children?: ReactNode
}

function InsightPanel({ title, description, section, sectionKey, action, headLink, children }: InsightPanelProps) {
  const headingId = useId()
  const missing = !section || !section.available
  const hint = section?.connect_hint?.trim() || FALLBACK_HINTS[sectionKey]
  return (
    <section className="panel" aria-labelledby={headingId}>
      <div className="panel-head">
        <div>
          <h2 className="panel-title" id={headingId} style={{ margin: 0 }}>
            {title}
          </h2>
          {description ? <div className="panel-subtitle">{description}</div> : null}
        </div>
        {!missing ? headLink : null}
      </div>
      {missing ? (
        <div className="panel-empty-state" role="note">
          <p style={{ margin: action ? '0 0 12px' : 0 }}>{hint}</p>
          {action}
        </div>
      ) : (
        <div className="insight-body">
          {/* An available section can still carry advice ("Try a longer date range"). */}
          {section.connect_hint?.trim() ? (
            <p className="kpi-meta" role="note">
              {section.connect_hint}
            </p>
          ) : null}
          {children}
        </div>
      )}
    </section>
  )
}

function SubHeading({ children }: { children: ReactNode }) {
  return (
    <h3 className="panel-title" style={{ margin: 0 }}>
      {children}
    </h3>
  )
}

/** Shown while the figures load: the shape of the tile row, hidden from screen readers. */
function OverviewSkeleton() {
  const bar = (width: string, height: number) => (
    <div style={{ width, height, borderRadius: 4, background: 'var(--color-surface-subtle)' }} />
  )
  return (
    <div aria-busy="true">
      <p className="sr-only" role="status" aria-live="polite">
        Loading your figures…
      </p>
      <div className="kpi-grid" aria-hidden="true">
        {Array.from({ length: 8 }, (_, index) => (
          <div key={index} className="kpi">
            {bar('60%', 12)}
            {bar('40%', 26)}
            {bar('70%', 10)}
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Page ───────────────────────────────────────────────────────────────────
export function DashboardPage() {
  const { canView, session } = useAuth()
  const navigate = useNavigate()
  const globalAutonomy = session?.organization.global_autonomy
  const [range, setRange] = useState<DateRange>(() => rangeForPreset('30d'))
  const canSeeInsights = canView('reports')
  const brand = useResource((signal) => api.brandProfile(signal), [], 'brand-profile')
  const { data, loading, error, reload } = useResource(
    (signal) => api.dashboard(signal),
    [globalAutonomy],
    'dashboard',
  )
  const insights = useResource<InsightsOverview | null>(
    (signal) => (canSeeInsights ? insightsApi.overview(range.from, range.to, signal) : Promise.resolve(null)),
    [range.from, range.to, canSeeInsights],
    `insights-overview:${range.from}:${range.to}:${canSeeInsights ? 'on' : 'off'}`,
  )

  if (!session) return null

  const header = (
    <PageHeader
      title={`${greeting()}, ${session.user.name.split(' ')[0]}`}
      description={
        session.organization.primary_domain
          ? `Here is what is happening on ${session.organization.primary_domain}.`
          : 'Here is what is happening in your workspace.'
      }
    />
  )

  if (loading && !data) {
    return (
      <>
        {header}
        <OverviewSkeleton />
      </>
    )
  }
  if (error && !data) {
    return (
      <>
        {header}
        <ErrorState message={error} onRetry={reload} />
      </>
    )
  }
  if (!data) return null

  const { kpis, agents, connectors, approvals, ready } = data
  const rows = [...agents, ...ready]

  const hasBrand = Boolean(brand.data?.profile)
  const steps = [
    {
      done: hasBrand,
      title: 'Create your brand profile',
      text: 'Paste your website address — every content agent writes from it.',
      to: '/brand',
      cta: 'Create profile',
      visible: canView('content'),
    },
    {
      done: kpis.connectors_connected > 0,
      title: 'Connect your site and data',
      text: 'Add your CMS, analytics and any ad platforms.',
      to: '/connectors',
      cta: 'Open integrations',
      visible: canView('connectors'),
    },
    {
      done: data.ready_agents + data.active_agents > 0,
      title: 'Set up your first agent',
      text: 'Open an agent and follow the three setup steps.',
      to: '/agents',
      cta: 'Open agents',
      visible: canView('agents'),
    },
    {
      done: kpis.agents_running > 0,
      title: 'Start an agent',
      text: 'It runs on its schedule. Changes that need your approval appear under Approvals.',
      to: '/agents',
      cta: 'Open agents',
      visible: canView('agents'),
    },
  ].filter((step) => step.visible)
  const remaining = steps.filter((step) => !step.done).length

  // ── Figures ──
  const overview = insights.data
  const sections = overview?.sections ?? {}
  const { seo, social, ads, content, llm } = sections
  const integrationsButton = canView('connectors') ? (
    <Link to="/connectors" className="btn btn-secondary btn-sm">
      Go to Integrations
    </Link>
  ) : (
    <p className="kpi-meta" style={{ margin: 0 }}>
      Ask a workspace admin to connect it under Integrations.
    </p>
  )
  /** A section is drawn when the server sent it, or when the role could see it (then its hint shows). */
  const showSection = (key: SectionKey) => Boolean(sections[key]) || roleCanSee(key, canView)
  /** A headline tile's value, or the words for why there is none. */
  const tileText = (key: SectionKey, section: InsightSectionBase | undefined): string | undefined => {
    if (!section) return roleCanSee(key, canView) ? NOT_CONNECTED : 'Not available to your role'
    return section.available ? undefined : NOT_CONNECTED
  }

  const produced: InsightMetric | undefined = content
    ? {
        key: 'produced',
        label: 'Content and creatives produced',
        value: addKnown(findMetric(content, 'created')?.value, findMetric(content, 'creatives')?.value),
        previous: addKnown(findMetric(content, 'created')?.previous, findMetric(content, 'creatives')?.previous),
        change_pct: null,
        unit: 'count',
      }
    : undefined
  const visits = findMetric(seo, 'sessions')

  const presentSections = (Object.keys(sections) as SectionKey[]).filter((key) => sections[key])
  const brandNew =
    Boolean(overview) &&
    kpis.connectors_connected === 0 &&
    presentSections.every((key) => key === 'agents' || !sections[key]?.available)

  const reportsLink = canView('reports') ? (
    <Link to="/reports" className="panel-link">
      Full report <ChevronRightIcon size={14} />
    </Link>
  ) : null

  const heroSeries = [findSeries(seo, 'sessions'), findSeries(seo, 'clicks'), findSeries(ads, 'spend')].find(
    (series): series is InsightSeries => Boolean(series && hasPoints(series)),
  )

  const adsMix: InsightBreakdownItem[] = (ads?.channels ?? []).map((channel) => ({
    key: channel.channel,
    label: channel.label,
    value: channel.spend,
    unit: 'currency',
    note: channel.note ?? notReportedBy(channel.label),
  }))
  const socialMix: InsightBreakdownItem[] = (social?.platforms ?? []).map((platform) => ({
    key: platform.platform,
    label: platform.label,
    value: platform.engagement,
    unit: 'count',
    note: platform.note ?? notReportedBy(platform.label),
  }))
  const mix = adsMix.some((item) => isKnown(item.value))
    ? { title: 'Ad spend by channel', items: adsMix }
    : content?.by_kind.some((item) => isKnown(item.value))
      ? { title: 'Content produced by kind', items: content.by_kind }
      : socialMix.some((item) => isKnown(item.value))
        ? { title: 'Engagement by platform', items: socialMix }
        : null

  return (
    <>
      {header}

      {steps.length > 0 && remaining > 0 ? (
        <section className="panel setup-panel" aria-labelledby="dashboard-setup-heading">
          <div className="panel-head">
            <div>
              <h2 className="panel-title" id="dashboard-setup-heading" style={{ margin: 0 }}>
                Get started
              </h2>
              <div className="panel-subtitle">
                {steps.length - remaining} of {steps.length} complete
              </div>
            </div>
            <div className="progress-bar" aria-hidden="true">
              <div
                className="progress-bar-fill"
                style={{ width: `${((steps.length - remaining) / steps.length) * 100}%` }}
              />
            </div>
          </div>
          <ol className="setup-steps">
            {steps.map((step, index) => (
              <li key={step.title} className={`setup-step${step.done ? ' is-done' : ''}`}>
                <span className="setup-step-mark" aria-hidden="true">
                  {step.done ? <CheckIcon size={14} /> : index + 1}
                </span>
                <div className="setup-step-text">
                  <div className="setup-step-title">{step.title}</div>
                  <div className="setup-step-desc">{step.text}</div>
                </div>
                {!step.done ? (
                  <Link to={step.to} className="btn btn-secondary btn-sm">
                    {step.cta}
                  </Link>
                ) : null}
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {canSeeInsights ? (
        <section className="dash-figures" aria-labelledby="dashboard-figures-heading">
          <div className="dash-figures-head">
            <div>
              <h2 className="panel-title" id="dashboard-figures-heading">
                Your figures for {describeRange(range)}
              </h2>
              <div className="panel-subtitle">
                {overview
                  ? `Compared with ${longDate(overview.previous_from)} to ${longDate(overview.previous_to)}.`
                  : 'Compared with the period before it.'}
              </div>
            </div>
            <DateRangePicker value={range} onChange={setRange} disabled={insights.loading && !overview} />
          </div>

          {insights.loading && !overview ? (
            <OverviewSkeleton />
          ) : insights.error && !overview ? (
            insights.forbidden ? (
              <p className="kpi-meta">Your role cannot see these figures.</p>
            ) : (
              <ErrorState message={insights.error} onRetry={insights.reload} />
            )
          ) : overview ? (
            <>
              {brandNew ? (
                <div style={{ marginBottom: 16 }}>
                  <EmptyState
                    title="Your dashboard fills in as you connect your data"
                    description="Connect your website, analytics, ad and social accounts. Until then each figure stays blank rather than showing a made-up zero."
                    action={canView('connectors') ? integrationsButton : undefined}
                  />
                </div>
              ) : null}

              <div className="dash-stat-grid">
                <Tile
                  label="Search clicks"
                  metric={findMetric(seo, 'clicks')}
                  text={tileText('seo', seo)}
                  series={findSeries(seo, 'clicks')}
                  icon={<SeoIcon size={16} />}
                />
                <Tile
                  label="Website visits"
                  metric={visits}
                  text={visits ? undefined : tileText('seo', seo)}
                  series={findSeries(seo, 'sessions')}
                  icon={<SeoIcon size={16} />}
                  iconTone="forest"
                />
                <Tile
                  label="Social reach"
                  metric={findMetric(social, 'reach')}
                  text={tileText('social', social)}
                  series={findSeries(social, 'reach')}
                  icon={<CreativeIcon size={16} />}
                />
                <Tile
                  label="Ad spend"
                  metric={findMetric(ads, 'spend')}
                  text={tileText('ads', ads)}
                  series={findSeries(ads, 'spend')}
                  icon={<AdsIcon size={16} />}
                  iconTone="warn"
                />
                <Tile
                  label="Content produced"
                  metric={produced}
                  text={content ? undefined : tileText('content', content)}
                  icon={<CreativeIcon size={16} />}
                  iconTone="forest"
                />
                <Tile
                  label="Waiting for review"
                  metric={{
                    key: 'pending_approvals',
                    label: 'Waiting for review',
                    value: kpis.pending_approvals,
                    previous: null,
                    change_pct: null,
                    unit: 'count',
                  }}
                  meta={kpis.pending_approvals > 0 ? 'Right now' : 'Nothing is waiting'}
                  to={canView('approvals') ? '/approvals' : undefined}
                  attention={kpis.pending_approvals > 0}
                  noDelta
                  icon={<ApprovalsIcon size={16} />}
                  iconTone={kpis.pending_approvals > 0 ? 'alert' : 'copper'}
                />
              </div>

              <div className="dash-board">
                <section className="panel" aria-labelledby="dash-trend-heading">
                  <div className="panel-head">
                    <div>
                      <h2 className="panel-title" id="dash-trend-heading" style={{ margin: 0 }}>
                        {heroSeries ? heroSeries.label : 'Performance this period'}
                      </h2>
                      <div className="panel-subtitle">
                        Daily totals for this period. Days with no data are gaps, not zeros.
                      </div>
                    </div>
                    {reportsLink}
                  </div>
                  <div className="insight-body">
                    {heroSeries ? (
                      <LineChart title={heroSeries.label} series={[heroSeries]} height={248} filled />
                    ) : (
                      <div className="ins-chart-empty" role="note">
                        <p>
                          Connect Search Console, analytics or an ad account to see a trend here. Missing days stay
                          blank rather than showing a zero.
                        </p>
                      </div>
                    )}
                  </div>
                </section>
                <section className="panel" aria-labelledby="dash-mix-heading">
                  <div className="panel-head">
                    <div>
                      <h2 className="panel-title" id="dash-mix-heading" style={{ margin: 0 }}>
                        Mix
                      </h2>
                      <div className="panel-subtitle">
                        {mix ? mix.title : 'How the period splits once a source is connected.'}
                      </div>
                    </div>
                  </div>
                  <div className="insight-body">
                    {mix ? (
                      <DonutChart title={mix.title} items={mix.items} />
                    ) : (
                      <div className="ins-chart-empty" role="note">
                        <p>{FALLBACK_HINTS.ads}</p>
                        {integrationsButton}
                      </div>
                    )}
                  </div>
                </section>
              </div>

              <div className="dash-split">
                {showSection('seo') ? (
                  <InsightPanel
                    title="Search"
                    description="How people find you on Google and Bing."
                    section={seo}
                    sectionKey="seo"
                    action={integrationsButton}
                    headLink={reportsLink}
                  >
                    {seo ? (
                      <>
                        {hasPoints(findSeries(seo, 'clicks')) ? (
                          <LineChart
                            title="Search clicks"
                            series={[findSeries(seo, 'clicks') as InsightSeries]}
                            height={180}
                            filled
                          />
                        ) : null}
                        {hasPoints(findSeries(seo, 'position')) ? (
                          <div>
                            <SubHeading>Average position (lower is better)</SubHeading>
                            <LineChart
                              title="Average search position"
                              series={[findSeries(seo, 'position') as InsightSeries]}
                              invert
                              height={140}
                            />
                          </div>
                        ) : null}
                        {seo.top_queries.length > 0 ? (
                          <div>
                            <SubHeading>Top searches</SubHeading>
                            <BarList
                              label="Top searches by clicks"
                              items={seo.top_queries.map((query) => ({
                                key: query.query,
                                label: query.query,
                                value: query.clicks,
                                unit: 'count',
                              }))}
                            />
                          </div>
                        ) : null}
                      </>
                    ) : null}
                  </InsightPanel>
                ) : null}

                {showSection('social') ? (
                  <InsightPanel
                    title="Social"
                    description="Reach and engagement of published posts."
                    section={social}
                    sectionKey="social"
                    action={integrationsButton}
                    headLink={reportsLink}
                  >
                    {social ? (
                      <>
                        {social.series.some(hasPoints) ? (
                          <LineChart
                            title="Reach and engagement"
                            series={social.series.filter(hasPoints)}
                            height={180}
                            filled
                          />
                        ) : null}
                        {socialMix.length > 0 ? (
                          <div>
                            <SubHeading>By platform</SubHeading>
                            <BarList label="Engagement by platform" items={socialMix} />
                          </div>
                        ) : null}
                      </>
                    ) : null}
                  </InsightPanel>
                ) : null}
              </div>

              <div className="dash-split">
                {showSection('ads') ? (
                  <InsightPanel
                    title="Ads"
                    description="Spend, clicks and conversions across ad accounts."
                    section={ads}
                    sectionKey="ads"
                    action={integrationsButton}
                    headLink={reportsLink}
                  >
                    {ads ? (
                      <>
                        {hasPoints(findSeries(ads, 'spend')) ? (
                          <LineChart
                            title="Ad spend"
                            series={[findSeries(ads, 'spend') as InsightSeries]}
                            height={180}
                            filled
                          />
                        ) : null}
                        {adsMix.length > 0 ? (
                          <div>
                            <SubHeading>By channel</SubHeading>
                            <BarList label="Spend by channel" items={adsMix} />
                          </div>
                        ) : null}
                      </>
                    ) : null}
                  </InsightPanel>
                ) : null}

                {showSection('content') ? (
                  <InsightPanel
                    title="Content"
                    description="What agents and Creative Studio made this period."
                    section={content}
                    sectionKey="content"
                    action={
                      canView('content') ? (
                        <Link to="/creative" className="btn btn-secondary btn-sm">
                          Open Creative Studio
                        </Link>
                      ) : undefined
                    }
                  >
                    {content ? (
                      content.by_kind.length > 0 ? (
                        <DonutChart title="Items made by kind" items={content.by_kind} />
                      ) : (
                        <BarList label="Items made by kind" items={content.by_kind} />
                      )
                    ) : null}
                  </InsightPanel>
                ) : null}
              </div>

              {llm ? (
                <InsightPanel title="AI usage" description="Tokens and estimated model cost." section={llm} sectionKey="llm" headLink={reportsLink}>
                  <MetricTiles section={llm} />
                  {llm.note?.trim() ? <p className="kpi-meta">{llm.note}</p> : null}
                </InsightPanel>
              ) : null}
            </>
          ) : null}
        </section>
      ) : null}

      <div className="dash-ops">
        <section className="panel" aria-labelledby="dashboard-activity-heading">
          <div className="panel-head">
            <h2 className="panel-title" id="dashboard-activity-heading" style={{ margin: 0 }}>
              Agent activity
            </h2>
            {canView('agents') ? (
              <Link to="/agents" className="panel-link">
                View all <ChevronRightIcon size={14} />
              </Link>
            ) : null}
          </div>
          {rows.length > 0 ? (
            <ul className="list">
              {rows.map((agent) => (
                // The agent's name is a real link (keyboard and screen
                // readers); the rest of the row is a larger mouse target.
                <li
                  key={agent.slug}
                  className="list-row is-link"
                  onClick={(event: MouseEvent<HTMLLIElement>) => {
                    if ((event.target as HTMLElement).closest('a')) return
                    navigate(`/agents/${agent.slug}`)
                  }}
                >
                  <AgentIcon slug={agent.slug} size={28} />
                  <div className="list-row-main">
                    <div className="list-row-title">
                      <Link to={`/agents/${agent.slug}`} style={{ color: 'inherit' }}>
                        {agent.name}
                      </Link>
                    </div>
                    <div className="list-row-meta">
                      {agent.busy
                        ? agent.step || 'Working'
                        : agent.last_run_summary || agent.metric_label || 'No runs yet'}
                    </div>
                  </div>
                  <div className="list-row-side">
                    <StatusDot tone={agentState(agent).tone}>{agentState(agent).label}</StatusDot>
                    <span className="list-row-sub">
                      {agent.busy ? 'In progress' : agent.status === 'running' ? nextRunText(agent) : ''}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="panel-empty-state">
              No agents are set up yet.
              {canView('agents') ? (
                <>
                  {' '}
                  <Link to="/agents">Set one up</Link> to start optimizing.
                </>
              ) : null}
            </div>
          )}
        </section>

          <section className="panel" aria-labelledby="dashboard-approvals-heading">
            <div className="panel-head">
              <h2 className="panel-title" id="dashboard-approvals-heading" style={{ margin: 0 }}>
                Needs your approval
              </h2>
              {canView('approvals') && approvals.length > 0 ? (
                <Link to="/approvals" className="panel-link">
                  Review <ChevronRightIcon size={14} />
                </Link>
              ) : null}
            </div>
            {approvals.length > 0 ? (
              <ul className="list">
                {approvals.map((item) => (
                  <li key={item.id} className="list-row is-compact">
                    <div className="list-row-main">
                      <div className="list-row-title">{item.title}</div>
                      <div className="list-row-meta">
                        {item.type} · {item.agent} · {item.meta}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="panel-empty-state">
                {canView('approvals')
                  ? 'Nothing is waiting for review.'
                  : 'The approval queue is not visible to your role.'}
              </div>
            )}
          </section>

          {canView('connectors') ? (
            <section className="panel" aria-labelledby="dashboard-integrations-heading">
              <div className="panel-head">
                <h2 className="panel-title" id="dashboard-integrations-heading" style={{ margin: 0 }}>
                  Integration health
                </h2>
                <Link to="/connectors" className="panel-link">
                  Manage <ChevronRightIcon size={14} />
                </Link>
              </div>
              {connectors.length > 0 ? (
                <ul className="list">
                  {connectors.map((connector) => {
                    const failing =
                      connector.health === 'failing' || connector.health === 'degraded'
                    return (
                      <li key={connector.slug} className="list-row is-compact">
                        <ConnectorIcon slug={connector.slug} name={connector.name} size={24} />
                        <div className="list-row-main">
                          <div className="list-row-title">{connector.name}</div>
                          {connector.last_error ? (
                            <div className="list-row-meta is-error">{connector.last_error}</div>
                          ) : connector.activity_label ? (
                            <div className="list-row-meta">{connector.activity_label}</div>
                          ) : null}
                        </div>
                        <StatusDot tone={failing ? 'danger' : 'success'}>
                          {failing ? 'Issue' : 'Healthy'}
                        </StatusDot>
                      </li>
                    )
                  })}
                </ul>
              ) : (
                <div className="panel-empty-state">
                  Nothing connected yet. <Link to="/connectors">Add an integration</Link>.
                </div>
              )}
            </section>
          ) : null}
      </div>
    </>
  )
}
