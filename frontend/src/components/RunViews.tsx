/**
 * Showing what an agent run is doing and did.
 *
 * Everything here renders what the server recorded for the run — its state,
 * its timeline of steps in the agent's own words, and the audit entries it
 * produced. Nothing is estimated: a percentage appears only where the agent
 * reported a countable total, and a pass that stopped reporting is shown as
 * interrupted rather than as still working.
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type { AgentRun, RunState } from '@/api/types'
import { AgentIcon } from '@/components/AgentIcon'
import { PracticeBadge } from '@/components/SafetyBits'
import { useToasts } from '@/components/Toasts'
import { CheckIcon } from '@/components/icons'
import { Drawer, StatusDot } from '@/components/ui'
import { ago, elapsed, useNow, when } from '@/lib/time'
import { sentence } from '@/lib/plain'

const STATE_TONE: Record<RunState, 'success' | 'warning' | 'danger' | 'neutral' | 'info'> = {
  running: 'info',
  succeeded: 'success',
  skipped: 'warning',
  failed: 'danger',
  interrupted: 'warning',
}

export function RunStateBadge({ run }: { run: AgentRun }) {
  return <StatusDot tone={STATE_TONE[run.state] ?? 'neutral'}>{run.state_label}</StatusDot>
}

/** "Scheduled" or "Started by Priya" */
export function triggerLabel(run: AgentRun): string {
  if (run.trigger === 'manual') return run.triggered_by ? `Started by ${run.triggered_by}` : 'Started manually'
  if (run.trigger === 'schedule') return 'Scheduled'
  return run.trigger.charAt(0).toUpperCase() + run.trigger.slice(1)
}

/** Seconds without a progress report before the panel says so. */
const QUIET_AFTER_S = 120

/** The live view of an open run: current step, bar, elapsed, last update. */
export function LiveRun({ run, compact = false }: { run: AgentRun; compact?: boolean }) {
  const now = useNow(1000)
  const quietFor = run.last_update_at ? (now - new Date(run.last_update_at).getTime()) / 1000 : 0
  return (
    <div className={`live-run${compact ? ' is-compact' : ''}`}>
      <div className="live-run-head">
        <span className="live-dot" aria-hidden="true" />
        <span className="live-run-step">{run.step || 'Starting…'}</span>
        {run.step_percent !== null ? <span className="live-run-pct">{run.step_percent}%</span> : null}
      </div>
      <div
        className={`progress-track${run.step_percent === null ? ' is-indeterminate' : ''}`}
        role="progressbar"
        aria-label={run.step || 'Running'}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={run.step_percent ?? undefined}
      >
        <div className="progress-fill" style={run.step_percent === null ? undefined : { width: `${run.step_percent}%` }} />
      </div>
      <div className="live-run-meta">
        Running for {elapsed(run.started_at, now)} · {triggerLabel(run)}
        {run.last_update_at ? ` · last update ${ago(run.last_update_at, now)}` : ''}
      </div>
      {quietFor > QUIET_AFTER_S && !compact ? (
        <div className="live-run-quiet">
          No new progress for {Math.floor(quietFor / 60)} min. Some steps, such as AI writing, can take a while. A
          run that has not finished within the server time limit is marked as interrupted.
        </div>
      ) : null}
    </div>
  )
}

