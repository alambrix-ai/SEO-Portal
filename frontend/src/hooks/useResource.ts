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
import { softenErrorMessage } from '@/lib/softenError'

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

// ── Plain-English load errors ──────────────────────────────────────────────
// The browser's own wording for a dropped connection or a non-JSON reply
// ("Failed to fetch", "Unexpected token '<' … is not valid JSON") means
// nothing to a marketer. These cover Chrome, Firefox and Safari.
export const NETWORK_ERROR_MESSAGE =
  'We could not reach the server. Check your internet connection and try again.'
export const BAD_REPLY_MESSAGE =
  'The server sent a reply we could not read. Please try again in a moment.'
export const LOAD_FALLBACK_MESSAGE = 'Something went wrong while loading this. Please try again.'

const NETWORK_TEXT =
  /failed to fetch|networkerror|network request failed|load failed|network error|err_(network|internet|connection)/i
const BAD_REPLY_TEXT =
  /is not valid json|json\.parse|json parse error|in json at position|unexpected token|unexpected end of (json|data|input)|did not match the expected pattern|<!doctype|<html/i

/** Turn any message headed for a screen into plain English. */
export function softenLoadMessage(message: string): string {
  const text = (message || '').trim()
  if (NETWORK_TEXT.test(text)) return NETWORK_ERROR_MESSAGE
  if (BAD_REPLY_TEXT.test(text)) return BAD_REPLY_MESSAGE
  return softenErrorMessage(text, LOAD_FALLBACK_MESSAGE)
}

/**
 * The message to show for a failed load. Only `ApiError` carries wording the
 * backend chose. Anything else is the browser's or the runtime's wording
 * (including programming errors such as "Cannot read properties of
 * undefined"), so it maps to a fixed sentence and its text is never shown.
 */
export function friendlyLoadError(caught: unknown): string {
  if (caught instanceof ApiError) return softenErrorMessage(caught.message, LOAD_FALLBACK_MESSAGE)
  // response.json() rejects with a SyntaxError on an HTML or truncated body.
  if (caught instanceof SyntaxError) return BAD_REPLY_MESSAGE
  const text = caught instanceof Error ? caught.message : ''
  // fetch() rejects with a TypeError whose wording differs per browser.
  if (NETWORK_TEXT.test(text)) return NETWORK_ERROR_MESSAGE
  if (BAD_REPLY_TEXT.test(text)) return BAD_REPLY_MESSAGE
  return LOAD_FALLBACK_MESSAGE
}

const CACHE = new Map<string, CacheEntry>()
const DEFAULT_STALE_MS = 45_000

// Every entry is stored under the signed-in user and workspace, so one
// person's (or workspace's) payload can never paint for another, even inside
// the stale window. With no scope (signed out) nothing is cached at all.
const SCOPE_SEPARATOR = '\u0001'
let cacheScope = ''

/**
 * Set who the cache belongs to (`user id:workspace id`, or '' when signed
 * out). A change of scope empties the cache.
 */
export function setResourceCacheScope(scope: string) {
  if (scope === cacheScope) return
  cacheScope = scope
  CACHE.clear()
}

function scopedKey(key: string | undefined): string | null {
  if (!key || !cacheScope) return null
  return `${cacheScope}${SCOPE_SEPARATOR}${key}`
}

function cacheGet<T>(key: string | undefined, staleMs: number): T | null {
  const full = scopedKey(key)
  if (!full) return null
  const hit = CACHE.get(full)
  if (!hit) return null
  if (Date.now() - hit.at > staleMs) return null
  return hit.data as T
}

function cacheSet(key: string | undefined, data: unknown) {
  const full = scopedKey(key)
  if (!full) return
  CACHE.set(full, { at: Date.now(), data })
}

function cacheDrop(key: string | undefined) {
  const full = scopedKey(key)
  if (full) CACHE.delete(full)
}

/** Drop cached payloads after a mutation that changes what a screen shows. */
export function invalidateResourceCache(prefix?: string) {
  if (!prefix) {
    CACHE.clear()
    return
  }
  for (const full of [...CACHE.keys()]) {
    const key = full.slice(full.indexOf(SCOPE_SEPARATOR) + 1)
    if (key.startsWith(prefix)) CACHE.delete(full)
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
      if (caught instanceof ApiError && (caught.isForbidden || caught.isUnauthenticated)) {
        // No longer allowed to see it (role changed, signed out): stop
        // painting the previous payload and forget the cached copy.
        dataRef.current = null
        setData(null)
        cacheDrop(cacheKeyRef.current)
        setForbidden(caught.isForbidden)
        setError(friendlyLoadError(caught))
      } else if (!(caught instanceof Error && caught.name === 'AbortError')) {
        setError(friendlyLoadError(caught))
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
