/**
 * Insights API (`/insights/overview` and `/insights/llm-usage`).
 *
 * Types mirror `backend/app/api/v1/insights.py` (step 5.5). The same rule runs
 * through every type here: a figure the server does not have is `null`, never
 * `0`. A chart, tile or badge that turns `null` into `0` reports a measurement
 * that was never taken, so the display helpers at the bottom of this file all
 * return words ("No data", "No price set") for a missing value.
 *
 * Everything goes through the shared client in `./client`, so auth, token
 * refresh and the `{code, detail, fields}` error shape behave as on every
 * other screen. Query values are checked before they are sent, so a bad value
 * can never reshape the request.
 */
import { request } from './client'
import { moneyCompact, moneyExact } from '@/lib/money'

// ── Limits (mirror the server; the server still decides) ───────────────────
/** `settings.insights_max_range_days`. */
export const INSIGHTS_MAX_RANGE_DAYS = 366
/** `llm_usage.MAX_PAGE_SIZE`. */
export const USAGE_MAX_PAGE_SIZE = 100
/** `llm_usage.CURSOR_MAX_LENGTH`. */
const USAGE_CURSOR_MAX_LENGTH = 400

// ── Shared shapes ──────────────────────────────────────────────────────────
/**
 * How a value is written. `position` is a search ranking (lower is better);
 * `currency` is in the workspace currency; `percent` is already 0-100.
 */
export type InsightUnit = 'count' | 'currency' | 'percent' | 'position' | 'tokens' | 'score'

/** One figure, with the same figure for the previous period of equal length. */
export interface InsightMetric {
  key: string
  label: string
  /** `null` when nothing was reported for the period. */
  value: number | null
  /** `null` when there is no earlier data to compare with. */
  previous: number | null
  /** Percentage change from `previous`; `null` when it cannot be worked out. */
  change_pct: number | null
  unit: InsightUnit
  /** Which direction is good news. Defaults to `'up'`. */
  better?: 'up' | 'down'
  /** True for estimates (LLM cost); shown with "est.". */
  is_estimate?: boolean
  /** Plain text shown instead of a missing value, e.g. "Not reported by Instagram". */
  note?: string
}

/** One day (or one audit version) on a chart. `value: null` is a gap. */
export interface InsightPoint {
  /** `YYYY-MM-DD`, or a version label for audit trends. */
  date: string
  value: number | null
}

export interface InsightSeries {
  key: string
  label: string
  unit: InsightUnit
  points: InsightPoint[]
}

/** Fields every overview section has. */
export interface InsightSectionBase {
  /** False when no data source is connected or nothing was recorded. */
  available: boolean
  /** Plain advice for an unavailable section, e.g. "Connect Search Console to see clicks". */
  connect_hint: string
  metrics: InsightMetric[]
  series: InsightSeries[]
}

/** A labelled figure in a breakdown list (per channel, platform, kind...). */
export interface InsightBreakdownItem {
  key: string
  label: string
  value: number | null
  unit: InsightUnit
  note?: string
}

export interface SeoTopQuery {
  query: string
  clicks: number | null
  impressions: number | null
  position: number | null
}

export interface SeoSection extends InsightSectionBase {
  top_queries: SeoTopQuery[]
}

export interface AdsChannel {
  channel: string
  label: string
  spend: number | null
  clicks: number | null
  conversions: number | null
  cac: number | null
  note?: string
}

export interface AdsSection extends InsightSectionBase {
  channels: AdsChannel[]
}

export interface SocialPlatform {
  platform: string
  label: string
  impressions: number | null
  reach: number | null
  engagement: number | null
  clicks: number | null
  /** e.g. "Reach is not reported by Facebook". */
  note?: string
}

export interface SocialTopPost {
  id: string
  platform: string
  title: string
  /** Server-built https link to the published post, or null. */
  url: string | null
  posted_at: string | null
  impressions: number | null
  engagement: number | null
}

export interface SocialSection extends InsightSectionBase {
  platforms: SocialPlatform[]
  top_posts: SocialTopPost[]
}

export interface ContentSection extends InsightSectionBase {
  by_kind: InsightBreakdownItem[]
}

export type AgentsSection = InsightSectionBase

export interface LlmSection extends InsightSectionBase {
  currency_symbol: string
  /** e.g. the "AI image costs are not included" note. */
  note?: string
}

/**
 * `GET /insights/overview`. A section the caller's role may not see is left
 * out entirely, so every section is optional. Matches `SectionsOut` on the
 * server: there is no audits section.
 */
export interface InsightsOverview {
  from: string
  to: string
  previous_from: string
  previous_to: string
  days: number
  sections: {
    seo?: SeoSection
    ads?: AdsSection
    social?: SocialSection
    content?: ContentSection
    agents?: AgentsSection
    llm?: LlmSection
  }
}

// ── LLM usage (4.8 summary shapes) ─────────────────────────────────────────
export type UsageGroup = 'agent' | 'model' | 'provider' | 'day' | 'month' | 'job'
const USAGE_GROUPS: readonly UsageGroup[] = ['agent', 'model', 'provider', 'day', 'month', 'job']

/** `estimate | partial | no_price | unknown`. `no_price` means `cost` is null. */
export type CostStatus = 'estimate' | 'partial' | 'no_price' | 'unknown'

export interface UsageLine {
  key: string
  label: string
  runs: number
  tokens_in: number | null
  tokens_out: number | null
  /** Server-written count, or "unknown". */
  tokens_in_text: string
  tokens_out_text: string
  unknown_token_runs: number
  cost: number | null
  cost_status: CostStatus
  cost_label: string
  connector: string
  model: string
  is_estimate: boolean
}

