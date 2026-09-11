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

import { ApiError } from '@/api/client'
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
        const fieldMessage = Object.values(error.fields ?? {})[0]
        const raw = fieldMessage ?? error.message
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
