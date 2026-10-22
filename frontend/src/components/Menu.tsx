/**
 * A small dropdown menu: a trigger and a list of actions.
 *
 * Used for the user menu, the workspace switcher and the overflow ("⋯")
 * actions on table rows, so secondary actions stop competing with the one
 * primary action a row offers.
 */
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export interface MenuItem {
  label: ReactNode
  onSelect: () => void
  tone?: 'default' | 'danger'
  disabled?: boolean
  /** Marks the current choice (workspace switcher). */
  checked?: boolean
  hint?: ReactNode
}

export function Menu({
  trigger,
  items,
  header,
  footer,
  align = 'end',
  label,
  triggerClassName = 'btn btn-ghost btn-icon',
  placement = 'below',
}: {
  trigger: ReactNode
  items: (MenuItem | 'divider')[]
  header?: ReactNode
  footer?: ReactNode
  align?: 'start' | 'end'
  label: string
  triggerClassName?: string
  placement?: 'below' | 'above'
}) {
  const [open, setOpen] = useState(false)
  const wrapper = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (wrapper.current && !wrapper.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="menu" ref={wrapper}>
      <button
        type="button"
        className={triggerClassName}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        title={label}
        onClick={() => setOpen((value) => !value)}
      >
        {trigger}
      </button>
      {open ? (
        <div
          className={`menu-panel is-${align} is-${placement}`}
          role="menu"
          aria-label={label}
        >
          {header ? <div className="menu-header">{header}</div> : null}
          {items.map((item, index) =>
            item === 'divider' ? (
              <div key={`d${index}`} className="menu-divider" role="separator" />
            ) : (
              <button
                key={index}
                type="button"
                role="menuitem"
                className={`menu-item${item.tone === 'danger' ? ' is-danger' : ''}${
                  item.checked ? ' is-checked' : ''
                }`}
                disabled={item.disabled}
                onClick={() => {
                  setOpen(false)
                  item.onSelect()
                }}
              >
                <span className="menu-item-label">{item.label}</span>
                {item.hint ? <span className="menu-item-hint">{item.hint}</span> : null}
              </button>
            ),
          )}
          {footer ? <div className="menu-footer">{footer}</div> : null}
        </div>
      ) : null}
    </div>
  )
}