/** The steps a run went through, oldest first. */
export function RunTimeline({ run }: { run: AgentRun }) {
  if (run.timeline.length === 0) {
    return <p className="guide-text muted">No steps were recorded for this run.</p>
  }
  const live = run.state === 'running'
  return (
    <ol className="timeline">
      {run.timeline.map((step, index) => {
        const current = live && index === run.timeline.length - 1
        return (
          <li key={`${step.label}-${index}`} className={`timeline-item${current ? ' is-current' : ''}`}>
            <span className="timeline-mark" aria-hidden="true">
              {current ? <span className="live-dot" /> : <CheckIcon size={11} />}
            </span>
            <div className="timeline-body">
              <div className="timeline-text">{step.text}</div>
              <div className="timeline-meta">
                {step.at ? new Date(step.at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : ''}
                {step.total > 0 ? ` · ${step.done} of ${step.total}` : ''}
              </div>
            </div>
          </li>
        )
      })}
      {!live ? (
        <li className={`timeline-item is-end is-${run.state}`}>
          <span className="timeline-mark" aria-hidden="true" />
          <div className="timeline-body">
            <div className="timeline-text">{run.state_label}</div>
            <div className="timeline-meta">{run.finished_at ? when(run.finished_at) : ''}</div>
          </div>
        </li>
      ) : null}
    </ol>
  )
}

/** One run in full: outcome, timeline, and every change it applied or proposed. */
export function RunDrawer({
  slug,
  runId,
  onClose,
}: {
  slug: string
  runId: string
  onClose: () => void
}) {
  const { fromError } = useToasts()
  const [run, setRun] = useState<AgentRun | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer = 0
    const load = async () => {
      try {
        const fresh = await api.agentRun(slug, runId)
        if (cancelled) return
        setRun(fresh)
        // Follow a run that is still going.
        if (fresh.state === 'running') timer = window.setTimeout(() => void load(), 3000)
      } catch (caught) {
        if (!cancelled) fromError(caught)
      }
    }
    void load()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [slug, runId, fromError])

  const applied =
    run?.events.filter((e) => !e.awaiting_approval && !e.practice && !e.action.startsWith('could not finish')) ?? []
  const proposed = run?.events.filter((e) => e.awaiting_approval) ?? []
  const practised = run?.events.filter((e) => e.practice) ?? []

  return (
    <Drawer
      title={run ? `${run.agent_name} — run` : 'Run'}
      subtitle={run ? `${when(run.started_at)} · ${triggerLabel(run)}` : 'Loading…'}
      icon={<AgentIcon slug={slug} size={36} />}
      onClose={onClose}
    >
      {!run ? (
        <p className="guide-text muted">Loading…</p>
      ) : (
        <>
          <section className="run-outcome">
            <RunStateBadge run={run} />
            {run.state === 'running' ? (
              <LiveRun run={run} />
            ) : (
              <dl className="summary-list">
                <div>
                  <dt>Took</dt>
                  <dd>{run.duration_label || '—'}</dd>
                </div>
                <div>
                  <dt>Changes made</dt>
                  <dd>{run.actions_taken}</dd>
                </div>
                <div>
                  <dt>Sent for approval</dt>
                  <dd>{run.actions_queued}</dd>
                </div>
                {run.practice || run.actions_practised ? (
                  <div>
                    <dt>Practised, not sent</dt>
                    <dd>{run.actions_practised ?? 0}</dd>
                  </div>
                ) : null}
                {run.cost > 0 ? (
                  <div>
                    <dt>AI usage cost</dt>
                    <dd>{run.cost.toFixed(4)}</dd>
                  </div>
                ) : null}
              </dl>
            )}
            {run.summary ? (
              <div className={`banner ${run.state === 'skipped' ? 'is-warning' : 'is-info'}`}>
                <span>
                  <strong>{run.state === 'skipped' ? 'Why it did not go ahead: ' : 'Result: '}</strong>
                  {sentence(run.summary)}
                </span>
              </div>
            ) : null}
            {run.error ? (
              <div className="banner is-danger">
                <span>
                  <strong>What went wrong: </strong>
                  {sentence(run.error)}
                </span>
              </div>
            ) : null}
            {run.state === 'interrupted' ? (
              <div className="banner is-warning">
                This run stopped reporting before it finished — usually because the server restarted. It will not
                resume; the next scheduled run starts fresh.
              </div>
            ) : null}
          </section>

          <section className="guide-block">
            <div className="guide-title">What it did, step by step</div>
            <RunTimeline run={run} />
          </section>

          <section className="guide-block">
            <div className="guide-title">Changes from this run</div>
            {applied.length === 0 && proposed.length === 0 && practised.length === 0 ? (
              <p className="guide-text muted">
                {run.state === 'running' ? 'Nothing yet — changes appear here as the run applies or proposes them.' : 'This run made no changes.'}
              </p>
            ) : (
              <ul className="event-list">
                {practised.map((event, index) => (
                  <li key={`r${index}`} className="event-row">
                    <PracticeBadge />
                    <span className="event-text">{event.action.replace(/^practised /, '')}</span>
                  </li>
                ))}
                {proposed.map((event, index) => (
                  <li key={`p${index}`} className="event-row">
                    <span className="event-tag is-pending">For approval</span>
                    <span className="event-text">
                      {event.action.replace(/^proposed /, '').replace(/ for approval$/, '')}
                      {event.held?.length ? (
                        <ul className="practice-why">
                          {event.held.map((line) => (
                            <li key={line}>{line}</li>
                          ))}
                        </ul>
                      ) : null}
                    </span>
                  </li>
                ))}
                {applied.map((event, index) => (
                  <li key={`a${index}`} className="event-row">
                    <span className="event-tag is-applied">Applied</span>
                    <span className="event-text">{event.action}</span>
                  </li>
                ))}
              </ul>
            )}
            {proposed.length > 0 ? (
              <Link to="/approvals" className="panel-link">
                Review in Approvals →
              </Link>
            ) : null}
            {practised.length > 0 ? (
              <Link to={`/approvals?view=practice&agent=${encodeURIComponent(slug)}`} className="panel-link">
                See the full plans under Practice →
              </Link>
            ) : null}
          </section>
        </>
      )}
    </Drawer>
  )
}
