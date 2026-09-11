/**
 * UI primitives for the Industry design system.
 *
 * Surfaces use rounded ink-bordered cards with hard offset shadows. `Blueprint`
 * is an optional craft frame (quiet registration marks) for hero panels  - 
 * dense tiles and catalogues use plain `.card` so screens stay scannable.
 */
import { useEffect } from 'react'
import type { ComponentPropsWithoutRef, CSSProperties, ReactNode } from 'react'

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
export type TagTone = 'accent' | 'accent-2' | 'neutral' | 'outline'

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
      return 'outline'
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
      <p className="card-body">{message}</p>
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
  hint,
  error,
  htmlFor,
  required = false,
}: {
  label: string
  children: ReactNode
  hint?: string
  error?: string
  htmlFor?: string
  /** Marks the label, for the fields the form will not submit without. */
  required?: boolean
}) {
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
  // Escape is bound to the document, not to the backdrop. A keydown handler on
  // a div only fires when focus is already inside it, so pressing Escape after
  // clicking the page did nothing - the dialog looked stuck.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    // A modal over a scrollable grid should not let the page scroll behind it.
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose])

  return (
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <Blueprint
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        style={width ? { width: `min(${width}px, 100%)` } : undefined}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="dialog-title">{title}</div>
        <div className="dialog-body">{children}</div>
        {actions ? <div className="dialog-actions">{actions}</div> : null}
      </Blueprint>
    </div>
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
