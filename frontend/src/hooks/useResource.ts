/**
 * Data loading for the console's screens.
 *
 * Deliberately small: every screen loads one endpoint that returns everything
 * it renders, so there is no cache to invalidate and no query library to
 * configure. What it does handle is the two things that actually bite —
 * cancelling a request whose screen has unmounted, and reloading after a
 * mutation without a full navigation.
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

export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: unknown[] = [],
): ResourceState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  // Kept in a ref so `reload` is stable and does not re-trigger the effect.
  const loadRef = useRef(load)
  loadRef.current = load

  const controller = useRef<AbortController | null>(null)

  const run = useCallback(async () => {
    controller.current?.abort()
    const next = new AbortController()
    controller.current = next

    setLoading(true)
    setError(null)
    setForbidden(false)
    try {
      const result = await loadRef.current(next.signal)
      if (!next.signal.aborted) setData(result)
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
    void run()
    return () => controller.current?.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, loading, error, forbidden, reload: run, set: setData }
}

/**
 * Debounce a value — used by the search inputs so typing does not fire a
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
