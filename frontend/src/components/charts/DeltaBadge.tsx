/**
 * The change against the previous period, in words.
 *
 * Colour is a hint only: every badge carries an arrow (▲ / ▼ / –) and a word
 * ("up", "down", "no change"), so it reads the same in greyscale and to a
 * screen reader. A previous value of 0 or null has no meaningful percentage,
 * so it says "New" or "No earlier data" instead of inventing one.
 */
import { formatValue, type InsightMetric, type InsightUnit } from '@/api/insights'
import '@/styles/insights.css'

/** Changes smaller than this (in percent) read as "no change". */
const FLAT_PCT = 0.05
/** Beyond this the exact number stops meaning anything. */
const MAX_PCT = 999

type Tone = 'good' | 'bad' | 'neutral'

export interface DeltaBadgeProps {
  current: number | null | undefined
  previous: number | null | undefined
  /** The server's figure; worked out from the two values when missing. */
  changePct?: number | null
  /** Which direction is good news. Defaults to `'up'`. */
  better?: 'up' | 'down'
  /** Used in the spoken sentence for the previous value. */
  unit?: InsightUnit
  /** Defaults to "vs previous period". */
  comparison?: string
}

interface Delta {
  arrow: string
  /** Short lead, e.g. "0.4% up" or "No change". Empty when the whole sentence is `text`. */
  amount: string
  /** Trailing comparison, e.g. "vs previous period". Wraps on a second line in a tight tile. */
  comparison: string
  text: string
  tone: Tone
  spoken: string
}

function deltaParts(amount: string, comparison: string, extra: Omit<Delta, 'amount' | 'comparison' | 'text'>): Delta {
  const text = [amount, comparison].filter(Boolean).join(' ')
  return { ...extra, amount, comparison, text }
}

export function describeDelta({
  current,
  previous,
  changePct,
  better = 'up',
  unit = 'count',
  comparison = 'vs previous period',
}: DeltaBadgeProps): Delta {
  if (current === null || current === undefined || !Number.isFinite(current)) {
    return deltaParts('No data for this period', '', {
      arrow: '',
      tone: 'neutral',
      spoken: 'No data for this period',
    })
  }
  if (previous === null || previous === undefined || !Number.isFinite(previous)) {
    return deltaParts('No earlier data', '', {
      arrow: '',
      tone: 'neutral',
      spoken: 'No earlier data to compare with',
    })
  }
  const before = formatValue(previous, unit)
  if (previous === 0) {
    if (current === 0) {
      return deltaParts('No change', comparison, {
        arrow: '–',
        tone: 'neutral',
        spoken: `No change ${comparison}`,
      })
    }
    return deltaParts('New', comparison, {
      arrow: current > 0 ? '▲' : '▼',
      tone: toneFor(current > 0 ? 'up' : 'down', better),
      spoken: 'New: there was none in the previous period',
    })
  }

  const pct =
    changePct !== null && changePct !== undefined && Number.isFinite(changePct)
      ? changePct
      : ((current - previous) / Math.abs(previous)) * 100
  if (Math.abs(pct) < FLAT_PCT) {
    return deltaParts('No change', comparison, {
      arrow: '–',
      tone: 'neutral',
      spoken: `No change from ${before} in the previous period`,
    })
  }
  const direction = pct > 0 ? 'up' : 'down'
  const size = Math.abs(pct)
  const amount = size > MAX_PCT ? `over ${MAX_PCT}%` : `${size < 10 ? size.toFixed(1) : Math.round(size)}%`
  return deltaParts(`${amount} ${direction}`, comparison, {
    arrow: direction === 'up' ? '▲' : '▼',
    tone: toneFor(direction, better),
    spoken: `${direction === 'up' ? 'Up' : 'Down'} ${amount} from ${before} in the previous period`,
  })
}

function toneFor(direction: 'up' | 'down', better: 'up' | 'down'): Tone {
  return direction === better ? 'good' : 'bad'
}

export function DeltaBadge(props: DeltaBadgeProps) {
  const delta = describeDelta(props)
  return (
    <span className={`ins-delta ins-delta-${delta.tone}`} title={delta.spoken}>
      {delta.arrow ? (
        <span className="ins-delta-arrow" aria-hidden="true">
          {delta.arrow}
        </span>
      ) : null}
      <span aria-hidden="true" className="ins-delta-copy">
        {delta.amount ? <span className="ins-delta-amount">{delta.amount}</span> : null}
        {delta.comparison ? <span className="ins-delta-vs"> {delta.comparison}</span> : null}
        {!delta.amount && !delta.comparison ? delta.text : null}
      </span>
      <span className="sr-only">{delta.spoken}</span>
    </span>
  )
}

/** A badge straight from an insight metric. */
export function MetricDelta({ metric }: { metric: InsightMetric }) {
  return (
    <DeltaBadge
      current={metric.value}
      previous={metric.previous}
      changePct={metric.change_pct}
      better={metric.better}
      unit={metric.unit}
    />
  )
}
