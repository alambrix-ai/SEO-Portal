/**
 * App-wide confirm dialog.
 *
 * Replaces `window.confirm` so every destructive or consequential question
 * uses the same Industry panel - not the browser chrome that reads as
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
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'

import { Blueprint } from '@/components/ui'
import { popModalOpen, pushModalOpen } from '@/lib/modalOpen'

export type ConfirmTone = 'danger' | 'neutral'

export interface ConfirmOptions {
  title: string
  message: string
  /** Extra line under the message - consequence, kept quieter. */
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

/** The confirm panel's controls are buttons; links are covered for safety. */
const FOCUSABLE = 'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'

/**
 * Keyboard handling for the open confirm (WAI-ARIA alertdialog): Tab and
 * Shift+Tab stay inside it, Escape cancels, and focus goes back to the
 * control that asked once it closes.
 *
 * Same behaviour as `useModalFocus` in ui.tsx, which is not exported. The
 * listener runs in the capture phase and stops Tab and Escape there, because
 * a confirm usually opens over a Dialog or Drawer whose own document-level
 * handler would otherwise pull focus back into that panel (Tab) or close it
 * too (Escape).
 */
function useConfirmFocus(
  open: boolean,
  containerRef: RefObject<HTMLElement>,
  triggerRef: RefObject<HTMLElement | null>,
  onCancel: () => void,
): void {
  const onCancelRef = useRef(onCancel)
  onCancelRef.current = onCancel

  useEffect(() => {
    if (!open) return
    // Read from the ref, not document.activeElement: the Cancel button's
    // autoFocus has already moved focus by the time this effect runs.
    const trigger = triggerRef.current

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onCancelRef.current()
        return
      }
      const container = containerRef.current
      if (event.key !== 'Tab' || !container) return
      event.stopPropagation()
      const items = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (element) => element.getClientRects().length > 0,
      )
      const first = items[0]
      const last = items[items.length - 1]
      if (!first || !last) {
        event.preventDefault()
        return
      }
      const active = document.activeElement
      const inside = container.contains(active)
      if (event.shiftKey && (!inside || active === first)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (!inside || active === last)) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKey, true)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    pushModalOpen()
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.body.style.overflow = previous
      popModalOpen()
      // Back to the control that asked, if it is still on the page.
      if (trigger && trigger.isConnected && trigger !== document.body) {
        trigger.focus({ preventScroll: true })
      }
    }
  }, [open, containerRef, triggerRef])
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)
  const pendingRef = useRef<Pending | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  /** What had focus when the question was asked, to return to on close. */
  const triggerRef = useRef<HTMLElement | null>(null)

  const close = useCallback((value: boolean) => {
    const current = pendingRef.current
    pendingRef.current = null
    setPending(null)
    current?.resolve(value)
  }, [])

  useConfirmFocus(pending !== null, containerRef, triggerRef, () => close(false))

  const confirm = useCallback((options: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      // If a confirm is already open, reject the previous as cancelled so
      // callers are never left hanging.
      if (pendingRef.current) {
        pendingRef.current.resolve(false)
      } else {
        triggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
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
      {pending
        ? createPortal(
            <div
              ref={containerRef}
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
            </div>,
            document.body,
          )
        : null}
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
