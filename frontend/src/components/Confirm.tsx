/**
 * App-wide confirm dialog.
 *
 * Replaces `window.confirm` so every destructive or consequential question
 * uses the same Industry panel — not the browser chrome that reads as
 * "automarket-console.onrender.com says".
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { Blueprint } from '@/components/ui'

export type ConfirmTone = 'danger' | 'neutral'

export interface ConfirmOptions {
  title: string
  message: string
  /** Extra line under the message — consequence, kept quieter. */
  detail?: string
  confirmLabel?: string
  cancelLabel?: string
  tone?: ConfirmTone
  /** Optional mark (connector/agent icon) shown in the header. */
  icon?: ReactNode
}

interface ConfirmState {
  confirm: (options: ConfirmOptions) => Promise<boolean>
}

const ConfirmContext = createContext<ConfirmState | null>(null)

type Pending = ConfirmOptions & {
  resolve: (value: boolean) => void
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)
  const pendingRef = useRef<Pending | null>(null)

  const close = useCallback((value: boolean) => {
    const current = pendingRef.current
    pendingRef.current = null
    setPending(null)
    current?.resolve(value)
  }, [])

  useEffect(() => {
    if (!pending) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close(false)
    }
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [pending, close])

  const confirm = useCallback((options: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      // If a confirm is already open, reject the previous as cancelled so
      // callers are never left hanging.
      if (pendingRef.current) {
        pendingRef.current.resolve(false)
      }
      const next: Pending = { ...options, resolve }
      pendingRef.current = next
      setPending(next)
    })
  }, [])

  const value = useMemo(() => ({ confirm }), [confirm])

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      {pending ? (
        <div
          className="dialog-backdrop confirm-backdrop"
          role="presentation"
          onClick={() => close(false)}
        >
          <Blueprint
            className={`dialog confirm-dialog confirm-${pending.tone ?? 'neutral'}`}
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="confirm-title"
            aria-describedby="confirm-message"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="confirm-head">
              {pending.icon ? (
                <div className="confirm-icon">{pending.icon}</div>
              ) : (
                <div
                  className={`confirm-glyph confirm-glyph-${pending.tone ?? 'neutral'}`}
                  aria-hidden="true"
                >
                  {pending.tone === 'danger' ? '!' : '?'}
                </div>
              )}
              <div className="confirm-copy">
                <div className="confirm-eyebrow">
                  {pending.tone === 'danger' ? 'Please confirm' : 'Confirm'}
                </div>
                <h2 id="confirm-title" className="confirm-title">
                  {pending.title}
                </h2>
              </div>
            </div>
            <div className="confirm-body">
              <p id="confirm-message" className="confirm-message">
                {pending.message}
              </p>
              {pending.detail ? (
                <p className="confirm-detail">{pending.detail}</p>
              ) : null}
            </div>
            <div className="confirm-actions">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => close(false)}
                autoFocus
              >
                {pending.cancelLabel ?? 'Cancel'}
              </button>
              <button
                type="button"
                className={`btn btn-primary${pending.tone === 'danger' ? ' confirm-primary-danger' : ''}`}
                onClick={() => close(true)}
              >
                {pending.confirmLabel ?? 'Confirm'}
              </button>
            </div>
          </Blueprint>
        </div>
      ) : null}
    </ConfirmContext.Provider>
  )
}

export function useConfirm(): ConfirmState['confirm'] {
  const ctx = useContext(ConfirmContext)
  if (!ctx) {
    throw new Error('useConfirm must be used inside ConfirmProvider')
  }
  return ctx.confirm
}
