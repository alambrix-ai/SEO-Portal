/**
 * A small dropdown menu: a trigger and a list of actions.
 *
 * Used for the user menu, the workspace switcher and the overflow ("⋯")
 * actions on table rows, so secondary actions stop competing with the one
 * primary action a row offers.
 *
 * The panel is portaled to `document.body` and positioned with `position:
 * fixed`. Row lists live inside `.panel` / `.table-wrap` / `.page`, all of
 * which clip overflow — an in-flow absolute panel then peeks in from the
 * right like a sliding sheet instead of a popup.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'

export interface MenuItem {
  label: ReactNode
  onSelect: () => void
  tone?: 'default' | 'danger'
  disabled?: boolean
  /** Marks the current choice (workspace switcher). */
  checked?: boolean
  hint?: ReactNode
}

const GAP = 6
const VIEW_PAD = 8

function placePanel(
  trigger: DOMRect,
  size: { width: number; height: number },
  align: 'start' | 'end',
  preferred: 'below' | 'above',
): { top: number; left: number } {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const { width, height } = size

  const below = trigger.bottom + GAP
  const above = trigger.top - GAP - height
  let top =
    preferred === 'above'
      ? above >= VIEW_PAD
        ? above
        : below
      : below + height <= vh - VIEW_PAD
        ? below
        : Math.max(VIEW_PAD, above)
  if (top + height > vh - VIEW_PAD) top = Math.max(VIEW_PAD, vh - VIEW_PAD - height)
  if (top < VIEW_PAD) top = VIEW_PAD

  let left = align === 'end' ? trigger.right - width : trigger.left
  if (left + width > vw - VIEW_PAD) left = vw - VIEW_PAD - width
  if (left < VIEW_PAD) left = VIEW_PAD
  return { top, left }
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
  const [coords, setCoords] = useState<{ top: number; left: number; minWidth: number } | null>(null)
  const wrapper = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    if (!open) {
      setCoords(null)
      return
    }
    const place = () => {
      const button = triggerRef.current
      const panel = panelRef.current
      if (!button || !panel) return
      const rect = button.getBoundingClientRect()
      if (rect.bottom < VIEW_PAD || rect.top > window.innerHeight - VIEW_PAD) {
        setOpen(false)
        return
      }
      const minWidth = align === 'start' ? Math.max(220, Math.round(rect.width)) : 220
      panel.style.minWidth = `${minWidth}px`
      const next = placePanel(
        rect,
        { width: panel.offsetWidth, height: panel.offsetHeight },
        align,
        placement,
      )
      setCoords({ ...next, minWidth })
    }
    place()
    window.addEventListener('resize', place)
    document.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      document.removeEventListener('scroll', place, true)
    }
  }, [open, align, placement, items.length])

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      const node = event.target as Node
      if (wrapper.current?.contains(node) || panelRef.current?.contains(node)) return
      setOpen(false)
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

  const panel =
    open && typeof document !== 'undefined'
      ? createPortal(
          <div
            ref={panelRef}
            className={`menu-panel is-${align} is-${placement}`}
            role="menu"
            aria-label={label}
            style={{
              top: coords?.top ?? 0,
              left: coords?.left ?? 0,
              minWidth: coords?.minWidth ?? 220,
              visibility: coords ? 'visible' : 'hidden',
              pointerEvents: coords ? 'auto' : 'none',
            }}
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
          </div>,
          document.body,
        )
      : null

  return (
    <div className="menu" ref={wrapper}>
      <button
        ref={triggerRef}
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
      {panel}
    </div>
  )
}
