/**
 * The header's live activity control: which agents are running right now.
 *
 * Polls the workspace activity feed — quickly while something is running,
 * slowly otherwise — so a run started from any screen, or by the schedule,
 * is visible wherever the user is.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type { AgentActivity } from '@/api/types'
import { AgentIcon } from '@/components/AgentIcon'
import { RunStateBadge } from '@/components/RunViews'
import { runOutcome } from '@/lib/plain'
import { ago, elapsed, useNow, when } from '@/lib/time'

const FAST_MS = 4000
const SLOW_MS = 30000

export function ActivityIndicator() {
  const [data, setData] = useState<AgentActivity | null>(null)
  const [open, setOpen] = useState(false)
  const wrapper = useRef<HTMLDivElement>(null)
  const now = useNow(open ? 1000 : 15000)

  const load = useCallback(async () => {
    try {
      setData(await api.agentActivity())
    } catch {
      /* A failed poll is not worth a message; the next one will do. */
    }
  }, [])

  const running = data?.running ?? []

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void load()
    }, running.length > 0 || open ? FAST_MS : SLOW_MS)
    return () => window.clearInterval(timer)
  }, [running.length, open, load])

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (wrapper.current && !wrapper.current.contains(event.target as Node)) setOpen(false)
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

  const label =
    running.length === 0 ? 'No agents running' : `${running.length} agent${running.length === 1 ? '' : 's'} running`

  return (
    <div className="activity" ref={wrapper}>
      <button
        type="button"
        className={`activity-trigger${running.length ? ' is-live' : ''}`}
        aria-expanded={open}
        aria-label={`Agent activity: ${label}`}
        onClick={() => {
          setOpen((value) => !value)
          void load()
        }}
      >
        {running.length ? <span className="live-dot" aria-hidden="true" /> : <span className="idle-dot" aria-hidden="true" />}
        <span className="activity-trigger-text">{running.length ? `${running.length} running` : 'Activity'}</span>
      </button>

      {open ? (
        <>
        <div className="popover-scrim" onClick={() => setOpen(false)} />
        <div className="activity-panel" role="dialog" aria-label="Agent activity">
          <div className="activity-section-title">Running now</div>
          {running.length === 0 ? (
            <p className="activity-empty">No agent is running at the moment.</p>
          ) : (
            <ul className="activity-list">
              {running.map((run) => (
                <li key={run.id}>
                  <Link to={`/agents/${run.agent_slug}`} className="activity-row" onClick={() => setOpen(false)}>
                    <AgentIcon slug={run.agent_slug} size={28} />
                    <span className="activity-row-main">
                      <span className="activity-row-name">{run.agent_name}</span>
                      <span className="activity-row-step">{run.step || 'Starting…'}</span>
                      <span
                        className={`progress-track is-small${run.step_percent === null ? ' is-indeterminate' : ''}`}
                        aria-hidden="true"
                      >
                        <span
                          className="progress-fill"
                          style={run.step_percent === null ? undefined : { width: `${run.step_percent}%` }}
                        />
                      </span>
                    </span>
                    <span className="activity-row-side">{elapsed(run.started_at, now)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}

          <div className="activity-section-title">Recent runs</div>
          {(data?.recent ?? []).length === 0 ? (
            <p className="activity-empty">No runs yet.</p>
          ) : (
            <ul className="activity-list">
              {(data?.recent ?? []).slice(0, 6).map((run) => (
                <li key={run.id}>
                  <Link to={`/agents/${run.agent_slug}`} className="activity-row" onClick={() => setOpen(false)}>
                    <AgentIcon slug={run.agent_slug} size={28} />
                    <span className="activity-row-main">
                      <span className="activity-row-name">{run.agent_name}</span>
                      <span className="activity-row-step" title={runOutcome(run)}>
                        {runOutcome(run) || '—'}
                      </span>
                    </span>
                    <span className="activity-row-side is-stack">
                      <RunStateBadge run={run} />
                      <span title={when(run.started_at)}>{ago(run.finished_at ?? run.started_at, now)}</span>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
          <Link to="/agents" className="activity-footer" onClick={() => setOpen(false)}>
            All agents →
          </Link>
        </div>
        </>
      ) : null}
    </div>
  )
}
