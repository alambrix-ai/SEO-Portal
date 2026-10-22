/**
 * A word-sized trend line for a tile.
 *
 * It sits next to a number that already says the value, so its job is the
 * shape. The `aria-label` still says the shape in words (start, end, high,
 * gaps). Missing points are gaps, never zeros, and a series with nothing in
 * it renders nothing rather than a flat line.
 */
import { useMemo } from 'react'

import { formatValue, type InsightPoint, type InsightUnit } from '@/api/insights'
import { pointsAttr, segments } from './LineChart'
import '@/styles/insights.css'

const W = 100
const H = 28
const PAD = 2

export interface SparklineProps {
  /** What the line is, e.g. "Clicks". */
  label: string
  points: InsightPoint[]
  unit?: InsightUnit
  /** Lower is better (search position): flips the line so up is good. */
  invert?: boolean
}

export function Sparkline({ label, points, unit = 'count', invert = false }: SparklineProps) {
  const shape = useMemo(() => {
    const values = points.map((p) => (p.value === null || !Number.isFinite(p.value) ? null : p.value))
    const known = values.filter((v): v is number => v !== null)
    if (!known.length) return null
    const low = Math.min(...known)
    const high = Math.max(...known)
    const span = high - low || 1
    const x = (index: number) => PAD + (values.length === 1 ? (W - PAD * 2) / 2 : (index / (values.length - 1)) * (W - PAD * 2))
    const y = (value: number) => {
      const share = high === low ? 0.5 : (value - low) / span
      return PAD + (invert ? share : 1 - share) * (H - PAD * 2)
    }
    const firstKnown = known[0] ?? 0
    const lastKnown = known[known.length - 1] ?? 0
    return {
      runs: segments(values, x, y),
      low,
      high,
      firstKnown,
      lastKnown,
      gaps: values.length - known.length,
    }
  }, [points, invert])

  if (!shape) return null

  const summary =
    `${label} trend: ${formatValue(shape.firstKnown, unit)} at the start, ${formatValue(shape.lastKnown, unit)} at the end, ` +
    `ranging from ${formatValue(shape.low, unit)} to ${formatValue(shape.high, unit)}` +
    (shape.gaps ? `, with ${shape.gaps} day${shape.gaps === 1 ? '' : 's'} of no data.` : '.')

  return (
    <svg className="ins-spark" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary} focusable="false">
      {shape.runs.map((run, index) =>
        run.length === 1 ? (
          <circle key={index} cx={run[0]?.x} cy={run[0]?.y} r={1.6} className="ins-dot" />
        ) : (
          <polyline key={index} points={pointsAttr(run)} className="ins-line" />
        ),
      )}
    </svg>
  )
}
