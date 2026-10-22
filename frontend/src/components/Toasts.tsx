/**
 * Toast notifications.
 *
 * Messages come from the API's `toast` field wherever an endpoint returns one,
 * so the wording lives with the rule that produced it. The bell in the header
 * shows the count and clears them.
 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { ApiError, type ApiErrorFields } from '@/api/client'
import type { Toast } from '@/api/types'
import { softenErrorMessage } from '@/lib/softenError'

interface ToastItem extends Toast {
  id: number
}

interface ToastState {
  toasts: ToastItem[]
  push: (message: string, kind?: Toast['kind']) => void
  /** Show an API `ActionResult.toast` if it carried one. */
  fromResult: (result: { toast: Toast | null }) => void
  /** Turn a thrown error into a toast, using the API's own wording. */
  fromError: (error: unknown, fallback?: string) => void
  dismiss: (id: number) => void
  clear: () => void
}

const ToastContext = createContext<ToastState | null>(null)

const VISIBLE_MS = 4500

/**
 * Keys in `fields` that are not about a form field: `reason` is a code such
 * as "too_large" and `request_id` is for support. Neither means anything to
 * the person reading the toast.
 */
const NON_FIELD_KEYS = new Set(['reason', 'request_id'])

/** A bare code ("too_large", "rate_limited"), not a sentence. */
const CODE_SHAPED = /^[a-z0-9]+(?:[_.-][a-z0-9]+)*$/

/** The first field error worth showing, or undefined to use the summary. */
function fieldErrorMessage(fields: ApiErrorFields | undefined): string | undefined {
  for (const [key, value] of Object.entries(fields ?? {})) {
    if (NON_FIELD_KEYS.has(key) || typeof value !== 'string') continue
    const text = value.trim()
    if (text && !CODE_SHAPED.test(text)) return text
  }
  return undefined
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (message: string, kind: Toast['kind'] = 'info') => {
      if (!message) return
      const id = nextId.current++
      setToasts((current) => [...current, { id, message, kind }])
      window.setTimeout(() => dismiss(id), VISIBLE_MS)
    },
    [dismiss],
  )

  const fromResult = useCallback(
    (result: { toast: Toast | null }) => {
      if (result?.toast) push(result.toast.message, result.toast.kind)
    },
    [push],
  )

  const fromError = useCallback(
    (error: unknown, fallback = 'Something went wrong. Please try again.') => {
      if (error instanceof ApiError) {
        // Field errors are more useful than the summary when present.
        const raw = fieldErrorMessage(error.fields) ?? error.message
        push(softenErrorMessage(raw, fallback), error.isForbidden ? 'warning' : 'error')
        return
      }
      if (error instanceof Error && error.name === 'AbortError') return
      if (error instanceof Error) {
        push(softenErrorMessage(error.message, fallback), 'error')
        return
      }
      push(fallback, 'error')
    },
    [push],
  )

  const clear = useCallback(() => setToasts([]), [])

  const value = useMemo<ToastState>(
    () => ({ toasts, push, fromResult, fromError, dismiss, clear }),
    [toasts, push, fromResult, fromError, dismiss, clear],
  )

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  )
}

function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[]
  onDismiss: (id: number) => void
}) {
  if (toasts.length === 0) return null
  return (
    <div className="toast-viewport" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={`card elev-md toast toast-${toast.kind}`}>
          <span className="toast-message">{toast.message}</span>
          <button
            type="button"
            className="btn btn-ghost toast-dismiss"
            onClick={() => onDismiss(toast.id)}
            aria-label="Dismiss notification"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}

export function useToasts(): ToastState {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToasts must be used inside a ToastProvider')
  return context
}
