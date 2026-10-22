/**
 * UI primitives for the Industry design system.
 *
 * Surfaces use rounded ink-bordered cards with hard offset shadows. `Blueprint`
 * is an optional craft frame (quiet registration marks) for hero panels  - 
 * dense tiles and catalogues use plain `.card` so screens stay scannable.
 */
import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import type { ComponentPropsWithoutRef, CSSProperties, ReactNode, RefObject } from 'react'

import { softenLoadMessage } from '@/hooks/useResource'
import { popModalOpen, pushModalOpen } from '@/lib/modalOpen'

// ── Blueprint frame ────────────────────────────────────────────────────────
export function Blueprint({
  children,
  className = '',
  style,
  marks = false,
  as: Tag = 'div',
  ...rest
}: {
  children?: ReactNode
  className?: string
  style?: CSSProperties
  /** Quiet corner marks - off by default for denser catalogues. */
  marks?: boolean
  as?: 'div' | 'section' | 'article' | 'aside'
} & Omit<ComponentPropsWithoutRef<'div'>, 'className' | 'style' | 'children'>) {
  return (
    <Tag className={`blueprint ${className}`.trim()} style={style} {...rest}>
      {marks ? (
        <>
          <i className="corner tl" />
          <i className="corner tr" />
          <i className="corner bl" />
          <i className="corner br" />
        </>
      ) : null}
      {children}
    </Tag>
  )
}

// ── KPI tile ───────────────────────────────────────────────────────────────
export function StatTile({
  kicker,
  value,
  meta,
  size = 30,
}: {
  kicker: string
  value: ReactNode
  meta?: string
  size?: number
}) {
  return (
    <div className="card elev-sm stat-tile">
      <div className="card-kicker">{kicker}</div>
      <div className="stat-value" style={{ fontSize: size }}>
        {value}
      </div>
      {meta ? <div className="card-meta">{meta}</div> : null}
    </div>
  )
}

// ── Tags ───────────────────────────────────────────────────────────────────
export type TagTone = 'accent' | 'accent-2' | 'neutral' | 'outline' | 'danger' | 'warning'

export function Tag({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode
  tone?: TagTone
  title?: string
}) {
  return (
    <span className={`tag tag-${tone}`} title={title}>
      {children}
    </span>
  )
}

/** Status word to tone, so the same state reads the same on every screen. */
export function statusTone(status: string): TagTone {
  switch (status) {
    case 'running':
    case 'flagged':
    case 'Discovered':
      return 'accent'
    case 'rewritten':
    case 'Won':
    case 'ok':
      return 'accent-2'
    case 'error':
    case 'failing':
      return 'danger'
    default:
      return 'neutral'
  }
}

// ── Section heading ────────────────────────────────────────────────────────
export function SectionHeading({
  title,
  description,
  action,
  spaced = false,
}: {
  title: string
  description?: string
  action?: ReactNode
  spaced?: boolean
}) {
  return (
    <div className={spaced ? 'section-heading section-heading-spaced' : 'section-heading'}>
      <div>
        <h3>{title}</h3>
        {description ? <p className="section-description">{description}</p> : null}
      </div>
      {action}
    </div>
  )
}