export interface JobUsageLine {
  job_id: string
  created_at: string
  status: string
  text_connector: string
  text_model: string
  model_label: string
  image_model: string
  tokens_in: number | null
  tokens_out: number | null
  tokens_in_text: string
  tokens_out_text: string
  images_generated: number
  cost: number | null
  cost_status: CostStatus
  cost_label: string
  is_estimate: boolean
}

export interface UsageTotals {
  runs: number
  tokens_in: number | null
  tokens_out: number | null
  tokens_in_text: string
  tokens_out_text: string
  unknown_token_runs: number
  cost: number | null
  cost_status: CostStatus
  cost_label: string
  images_generated: number | null
  is_estimate: boolean
}

/** `GET /insights/llm-usage`. `lines` are `JobUsageLine`s when `group === 'job'`. */
export interface LlmUsageSummary {
  group: UsageGroup
  start: string
  end: string
  lines: UsageLine[] | JobUsageLine[]
  totals: UsageTotals
  next_cursor: string | null
  currency_symbol: string
  note: string
}

export function isJobLines(summary: LlmUsageSummary): summary is LlmUsageSummary & { lines: JobUsageLine[] } {
  return summary.group === 'job'
}

// ── Query building ─────────────────────────────────────────────────────────
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const CURSOR = /^[A-Za-z0-9_\-=]+$/

function checkDate(value: string): string {
  if (!ISO_DATE.test(value) || Number.isNaN(Date.parse(`${value}T00:00:00Z`))) {
    throw new Error('Invalid date')
  }
  return value
}

export interface UsageQuery {
  from: string
  to: string
  group: UsageGroup
  cursor?: string | null
  limit?: number
}

function rangeParams(from: string, to: string): URLSearchParams {
  return new URLSearchParams({ from: checkDate(from), to: checkDate(to) })
}

function usagePath(query: UsageQuery): string {
  const params = rangeParams(query.from, query.to)
  if (!USAGE_GROUPS.includes(query.group)) throw new Error('Invalid group')
  params.set('group', query.group)
  if (query.cursor) {
    // The cursor is opaque and the server re-checks it; this only stops an
    // odd value travelling at all.
    if (query.cursor.length > USAGE_CURSOR_MAX_LENGTH || !CURSOR.test(query.cursor)) {
      throw new Error('Invalid cursor')
    }
    params.set('cursor', query.cursor)
  }
  if (query.limit !== undefined) {
    if (!Number.isInteger(query.limit) || query.limit < 1 || query.limit > USAGE_MAX_PAGE_SIZE) {
      throw new Error('Invalid page size')
    }
    params.set('limit', String(query.limit))
  }
  return `/insights/llm-usage?${params.toString()}`
}

// ── Endpoints ──────────────────────────────────────────────────────────────
export const insightsApi = {
  overview: (from: string, to: string, signal?: AbortSignal) =>
    request<InsightsOverview>(`/insights/overview?${rangeParams(from, to).toString()}`, { signal }),
  /** Needs Agents or Admin view; never offered to Client Viewers. */
  llmUsage: (query: UsageQuery, signal?: AbortSignal) =>
    request<LlmUsageSummary>(usagePath(query), { signal }),
}

// ── Display helpers ────────────────────────────────────────────────────────
/** Shown for a value the server did not have. Never "0". */
export const NO_DATA = 'No data'
const NO_PRICE = 'No price set'

/** "Not reported by Instagram", for a figure a platform does not give out. */
export function notReportedBy(source: string): string {
  return `Not reported by ${source}`
}

const compactFormat = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 })
const plainFormat = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })
const oneDecimal = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 1, maximumFractionDigits: 1 })

/** `12.3K`, `4.5M`; small numbers as they are. */
export function compactNumber(value: number): string {
  if (!Number.isFinite(value)) return NO_DATA
  return Math.abs(value) < 1000 ? plainFormat.format(value) : compactFormat.format(value)
}

/** A currency amount; estimates end in "est." so nobody reads them as an invoice. */
function formatCurrency(value: number, estimate = false): string {
  if (!Number.isFinite(value)) return NO_DATA
  // Small sums keep their paise (an LLM run costs a few rupees); larger ones
  // use the console's lakh/crore tile format from `lib/money`.
  const text = Math.abs(value) < 1000 ? moneyExact(value) : moneyCompact(value)
  return estimate ? `${text} est.` : text
}

/** Any insight value as text, in its unit. A missing value becomes words. */
export function formatValue(
  value: number | null | undefined,
  unit: InsightUnit,
  options: { estimate?: boolean; note?: string } = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return options.note?.trim() || (unit === 'currency' && options.estimate ? NO_PRICE : NO_DATA)
  }
  switch (unit) {
    case 'currency':
      return formatCurrency(value, options.estimate ?? false)
    case 'percent':
      return `${oneDecimal.format(value)}%`
    case 'position':
      return oneDecimal.format(value)
    case 'score':
      return plainFormat.format(value)
    case 'count':
    case 'tokens':
      return compactNumber(value)
  }
}

/** An insight metric's own value as text. */
export function formatMetric(metric: InsightMetric): string {
  return formatValue(metric.value, metric.unit, { estimate: metric.is_estimate, note: metric.note })
}

/** An LLM cost from the usage summary, honouring its status. */
export function formatUsageCost(cost: number | null, status: CostStatus, label: string): string {
  if (status === 'no_price' || cost === null) return status === 'unknown' ? label || 'Unknown' : NO_PRICE
  const text = formatCurrency(cost, true)
  return status === 'partial' ? `${text} (partial)` : text
}
