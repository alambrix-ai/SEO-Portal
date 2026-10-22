/**
 * A compact area chart for a KPI tile.
 *
 * The number already states the value; this shows the shape of the period.
 * A series that barely moved is drawn near the top of a 0-based band so a
 * 2% wobble is not stretched into a crash. Missing days stay gaps, never
 * zeros. A series with nothing in it renders nothing rather than a flat line.
 */
import { useId, useMemo } from 'react'

import { formatValue, type InsightPoint, type InsightUnit } from '@/api/insights'
import { areaAttr, pointsAttr, segments } from './LineChart'
import '@/styles/insights.css'

const W = 240
const H = 72
const PAD = { x: 8, y: 10 }

export interface SparklineProps {
  /** What the line is, e.g. "Clicks". */
  label: string
  points: InsightPoint[]
  unit?: InsightUnit
  /** Lower is better (search position): flips the line so up is good. */
  invert?: boolean
}

/** Vertical domain so a quiet series does not look violent. */
export function sparkDomain(known: readonly number[], invert: boolean): { low: number; high: number } {
  let low = Math.min(...known)
  let high = Math.max(...known)
  if (high === low) {
    if (!invert && low >= 0) {
      return { low: 0, high: high === 0 ? 1 : high * 1.12 }
    }
    const pad = Math.abs(high) * 0.18 || 1
    return { low: low - pad, high: high + pad }
  }
  const span = high - low
  const magnitude = Math.max(Math.abs(high), Math.abs(low), 1)
  if (!invert && low >= 0 && span / magnitude < 0.2) {
    return { low: 0, high: high * 1.08 }
  }
  const pad = span * 0.16
  if (invert) return { low: low - pad, high: high + pad }
  return {
    low: low >= 0 ? Math.max(0, low - pad) : low - pad,
    high: high + pad,
  }
}

function downsample<T>(items: readonly T[], max = 36): T[] {
  if (items.length <= max) return [...items]
  const indexes = new Set<number>()
  const last = items.length - 1
  for (let i = 0; i < max; i += 1) {
    indexes.add(i === max - 1 ? last : Math.round((i * last) / (max - 1)))
  }
  return [...indexes]
    .sort((a, b) => a - b)
    .map((index) => items[index])
    .filter((item): item is T => item !== undefined)
}

export function Sparkline({ label, points, unit = 'count', invert = false }: SparklineProps) {
  const fillId = useId().replace(/:/g, '')
  const shape = useMemo(() => {
    const sampled = downsample(points)
    const values = sampled.map((p) => (p.value === null || !Number.isFinite(p.value) ? null : p.value))
    const known = values.filter((v): v is number => v !== null)
    if (!known.length) return null
    const { low, high } = sparkDomain(known, invert)
    const span = high - low || 1
    const x = (index: number) =>
      PAD.x + (values.length === 1 ? (W - PAD.x * 2) / 2 : (index / (values.length - 1)) * (W - PAD.x * 2))
    const y = (value: number) => {
      const share = (value - low) / span
      return PAD.y + (invert ? share : 1 - share) * (H - PAD.y * 2)
    }
    const firstKnown = known[0] ?? 0
    const lastKnown = known[known.length - 1] ?? 0
    const lastIndex = values.reduce<number>((found, value, index) => (value !== null ? index : found), -1)
    const lastValue = lastIndex >= 0 ? values[lastIndex] : null
    return {
      runs: segments(values, x, y),
      baseline: y(invert ? high : low < 0 ? 0 : low),
      end:
        lastIndex >= 0 && lastValue !== null && lastValue !== undefined
          ? { x: x(lastIndex), y: y(lastValue) }
          : null,
      low: Math.min(...known),
      high: Math.max(...known),
      firstKnown,
      lastKnown,
      gaps: points.filter((p) => p.value === null || !Number.isFinite(p.value)).length,
    }
  }, [points, invert])

  if (!shape) return null

  const summary =
    `${label} over this period: ${formatValue(shape.firstKnown, unit)} at the start, ${formatValue(shape.lastKnown, unit)} at the end, ` +
    `ranging from ${formatValue(shape.low, unit)} to ${formatValue(shape.high, unit)}` +
    (shape.gaps ? `, with ${shape.gaps} day${shape.gaps === 1 ? '' : 's'} of no data.` : '.')

  return (
    <div className="ins-spark-wrap">
      <svg className="ins-spark" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary} focusable="false">
        <defs>
          <linearGradient id={fillId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        <line x1={PAD.x} x2={W - PAD.x} y1={shape.baseline} y2={shape.baseline} className="ins-spark-base" />
        {shape.runs.map((run, index) =>
          run.length > 1 ? (
            <polygon
              key={`fill-${index}`}
              points={areaAttr(run, shape.baseline)}
              fill={`url(#${fillId})`}
              className="ins-spark-fill"
            />
          ) : null,
        )}
        {shape.runs.map((run, index) =>
          run.length === 1 ? (
            <circle key={index} cx={run[0]?.x} cy={run[0]?.y} r={2.4} className="ins-dot" />
          ) : (
            <polyline key={index} points={pointsAttr(run)} className="ins-line" />
          ),
        )}
        {shape.end ? <circle cx={shape.end.x} cy={shape.end.y} r={3.2} className="ins-spark-end" /> : null}
      </svg>
      <div className="ins-spark-ends" aria-hidden="true">
        <span>{formatValue(shape.firstKnown, unit)}</span>
        <span>{formatValue(shape.lastKnown, unit)}</span>
      </div>
    </div>
  )
}
