/**
 * A hand-drawn SVG line chart for insight series.
 *
 * Extends the ReportsPage trend: no chart library (bundle weight and a CSP
 * review for no gain), and the same rule that a chart must never draw "no
 * data" as zero. A `null` point breaks the line - the gap is the honest
 * picture - and a lone point between two gaps is drawn as a dot so it is not
 * lost. A series with nothing in it shows a sentence, not a flat line.
 *
 * Accessibility: the SVG has an `aria-label` summary, and a visually hidden
 * table carries every value (missing ones as "No data") for screen readers.
 * Several series differ by dash pattern and marker as well as colour, and
 * the legend shows the same pattern.
 */
import { useMemo } from 'react'

import { formatValue, NO_DATA, type InsightSeries, type InsightUnit } from '@/api/insights'
import '@/styles/insights.css'

/** Chart geometry, in viewBox units. Wide and shallow, as a trend is read. */
const W = 720
const PAD = { top: 12, right: 12, bottom: 24, left: 52 }
/** More series than this stops being readable; the rest are left out. */
const MAX_SERIES = 4
/** Dash patterns per series, so colour is not the only difference. */
const DASHES = ['', '6 4', '2 3', '10 3 2 3'] as const

export interface ChartPoint {
  x: number
  y: number
}

/**
 * Split values into runs of consecutive non-null points. Each run becomes
 * its own line; a missing value ends the run, so it is drawn as a gap.
 */
export function segments(
  values: readonly (number | null)[],
  x: (index: number) => number,
  y: (value: number) => number,
): ChartPoint[][] {
  const runs: ChartPoint[][] = []
  let current: ChartPoint[] = []
  values.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) {
      if (current.length) runs.push(current)
      current = []
      return
    }
    current.push({ x: x(index), y: y(value) })
  })
  if (current.length) runs.push(current)
  return runs
}

export function pointsAttr(run: readonly ChartPoint[]): string {
  return run.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')
}

/** "3 Oct" from `YYYY-MM-DD`; anything else (an audit version label) as is. */
export function shortDate(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  const date = new Date(`${value}T00:00:00`)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

export interface LineChartProps {
  /** Names the chart in the summary and table caption, e.g. "Search clicks". */
  title: string
  series: InsightSeries[]
  /** Overrides the series' own unit for axis and table values. */
  unit?: InsightUnit
  /** Lower is better (search position): the axis is flipped so up is good. */
  invert?: boolean
  height?: number
  /** Shown when there is nothing to draw. */
  emptyText?: string
}

export function LineChart({
  title,
  series,
  unit,
  invert = false,
  height = 200,
  emptyText = 'Nothing was recorded in this period. That is missing data, not a zero.',
}: LineChartProps) {
  const shown = series.slice(0, MAX_SERIES)
  const valueUnit: InsightUnit = unit ?? shown[0]?.unit ?? 'count'

  const chart = useMemo(() => {
    const shown = series.slice(0, MAX_SERIES)
    // Line the series up on the union of their dates, so a date one series
    // lacks is a gap in that series rather than a shifted point.
    const dates = Array.from(new Set(shown.flatMap((s) => s.points.map((p) => p.date)))).sort()
    const rows = shown.map((s) => {
      const byDate = new Map(s.points.map((p) => [p.date, p.value]))
      return dates.map((date) => {
        const value = byDate.get(date)
        return value === undefined || value === null || !Number.isFinite(value) ? null : value
      })
    })
    const known = rows.flat().filter((v): v is number => v !== null)
    if (!known.length) return null

    let low = Math.min(0, ...known)
    let high = Math.max(...known)
    if (invert) low = Math.min(...known)
    if (high === low) high = low + 1

    const innerW = W - PAD.left - PAD.right
    const innerH = height - PAD.top - PAD.bottom
    const x = (index: number) =>
      PAD.left + (dates.length === 1 ? innerW / 2 : (index / (dates.length - 1)) * innerW)
    const y = (value: number) => {
      const share = (value - low) / (high - low)
      return PAD.top + (invert ? share : 1 - share) * innerH
    }
    const ticks = [0, 0.5, 1].map((fraction) => {
      const value = low + (high - low) * fraction
      return { value, y: y(value) }
    })
    return {
      dates,
      rows,
      ticks,
      lines: rows.map((values) => segments(values, x, y)),
      min: Math.min(...known),
      max: Math.max(...known),
      gaps: rows.reduce((count, values) => count + values.filter((v) => v === null).length, 0),
    }
  }, [series, invert, height])

  if (!chart) {
    return (
      <div className="ins-chart-empty" role="note">
        <p>{emptyText}</p>
      </div>
    )
  }

  const first = chart.dates[0] ?? ''
  const last = chart.dates[chart.dates.length - 1] ?? ''
  const summary =
    `${title}${shown.length > 1 ? ` (${shown.map((s) => s.label).join(', ')})` : ''}, from ${shortDate(first)} to ${shortDate(last)}. ` +
    `Lowest ${formatValue(chart.min, valueUnit)}, highest ${formatValue(chart.max, valueUnit)}.` +
    (chart.gaps ? ` ${chart.gaps} point${chart.gaps === 1 ? '' : 's'} with no data.` : '') +
    ' Full values are in the table that follows.'

  return (
    <figure className="ins-chart">
      <svg
        className="ins-chart-svg"
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={summary}
      >
        {chart.ticks.map((tick) => (
          <g key={tick.y}>
            <line x1={PAD.left} x2={W - PAD.right} y1={tick.y} y2={tick.y} className="ins-grid" />
            <text x={PAD.left - 6} y={tick.y} className="ins-axis" textAnchor="end" dominantBaseline="middle">
              {formatValue(tick.value, valueUnit)}
            </text>
          </g>
        ))}
        <text x={PAD.left} y={height - 6} className="ins-axis" textAnchor="start">
          {shortDate(first)}
        </text>
        {last !== first ? (
          <text x={W - PAD.right} y={height - 6} className="ins-axis" textAnchor="end">
            {shortDate(last)}
          </text>
        ) : null}
        {chart.lines.map((runs, seriesIndex) => (
          <g key={shown[seriesIndex]?.key ?? seriesIndex} className={`ins-series ins-series-${seriesIndex}`}>
            {runs.map((run, runIndex) =>
              run.length === 1 ? (
                <circle key={runIndex} cx={run[0]?.x} cy={run[0]?.y} r={3.5} className="ins-dot" />
              ) : (
                <polyline
                  key={runIndex}
                  points={pointsAttr(run)}
                  className="ins-line"
                  strokeDasharray={DASHES[seriesIndex] || undefined}
                />
              ),
            )}
          </g>
        ))}
      </svg>

      {shown.length > 1 ? (
        <figcaption className="ins-legend">
          {shown.map((s, index) => (
            <span key={s.key} className={`ins-legend-item ins-series-${index}`}>
              <svg width="24" height="8" aria-hidden="true" focusable="false">
                <line
                  x1="0"
                  x2="24"
                  y1="4"
                  y2="4"
                  className="ins-line"
                  strokeDasharray={DASHES[index] || undefined}
                />
              </svg>
              {s.label}
            </span>
          ))}
        </figcaption>
      ) : null}

      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            {shown.map((s) => (
              <th scope="col" key={s.key}>
                {s.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {chart.dates.map((date, row) => (
            <tr key={date}>
              <th scope="row">{shortDate(date)}</th>
              {chart.rows.map((values, col) => {
                const value = values[row] ?? null
                return <td key={shown[col]?.key ?? col}>{value === null ? NO_DATA : formatValue(value, valueUnit)}</td>
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
