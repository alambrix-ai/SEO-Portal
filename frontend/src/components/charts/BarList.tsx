/**
 * A ranked list of labelled bars (per channel, platform, kind of asset...).
 *
 * It is a real list with the value written beside every bar, so it reads
 * fully without the bars; the bars are decoration and hidden from screen
 * readers. A missing value shows its reason in words ("Not reported by
 * Instagram") and gets no bar, because a zero-width bar would read as zero.
 */
import { formatValue, type InsightBreakdownItem } from '@/api/insights'
import '@/styles/insights.css'

/** Longest list drawn; a breakdown past this is a table's job. */
const MAX_ITEMS = 20

export interface BarListProps {
  /** Names the list for screen readers, e.g. "Spend by channel". */
  label: string
  items: InsightBreakdownItem[]
  /** Text for an item without a value and without its own note. */
  missingText?: string
  /** Shown when the list is empty. */
  emptyText?: string
  /** Sort by value, highest first (missing values last). Defaults to true. */
  sorted?: boolean
  /** Treat values as estimates ("est.") when they are currency. */
  estimate?: boolean
}

export function BarList({
  label,
  items,
  missingText = 'No data',
  emptyText = 'Nothing to show for this period.',
  sorted = true,
  estimate = false,
}: BarListProps) {
  if (!items.length) {
    return (
      <div className="ins-chart-empty" role="note">
        <p>{emptyText}</p>
      </div>
    )
  }

  const known = (value: number | null): value is number => value !== null && Number.isFinite(value)
  const rows = sorted
    ? [...items].sort((a, b) => {
        if (!known(a.value)) return known(b.value) ? 1 : 0
        if (!known(b.value)) return -1
        return b.value - a.value
      })
    : items
  const shown = rows.slice(0, MAX_ITEMS)
  const peak = Math.max(0, ...shown.map((item) => (known(item.value) ? Math.abs(item.value) : 0)))

  return (
    <ul className="ins-bars" aria-label={label}>
      {shown.map((item) => {
        const has = known(item.value)
        const share = has && peak > 0 ? Math.abs(item.value as number) / peak : 0
        return (
          <li key={item.key} className="ins-bar-row">
            <span className="ins-bar-label">{item.label}</span>
            <span className={has ? 'ins-bar-value' : 'ins-bar-value ins-missing'}>
              {has
                ? formatValue(item.value, item.unit, { estimate })
                : item.note?.trim() || missingText}
            </span>
            <span className="ins-bar-track" aria-hidden="true">
              {has ? <span className="ins-bar-fill" style={{ width: `${share > 0 ? Math.max(share * 100, 1) : 0}%` }} /> : null}
            </span>
          </li>
        )
      })}
      {rows.length > shown.length ? (
        <li className="ins-bar-more muted small">and {rows.length - shown.length} more</li>
      ) : null}
    </ul>
  )
}