// ── Empty and loading states ───────────────────────────────────────────────
export function EmptyState({
  title,
  description,
  action,
}: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <Blueprint className="card empty-state">
      <div className="card-title">{title}</div>
      {description ? <p className="card-body">{description}</p> : null}
      {action}
    </Blueprint>
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      {label}
    </div>
  )
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string
  onRetry?: () => void
}) {
  return (
    <Blueprint className="card empty-state">
      <div className="card-title">Could not load this</div>
      {/* Callers pass raw caught text too; never show "Failed to fetch" or a
          JSON parse error to a marketer. */}
      <p className="card-body">{softenLoadMessage(message)}</p>
      {onRetry ? (
        <button type="button" className="btn btn-secondary" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </Blueprint>
  )
}

// ── Form field ─────────────────────────────────────────────────────────────
export function Field({
  label,
  children,
  description,
  hint,
  error,
  htmlFor,
  required = false,
}: {
  label: string
  children: ReactNode
  /** Sits under the label, before the control, so it is not read as the next field. */
  description?: string
  hint?: string
  error?: string
  htmlFor?: string
  /** Marks the label, for the fields the form will not submit without. */
  required?: boolean
}) {
  const descriptionId = htmlFor ? `${htmlFor}-desc` : undefined
  return (
    <div className="field">
      <label htmlFor={htmlFor}>
        {label}
        {/* aria-hidden because the asterisk is a visual convention: a screen
            reader gets the same fact from the input's own required state,
            and hearing "asterisk" after every label is noise. */}
        {required ? (
          <span className="field-required" aria-hidden="true">
            *
          </span>
        ) : null}
      </label>
      {description ? (
        <p className="field-description" id={descriptionId}>
          {description}
        </p>
      ) : null}
      {children}
      {error ? (
        <div className="field-error">{error}</div>
      ) : hint ? (
        <div className="field-hint">{hint}</div>
      ) : null}
    </div>
  )
}

// ── Segmented control ──────────────────────────────────────────────────────
export function Segmented<T extends string>({
  name,
  value,
  options,
  onChange,
  disabled = false,
}: {
  name: string
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
  disabled?: boolean
}) {
  return (
    <div className="seg">
      {options.map((option) => (
        <label className="seg-opt" key={option.value}>
          <input
            type="radio"
            name={name}
            checked={value === option.value}
            disabled={disabled}
            onChange={() => onChange(option.value)}
          />
          <span>{option.label}</span>
        </label>
      ))}
    </div>
  )
}

// ── Modal focus ────────────────────────────────────────────────────────────
const FOCUSABLE = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'iframe',
  'audio[controls]',
  'video[controls]',
  '[contenteditable]:not([contenteditable="false"])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/** Form fields get first focus; a button never does, so Enter on open can't confirm. */
const FIRST_FIELD =
  'input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled])'

function isShown(element: HTMLElement): boolean {
  return element.getClientRects().length > 0
}

function focusables(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(isShown)
}

/** Open modals, innermost last. Only the innermost one handles keys. */
const MODAL_STACK: object[] = []

/**
 * Keyboard behaviour shared by `Dialog` and `Drawer` (WAI-ARIA dialog pattern):
 * focus moves into the panel on open, Tab and Shift+Tab stay inside it, Escape
 * closes it, and focus goes back to whatever opened it on close.
 *
 * Runs once per mount. `onClose` is read through a ref because callers pass a
 * new arrow every render, and re-running would pull focus back to the first
 * field on every keystroke.
 */
function useModalFocus(panelRef: RefObject<HTMLElement>, onClose: () => void): void {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    const token = {}
    MODAL_STACK.push(token)
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const panel = panelRef.current

    // A child with autoFocus has already taken focus during commit; keep it.
    if (panel && !panel.contains(document.activeElement)) {
      const field = Array.from(panel.querySelectorAll<HTMLElement>(FIRST_FIELD)).find(isShown)
      ;(field ?? panel).focus()
    }

    const onKey = (event: KeyboardEvent) => {
      if (MODAL_STACK[MODAL_STACK.length - 1] !== token) return
      if (event.key === 'Escape') {
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab' || !panel) return
      const items = focusables(panel)
      const first = items[0]
      const last = items[items.length - 1]
      if (!first || !last) {
        event.preventDefault()
        panel.focus()
        return
      }
      const active = document.activeElement
      const inside = panel.contains(active)
      if (event.shiftKey && (!inside || active === first || active === panel)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (!inside || active === last)) {
        event.preventDefault()
        first.focus()
      }
    }
    // Bound to the document, not the panel: a keydown handler on a div only
    // fires when focus is already inside it, so Escape after clicking the page
    // did nothing and the dialog looked stuck.
    document.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    pushModalOpen()

    return () => {
      document.removeEventListener('keydown', onKey)
      const index = MODAL_STACK.indexOf(token)
      if (index !== -1) MODAL_STACK.splice(index, 1)
      document.body.style.overflow = previous
      popModalOpen()
      // Back to the control that opened it, if it is still on the page.
      if (trigger && trigger.isConnected && trigger !== document.body) {
        trigger.focus({ preventScroll: true })
      }
    }
  }, [panelRef])
}

// ── Dialog ─────────────────────────────────────────────────────────────────
export function Dialog({
  title,
  children,
  actions,
  onClose,
  width,
}: {
  title: string
  children: ReactNode
  actions?: ReactNode
  onClose: () => void
  width?: number
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  useModalFocus(panelRef, onClose)

  // A plain div with Blueprint's class (no marks), because Blueprint does not
  // forward a ref and the focus trap needs the panel element.
  return createPortal(
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <div
        ref={panelRef}
        className="blueprint dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        style={width ? { width: `min(${width}px, 100%)` } : undefined}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="dialog-title">{title}</div>
        <div className="dialog-body">{children}</div>
        {actions ? <div className="dialog-actions">{actions}</div> : null}
      </div>
    </div>,
    document.body,
  )
}

// ── Meter ──────────────────────────────────────────────────────────────────
export function Meter({
  percent,
  label,
  tone = 'accent',
}: {
  percent: number
  label?: string
  tone?: 'accent' | 'neutral'
}) {
  const clamped = Math.max(0, Math.min(100, percent))
  return (
    <div
      className="meter"
      role="meter"
      aria-valuenow={clamped}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <div className={`meter-fill meter-${tone}`} style={{ width: `${clamped}%` }} />
    </div>
  )
}

// ── Page header ────────────────────────────────────────────────────────────
/** Title, one-line purpose and the page's primary actions, on every screen. */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="page-header">
      <div className="page-header-text">
        <h1 className="page-header-title">{title}</h1>
        {description ? <p className="page-header-description">{description}</p> : null}
      </div>
      {actions ? <div className="page-header-actions">{actions}</div> : null}
    </div>
  )
}

