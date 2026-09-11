/**
 * Data loading for the console's screens.
 *
 * Deliberately small: every screen loads one endpoint that returns everything
 * it renders. A short in-memory cache keeps the previous payload so switching
 * tabs does not blank the page while a fresh request runs. Abort cancels
 * in-flight work when the screen unmounts.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/api/client'

interface ResourceState<T> {
  data: T | null
  loading: boolean
  error: string | null
  /** True when the caller's role is not allowed to see this. */
  forbidden: boolean
  reload: () => Promise<void>
  /** Replace the data locally, for endpoints that return the new state. */
  set: (value: T) => void
}

interface CacheEntry {
  at: number
  data: unknown
}

const CACHE = new Map<string, CacheEntry>()
const DEFAULT_STALE_MS = 45_000

function cacheGet<T>(key: string | undefined, staleMs: number): T | null {
  if (!key) return null
  const hit = CACHE.get(key)
  if (!hit) return null
  if (Date.now() - hit.at > staleMs) return null
  return hit.data as T
}

function cacheSet(key: string | undefined, data: unknown) {
  if (!key) return
  CACHE.set(key, { at: Date.now(), data })
}

/** Drop cached payloads after a mutation that changes what a screen shows. */
export function invalidateResourceCache(prefix?: string) {
  if (!prefix) {
    CACHE.clear()
    return
  }
  for (const key of [...CACHE.keys()]) {
    if (key === prefix || key.startsWith(`${prefix}:`) || key.startsWith(prefix)) {
      CACHE.delete(key)
    }
  }
}

export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: unknown[] = [],
  cacheKey?: string,
): ResourceState<T> {
  const staleMs = DEFAULT_STALE_MS
  const initial = cacheGet<T>(cacheKey, staleMs)
  const [data, setData] = useState<T | null>(initial)
  const [loading, setLoading] = useState(initial === null)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  const loadRef = useRef(load)
  loadRef.current = load
  const cacheKeyRef = useRef(cacheKey)
  cacheKeyRef.current = cacheKey
  const dataRef = useRef<T | null>(initial)
  dataRef.current = data

  const controller = useRef<AbortController | null>(null)

  const run = useCallback(async () => {
    controller.current?.abort()
    const next = new AbortController()
    controller.current = next

    // Keep painting cached / previous data; only blank when we have nothing.
    setLoading(dataRef.current === null)
    setError(null)
    setForbidden(false)
    try {
      const result = await loadRef.current(next.signal)
      if (next.signal.aborted) return
      dataRef.current = result
      setData(result)
      cacheSet(cacheKeyRef.current, result)
    } catch (caught) {
      if (next.signal.aborted) return
      if (caught instanceof ApiError && caught.isForbidden) {
        setForbidden(true)
        setError(caught.message)
      } else if (caught instanceof Error && caught.name !== 'AbortError') {
        setError(caught.message)
      }
    } finally {
      if (!next.signal.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    const cached = cacheGet<T>(cacheKey, staleMs)
    if (cached !== null) {
      dataRef.current = cached
      setData(cached)
      setLoading(false)
    } else {
      // New cache key (e.g. switched tenant) — don't keep painting the old payload.
      dataRef.current = null
      setData(null)
      setLoading(true)
    }
    void run()
    return () => controller.current?.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  const set = useCallback((value: T) => {
    dataRef.current = value
    setData(value)
    cacheSet(cacheKeyRef.current, value)
  }, [])

  return { data, loading, error, forbidden, reload: run, set }
}

/**
 * Debounce a value - used by the search inputs so typing does not fire a
 * request per keystroke.
 */
export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return debounced
}
