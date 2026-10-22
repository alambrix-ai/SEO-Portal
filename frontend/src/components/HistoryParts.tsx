/**
 * Small pieces every history view shares: the ▲/▼ change, the sparkline and
 * the before → after tile. Site analysis uses them too.
 */

/**
 * A change since the previous run. Colour follows whether the change is good,
 * so a fall in open issues is green: pass `lowerIsBetter` for those numbers.
 */
export function Change({
  value,
  long = false,
  lowerIsBetter = false,
  first = 'First analysis of this site',
  unit = '',
}: {
  value: number | null | undefined
  long?: boolean
  lowerIsBetter?: boolean
  /** What `long` says when there is nothing to compare with. */
  first?: string
  unit?: string
}) {
  if (value === null || value === undefined) return <span className="muted">{long ? first : '–'}</span>
  if (value === 0) return <span className="muted">{long ? 'No change since the last run' : '0'}</span>
  const good = lowerIsBetter ? value < 0 : value > 0
  return (
    <span className={good ? 'sa-up' : 'sa-down'}>
      {value > 0 ? '▲' : '▼'} {Math.abs(value)}
      {unit}
      {long ? ' since the last run' : ''}
    </span>
  )
}

/**
 * A sparkline. Defaults suit a 0-100 score; `invert` puts small numbers at
 * the top (a search position, where 1 is best). Missing points leave a gap.
 */
export function Trend({
  points,
  min = 0,
  max = 100,
  invert = false,
  label = 'Score trend',
  width = 180,
  height = 44,
}: {
  points: (number | null)[]
  min?: number
  max?: number
  invert?: boolean
  label?: string
  width?: number
  height?: number
}) {
  const span = max - min || 1
  const step = points.length > 1 ? width / (points.length - 1) : width
  const y = (p: number) => {
    const share = Math.max(0, Math.min(1, (p - min) / span))
    return invert ? share * height : height - share * height
  }
  let path = ''
  let pen = false
  points.forEach((p, i) => {
    if (p === null) {
      pen = false
      return
    }
    path += `${pen ? 'L' : 'M'}${(i * step).toFixed(1)},${y(p).toFixed(1)} `
    pen = true
  })
  return (
    <svg
      className="sa-trend"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${label}: ${points.map((p) => (p === null ? 'none' : p)).join(', ')}`}
    >
      <path d={path.trim()} />
      {points.map((p, i) => (p === null ? null : <circle key={i} cx={i * step} cy={y(p)} r={2.5} />))}
    </svg>
  )
}

/** "before → after" with the change, for the "What changed" tiles. */
export function Delta({
  label,
  value,
  lowerIsBetter = false,
  unit = '',
}: {
  label: string
  value: { before: number | null | undefined; after: number | null | undefined; change?: number | null }
  lowerIsBetter?: boolean
  unit?: string
}) {
  const change =
    value.change !== undefined
      ? value.change
      : value.before !== null && value.before !== undefined && value.after !== null && value.after !== undefined
        ? Math.round((value.after - value.before) * 10) / 10
        : null
  return (
    <div className="sa-delta">
      <span className="small muted">{label}</span>
      <strong>
        {value.before ?? '–'}
        {value.before !== null && value.before !== undefined ? unit : ''} → {value.after ?? '–'}
        {value.after !== null && value.after !== undefined ? unit : ''}
      </strong>
      <Change value={change} lowerIsBetter={lowerIsBetter} unit={unit} />
    </div>
  )
}
