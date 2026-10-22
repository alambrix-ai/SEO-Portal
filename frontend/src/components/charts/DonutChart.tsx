/**
 * A labelled donut for a mix (spend by channel, content by kind, …).
 *
 * Only measured values become slices. A missing figure is a legend row in
 * words, never a 0° wedge. One full share is drawn as a ring, not a 360°
 * arc (SVG treats a full-circle arc as empty).
 */
import { useMemo } from 'react'

import { formatValue, type InsightBreakdownItem, type InsightUnit } from '@/api/insights'
import '@/styles/insights.css'

const CX = 80
const CY = 80
const OUTER = 68
const INNER = 40
const MAX_SLICES = 6

export interface DonutChartProps {
  title: string
  items: InsightBreakdownItem[]
  emptyText?: string
  estimate?: boolean
}

function polar(radius: number, angle: number): { x: number; y: number } {
  const rad = ((angle - 90) * Math.PI) / 180
  return { x: CX + radius * Math.cos(rad), y: CY + radius * Math.sin(rad) }
}

function wedge(start: number, end: number): string {
  const large = end - start > 180 ? 1 : 0
  const outerStart = polar(OUTER, start)
  const outerEnd = polar(OUTER, end)
  const innerStart = polar(INNER, start)
  const innerEnd = polar(INNER, end)
  return [
    `M ${outerStart.x.toFixed(2)} ${outerStart.y.toFixed(2)}`,
    `A ${OUTER} ${OUTER} 0 ${large} 1 ${outerEnd.x.toFixed(2)} ${outerEnd.y.toFixed(2)}`,
    `L ${innerEnd.x.toFixed(2)} ${innerEnd.y.toFixed(2)}`,
    `A ${INNER} ${INNER} 0 ${large} 0 ${innerStart.x.toFixed(2)} ${innerStart.y.toFixed(2)}`,
    'Z',
  ].join(' ')
}

export function DonutChart({
  title,
  items,
  emptyText = 'Nothing was recorded in this period. That is missing data, not a zero.',
  estimate = false,
}: DonutChartProps) {
  const chart = useMemo(() => {
    const known = (value: number | null): value is number =>
      value !== null && Number.isFinite(value) && value > 0
    const slices = items.filter((item) => known(item.value)).slice(0, MAX_SLICES)
    const leftover = items.filter((item) => !known(item.value))
    const total = slices.reduce((sum, item) => sum + (item.value as number), 0)
    if (!slices.length || total <= 0) return null
    let cursor = 0
    const drawn = slices.map((item, index) => {
      const share = (item.value as number) / total
      const start = cursor * 360
      cursor += share
      const end = index === slices.length - 1 ? 360 : cursor * 360
      return { item, share, start, end, full: slices.length === 1 }
    })
    return { drawn, leftover, total, unit: (slices[0]?.unit ?? 'count') as InsightUnit }
  }, [items])

  if (!chart) {
    return (
      <div className="ins-chart-empty" role="note">
        <p>{emptyText}</p>
      </div>
    )
  }

  const summary = `${title}: ${chart.drawn
    .map((slice) => `${slice.item.label} ${formatValue(slice.item.value, slice.item.unit, { estimate })}`)
    .join(', ')}.`

  return (
    <figure className="ins-donut">
      <svg className="ins-donut-svg" viewBox="0 0 160 160" role="img" aria-label={summary}>
        {chart.drawn.map((slice, index) =>
          slice.full ? (
            <g key={slice.item.key} className={`ins-donut-slice ins-series-${index}`}>
              <circle cx={CX} cy={CY} r={(OUTER + INNER) / 2} fill="none" stroke="currentColor" strokeWidth={OUTER - INNER} />
            </g>
          ) : (
            <path
              key={slice.item.key}
              className={`ins-donut-slice ins-series-${index}`}
              d={wedge(slice.start, slice.end)}
            />
          ),
        )}
        <circle cx={CX} cy={CY} r={INNER - 0.5} className="ins-donut-hole" />
        <text x={CX} y={CY - 6} className="ins-donut-total" textAnchor="middle">
          {formatValue(chart.total, chart.unit, { estimate })}
        </text>
        <text x={CX} y={CY + 12} className="ins-donut-total-label" textAnchor="middle">
          Total
        </text>
      </svg>
      <figcaption className="ins-donut-legend">
        {chart.drawn.map((slice, index) => (
          <div key={slice.item.key} className={`ins-donut-legend-item ins-series-${index}`}>
            <span className="ins-donut-swatch" aria-hidden="true" />
            <span className="ins-donut-legend-copy">
              <span className="ins-donut-legend-name">{slice.item.label}</span>
              <span className="ins-donut-legend-value">
                {formatValue(slice.item.value, slice.item.unit, { estimate })} · {Math.round(slice.share * 100)}%
              </span>
            </span>
          </div>
        ))}
        {chart.leftover.map((item) => (
          <div key={item.key} className="ins-donut-legend-item is-missing">
            <span className="ins-donut-swatch" aria-hidden="true" />
            <span className="ins-donut-legend-copy">
              <span className="ins-donut-legend-name">{item.label}</span>
              <span className="ins-donut-legend-value ins-missing">{item.note?.trim() || 'No data'}</span>
            </span>
          </div>
        ))}
      </figcaption>
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th scope="col">Part</th>
            <th scope="col">Value</th>
            <th scope="col">Share</th>
          </tr>
        </thead>
        <tbody>
          {chart.drawn.map((slice) => (
            <tr key={slice.item.key}>
              <th scope="row">{slice.item.label}</th>
              <td>{formatValue(slice.item.value, slice.item.unit, { estimate })}</td>
              <td>{Math.round(slice.share * 100)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