// ── Drawer ─────────────────────────────────────────────────────────────────
/**
 * A side panel for editing one record (an agent's settings, a connector's
 * credentials) without losing the list behind it.
 */
export function Drawer({
  title,
  subtitle,
  icon,
  children,
  actions,
  onClose,
  width = 520,
}: {
  title: string
  subtitle?: ReactNode
  icon?: ReactNode
  children: ReactNode
  actions?: ReactNode
  onClose: () => void
  width?: number
}) {
  const panelRef = useRef<HTMLElement>(null)
  useModalFocus(panelRef, onClose)

  return createPortal(
    <div className="drawer-backdrop" role="presentation" onClick={onClose}>
      <aside
        ref={panelRef}
        tabIndex={-1}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        style={{ width: `min(${width}px, 100%)` }}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer-head">
          {icon ? <div className="drawer-head-icon">{icon}</div> : null}
          <div className="drawer-head-text">
            <div className="drawer-title">{title}</div>
            {subtitle ? <div className="drawer-subtitle">{subtitle}</div> : null}
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            aria-label="Close"
            onClick={onClose}
          >
            <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
              <line x1="5" y1="5" x2="15" y2="15" />
              <line x1="15" y1="5" x2="5" y2="15" />
            </svg>
          </button>
        </header>
        <div className="drawer-body">{children}</div>
        {actions ? <footer className="drawer-actions">{actions}</footer> : null}
      </aside>
    </div>,
    document.body,
  )
}

/** A titled group of fields inside a drawer or form. */
export function FormSection({
  title,
  description,
  children,
}: {
  title: string
  description?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="form-section">
      <div className="form-section-head">
        <div className="form-section-title">{title}</div>
        {description ? <div className="form-section-description">{description}</div> : null}
      </div>
      <div className="form-section-body">{children}</div>
    </section>
  )
}

/** Status dot + label: the compact status cell used in tables. */
export function StatusDot({
  tone,
  children,
  title,
}: {
  tone: 'success' | 'warning' | 'danger' | 'neutral' | 'info'
  children: ReactNode
  title?: string
}) {
  return (
    <span className={`status-dot is-${tone}`} title={title}>
      <span className="status-dot-mark" aria-hidden="true" />
      {children}
    </span>
  )
}
