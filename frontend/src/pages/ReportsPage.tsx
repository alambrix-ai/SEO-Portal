/**
 * Reports - the numbers, grouped, and the trend behind them.
 *
 * Every figure is an aggregate over the same daily metric rows, so the tiles
 * and the chart cannot disagree with each other or with the audit log.
 *
 * The previous version was six tiles in one undifferentiated row, a bare
 * `<h3>`, and a bare polyline: no scale, no dates, no gridlines, and - when
 * a workspace had no data yet - a flat line along the bottom that looked
 * exactly like a measurement of zero rather than an absence of measurement.
 * That last one is the reason this was worth rewriting rather than restyling:
 * a chart that cannot tell "no data" from "zero" is a chart that misreports.
 */
import { useMemo, useState } from 'react'

import { api } from '@/api/client'
import { ErrorState, Loading, SectionHeading, Segmented, StatTile } from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { moneyExact } from '@/lib/money'

type Range = '7d' | '30d' | '90d'

const RANGE_LABEL: Record<Range, string> = {
  '7d': 'the last 7 days',
  '30d': 'the last 30 days',
  '90d': 'the last 90 days',
}

/** Chart geometry. Wide and shallow, the shape a trend is read in. */
const W = 720
const H = 180
const PAD = { top: 14, right: 8, bottom: 8, left: 8 }

export function ReportsPage() {
  const [range, setRange] = useState<Range>('30d')
  const { data, loading, error, reload } = useResource(
    (signal) => api.reports(range, signal),
    [range],
    `reports:${range}`,
  )

  const chart = useMemo(() => {
    const series = data?.trend ?? []
    // A workspace with no metrics yet, or a period where nothing was
    // recorded, has no trend to draw. Saying so beats a flat line that reads
    // as a measured zero.
    const peak = Math.max(...series, 0)
    if (series.length < 2 || peak === 0) return null

    const innerW = W - PAD.left - PAD.right
    const innerH = H - PAD.top - PAD.bottom
    const x = (index: number) => PAD.left + (index / (series.length - 1)) * innerW
    const y = (value: number) => PAD.top + innerH - (value / peak) * innerH

    const line = series.map((value, index) => `${x(index)},${y(value)}`).join(' ')
    return {
      peak,
      line,
      // The same points closed along the baseline, for the fill.
      area: `${PAD.left},${PAD.top + innerH} ${line} ${PAD.left + innerW},${
        PAD.top + innerH
      }`,
      // Quarter gridlines, so the eye can estimate a value off the shape.
      grid: [0.25, 0.5, 0.75].map((fraction) => PAD.top + innerH * fraction),
      // Indexed access is `number | undefined` under noUncheckedIndexedAccess
      // and the length guard above is not something the compiler can carry
      // this far, so the fallback is explicit rather than asserted away.
      last: series[series.length - 1] ?? 0,
      first: series[0] ?? 0,
      days: series.length,
    }
  }, [data])

  return (
    <>
      <div className="reports-head">
        <div>
          <h2 className="reports-title">Performance</h2>
          <p className="reports-period">
            Everything on this page covers {RANGE_LABEL[range]}.
          </p>
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

      {loading && !data ? <Loading label="Loading reports…" /> : null}
      {error && !data ? <ErrorState message={error} onRetry={reload} /> : null}

      {data ? (
        <>
          {/* Grouped, because six tiles in a row is a row of six numbers.
              Three headings turn it into three answers: how is search going,
              is authority growing, what is paid costing. */}
          <SectionHeading title="Search & answers" />
          <div className="grid-tiles-sm">
            <StatTile kicker="Organic sessions" value={data.organic_sessions} size={26} />
            <StatTile kicker="AEO citations" value={data.aeo_citations} size={26} />
            <StatTile kicker="Pages optimised" value={data.pages_optimised} size={26} />
          </div>

          <SectionHeading title="Authority" spaced />
          <div className="grid-tiles-sm">
            <StatTile kicker="Backlinks won" value={data.backlinks_won} size={26} />
            <StatTile
              kicker="Spam referrers blocked"
              value={data.referral_spam_blocked.toLocaleString('en-IN')}
              size={26}
            />
          </div>

          <SectionHeading title="Paid & protection" spaced />
          <div className="grid-tiles-sm">
            <StatTile kicker="Ad spend" value={data.ad_spend} size={26} />
            <StatTile kicker="Blended CAC" value={moneyExact(data.blended_cac)} size={26} />
            <StatTile kicker="Fraudulent clicks blocked" value={data.fraud_blocked} size={26} />
          </div>

          <SectionHeading
            title="Organic sessions"
            description={
              chart
                ? `${chart.days} days · peak ${chart.peak.toLocaleString('en-IN')}`
                : undefined
            }
            spaced
          />

          {chart ? (
            <div className="card trend-card">
              <svg
                className="trend"
                viewBox={`0 0 ${W} ${H}`}
                preserveAspectRatio="none"
                role="img"
                aria-label={`Organic sessions over ${RANGE_LABEL[range]}, peaking at ${chart.peak}`}
              >
                {chart.grid.map((y) => (
                  <line key={y} x1={PAD.left} x2={W - PAD.right} y1={y} y2={y} className="trend-grid" />
                ))}
                <polygon points={chart.area} className="trend-area" />
                <polyline points={chart.line} className="trend-line" />
              </svg>
              {/* The scale, in words rather than as axis furniture: three
                  numbers say more here than a labelled axis on a sparkline. */}
              <div className="trend-scale">
                <span>Peak {chart.peak.toLocaleString('en-IN')}</span>
                <span className="muted">
                  {chart.first.toLocaleString('en-IN')} at the start ·{' '}
                  {chart.last.toLocaleString('en-IN')} now
                </span>
              </div>
            </div>
          ) : (
            <div className="panel-empty">
              <p>No organic sessions recorded in this period.</p>
              <p className="small muted">
                This is an absence of data rather than a measurement of zero  - 
                the trend appears once an analytics connector has reported a
                day of traffic.
              </p>
            </div>
          )}
        </>
      ) : null}
    </>
  )
}
