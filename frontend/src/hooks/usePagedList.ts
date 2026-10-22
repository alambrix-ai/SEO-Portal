/**
 * Client-side paging for screens that already load a full list.
 *
 * Page size is remembered per list (`localStorage`) so a marketer who prefers
 * 50 rows does not have to set it again on every visit. Custom sizes are
 * clamped so a typed "9999" cannot freeze the tab.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

export const PAGE_SIZE_PRESETS = [20, 40, 50, 80, 100] as const
export const DEFAULT_PAGE_SIZE = 20
export const MAX_PAGE_SIZE = 200

const STORAGE_PREFIX = 'synaprank.pageSize.'

export function clampPageSize(value: number, max = MAX_PAGE_SIZE): number {
  if (!Number.isFinite(value)) return DEFAULT_PAGE_SIZE
  return Math.min(max, Math.max(1, Math.floor(value)))
}

export function isPageSizePreset(value: number): boolean {
  return (PAGE_SIZE_PRESETS as readonly number[]).includes(value)
}

function readStored(key: string, max: number): number {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + key)
    if (!raw) return DEFAULT_PAGE_SIZE
    return clampPageSize(Number.parseInt(raw, 10), max)
  } catch {
    return DEFAULT_PAGE_SIZE
  }
}

function writeStored(key: string, value: number) {
  try {
    localStorage.setItem(STORAGE_PREFIX + key, String(value))
  } catch {
    /* private mode / quota */
  }
}

export function usePageSize(storageKey: string, max = MAX_PAGE_SIZE) {
  const [pageSize, setPageSizeState] = useState(() => readStored(storageKey, max))
  const setPageSize = useCallback(
    (value: number) => {
      const next = clampPageSize(value, max)
      setPageSizeState(next)
      writeStored(storageKey, next)
    },
    [storageKey, max],
  )
  return [pageSize, setPageSize] as const
}

export function usePagedList<T>(
  items: readonly T[],
  options: {
    storageKey: string
    resetKey?: string | number
    maxPageSize?: number
  },
) {
  const max = options.maxPageSize ?? MAX_PAGE_SIZE
  const [pageSize, setPageSize] = usePageSize(options.storageKey, max)
  const [page, setPage] = useState(1)

  useEffect(() => {
    setPage(1)
  }, [options.resetKey, pageSize])

  return useMemo(() => {
    const total = items.length
    const pageCount = Math.max(1, Math.ceil(total / pageSize) || 1)
    const safePage = Math.min(Math.max(1, page), pageCount)
    const start = total === 0 ? 0 : (safePage - 1) * pageSize
    const slice = items.slice(start, start + pageSize)
    const from = total === 0 ? 0 : start + 1
    const to = start + slice.length
    return {
      slice,
      total,
      from,
      to,
      page: safePage,
      pageCount,
      pageSize,
      setPage,
      setPageSize,
    }
  }, [items, page, pageSize, setPageSize])
}
