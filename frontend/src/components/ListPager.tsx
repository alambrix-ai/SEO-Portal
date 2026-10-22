/**
 * Page-size and page controls for long lists.
 *
 * Presets are 20 / 40 / 50 / 80 / 100, plus a custom count. Numbered pages
 * are used when the full list is already in memory; Previous / Next is used
 * when the server pages with a cursor.
 */
import { useEffect, useState } from 'react'

import {
  PAGE_SIZE_PRESETS,
  clampPageSize,
  isPageSizePreset,
} from '@/hooks/usePagedList'

function pageWindow(current: number, count: number): Array<number | 'gap'> {
  if (count <= 7) return Array.from({ length: count }, (_, i) => i + 1)
  const keep = new Set([1, 2, count - 1, count, current - 1, current, current + 1])
  const nums = [...keep].filter((n) => n >= 1 && n <= count).sort((a, b) => a - b)
  const out: Array<number | 'gap'> = []
  for (const n of nums) {
    const prev = out[out.length - 1]
    if (typeof prev === 'number' && n - prev > 1) out.push('gap')
    out.push(n)
  }
  return out
}

function scrollListTop() {
  const root = document.querySelector('.page')
  if (root instanceof HTMLElement) root.scrollTo({ top: 0 })
}

export function ListPager({
  total,
  from,
  to,
  pageSize,
  onPageSize,
  page,
  pageCount,
  onPage,
  hasPrev,
  hasNext,
  onPrev,
  onNext,
  disabled = false,
  maxPageSize = 200,
  label = 'List pages',
}: {
  total?: number | null
  from: number
  to: number
  pageSize: number
  onPageSize: (value: number) => void
  page?: number
  pageCount?: number
  onPage?: (page: number) => void
  hasPrev?: boolean
  hasNext?: boolean
  onPrev?: () => void
  onNext?: () => void
  disabled?: boolean
  maxPageSize?: number
  label?: string
}) {
  const knownTotal = typeof total === 'number'
  const numbered = typeof page === 'number' && typeof pageCount === 'number' && Boolean(onPage)
  const cursor = Boolean(onPrev || onNext)
  const showNav = numbered ? (pageCount ?? 0) > 1 : cursor && (hasPrev || hasNext)
  const [draft, setDraft] = useState(String(pageSize))
  const [showCustom, setShowCustom] = useState(() => !isPageSizePreset(pageSize))

  useEffect(() => {
    setDraft(String(pageSize))
    setShowCustom(!isPageSizePreset(pageSize))
  }, [pageSize])

  if (knownTotal && total === 0) return null

  const commitCustom = () => {
    const next = clampPageSize(Number.parseInt(draft, 10), maxPageSize)
    setDraft(String(next))
    onPageSize(next)
  }

  const go = (next: number) => {
    onPage?.(next)
    scrollListTop()
  }

  const status =
    knownTotal && from > 0
      ? `Showing ${from}–${to} of ${total}`
      : from > 0
        ? `Showing ${from}–${to}`
        : 'No rows on this page'

  return (
    <nav className="list-pager" aria-label={label}>
      <p className="list-pager-status">{status}</p>
      <div className="list-pager-controls">
        <label className="list-pager-size">
          <span>Per page</span>
          <select
            className="input"
            aria-label="Rows per page"
            disabled={disabled}
            value={showCustom ? 'custom' : String(pageSize)}
            onChange={(event) => {
              const value = event.target.value
              if (value === 'custom') {
                setShowCustom(true)
                setDraft(String(pageSize))
                return
              }
              setShowCustom(false)
              onPageSize(Number(value))
            }}
          >
            {PAGE_SIZE_PRESETS.filter((size) => size <= maxPageSize).map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
            <option value="custom">Custom</option>
          </select>
        </label>
        {showCustom ? (
          <label className="list-pager-size">
            <span className="sr-only">Custom rows per page</span>
            <input
              className="input list-pager-custom"
              type="number"
              min={1}
              max={maxPageSize}
              step={1}
              inputMode="numeric"
              disabled={disabled}
              value={draft}
              aria-label={`Custom rows per page, 1 to ${maxPageSize}`}
              onChange={(event) => setDraft(event.target.value)}
              onBlur={commitCustom}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  commitCustom()
                }
              }}
            />
          </label>
        ) : null}

        {showNav && typeof page === 'number' && typeof pageCount === 'number' && onPage ? (
          <div className="list-pager-nav">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={disabled || page <= 1}
              onClick={() => go(page - 1)}
            >
              Previous
            </button>
            <div className="list-pager-pages">
              {pageWindow(page, pageCount).map((item, index) =>
                item === 'gap' ? (
                  <span key={`gap-${index}`} className="list-pager-gap" aria-hidden="true">
                    …
                  </span>
                ) : (
                  <button
                    key={item}
                    type="button"
                    className={`list-pager-page${item === page ? ' is-current' : ''}`}
                    aria-current={item === page ? 'page' : undefined}
                    disabled={disabled || item === page}
                    onClick={() => go(item)}
                  >
                    {item}
                  </button>
                ),
              )}
            </div>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={disabled || page >= pageCount}
              onClick={() => go(page + 1)}
            >
              Next
            </button>
          </div>
        ) : null}

        {showNav && cursor && !numbered ? (
          <div className="list-pager-nav">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={disabled || !hasPrev}
              onClick={() => {
                onPrev?.()
                scrollListTop()
              }}
            >
              Previous
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={disabled || !hasNext}
              onClick={() => {
                onNext?.()
                scrollListTop()
              }}
            >
              Next
            </button>
          </div>
        ) : null}
      </div>
    </nav>
  )
}
