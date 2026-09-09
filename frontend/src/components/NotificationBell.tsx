/**
 * The header's bell, and its unseen indicator.
 *
 * It used to be wired to `clear()` on the toast list, which meant clicking it
 * *dismissed* notifications rather than showing any — and since toasts expire
 * after four and a half seconds, the count it displayed was almost always
 * zero. A bell that counts only what happened in this tab since it loaded is
 * a decoration.
 *
 * So it opens a panel over `/notifications`, which is the workspace's audit
 * log filtered to the modules the caller can see. That means it survives a
 * reload, shows what the agents did while nobody was watching, and cannot
 * become a way around the access map.
 *
 * The dot means "there is something you have not seen", not "there is
 * activity" — the second is true from the first minute and would leave a
 * badge nobody can ever clear. The marker lives on the server, so reading
 * something on a laptop does not leave it unread on a phone, and the count
 * excludes the viewer's own actions: a dot that appears because you just
 * clicked something trains people to ignore the dot.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type { NotificationsOut } from '@/api/types'
import { BellIcon } from '@/components/icons'

/** How often the badge is refreshed while the panel is closed. */
const POLL_MS = 60_000

export function NotificationBell({ pendingApprovals }: { pendingApprovals: number }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<NotificationsOut | null>(null)
  const [unseen, setUnseen] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const wrapper = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    const result = await api.notifications()
    setData(result)
    setUnseen(result.unseen)
    return result
  }, [])

  // The badge has to be able to appear without the user doing anything —
  // agents work on a schedule, and the whole point of an indicator is to
  // notice something you were not watching for. A minute is slow enough to be
  // nothing and often enough to be useful.
  useEffect(() => {
    let cancelled = false
    const tick = () => {
      void load().catch(() => {
        /* A failed poll is not worth a message; the next one will do. */
      })
    }
    tick()
    const timer = window.setInterval(() => {
      if (!cancelled && !document.hidden) tick()
    }, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [load])

  // Opening it is what marks it read, and the dot clears immediately rather
  // than after the round trip — the user has seen it either way.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError('')
    load()
      .then(() => {
        if (cancelled) return
        setUnseen(0)
        return api.markNotificationsSeen()
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : 'Could not load')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, load])

  // Close on an outside click or Escape, like any other popover.
  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpen(false)
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

  const entries = data?.entries ?? []
  const label = unseen
    ? `Notifications — ${unseen} new`
    : pendingApprovals
      ? `Notifications — ${pendingApprovals} awaiting approval`
      : 'Notifications'

  return (
    <div className="bell-wrap" ref={wrapper}>
      <button
        type="button"
        className={`btn btn-icon btn-secondary topbar-bell${unseen ? ' has-unseen' : ''}`}
        onClick={() => setOpen((current) => !current)}
        title={label}
        aria-label={label}
        aria-expanded={open}
      >
        <BellIcon />
        {/* A count when it is small enough to be worth reading, a plain dot
            when it is not. "47 new" is not more actionable than "new". */}
        {unseen > 0 ? (
          <span className="topbar-bell-count">{unseen > 9 ? '9+' : unseen}</span>
        ) : pendingApprovals > 0 ? (
          <span className="topbar-bell-count is-quiet">{pendingApprovals}</span>
        ) : null}
      </button>

      {open ? (
        <div className="bell-panel" role="dialog" aria-label="Notifications">
          <div className="bell-panel-head">
            <span>Recent activity</span>
            {pendingApprovals > 0 ? (
              <Link to="/approvals" className="small" onClick={() => setOpen(false)}>
                {pendingApprovals} awaiting approval
              </Link>
            ) : null}
          </div>

          {loading && !data ? (
            <div className="bell-empty">Loading…</div>
          ) : error ? (
            <div className="bell-empty">{error}</div>
          ) : entries.length === 0 ? (
            <div className="bell-empty">
              Nothing yet. Agent actions and team changes show up here.
            </div>
          ) : (
            <ul className="bell-list">
              {entries.map((entry, index) => (
                <li key={`${entry.time}-${index}`} className="bell-item">
                  <div className="bell-item-action">
                    <strong>{entry.actor}</strong> {entry.action}
                  </div>
                  <div className="bell-item-meta">
                    {entry.time}
                    {entry.module ? ` · ${entry.module}` : ''}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  )
}
