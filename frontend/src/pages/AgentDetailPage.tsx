/**
 * One agent: what it is doing right now, what it has done, and its setup.
 *
 * Refreshes every few seconds while a run is open, and slowly otherwise, so
 * the "Right now" panel follows a run as it happens. Every number shown is
 * from the agent's recorded runs.
 */
import { useCallback, useEffect, useState, type KeyboardEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api } from '@/api/client'
import type { AgentOut, AgentRun } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { AgentIcon } from '@/components/AgentIcon'
import { AgentResultsView } from '@/components/AgentResultsView'
import { AgentSetup } from '@/components/AgentSetup'
import { LocalRankHistory, VersionHistory } from '@/components/AuditHistory'
import { LiveRun, RunDrawer, RunStateBadge, RunTimeline, triggerLabel } from '@/components/RunViews'
import { AgentSafetyBar, PracticeSummaryCard } from '@/components/SafetyBits'
import { useToasts } from '@/components/Toasts'
import { CheckIcon } from '@/components/icons'
import { EmptyState, ErrorState, Loading, StatusDot } from '@/components/ui'
import { friendlyLoadError } from '@/hooks/useResource'
import { agentState } from '@/lib/agentState'
import { needUseLines } from '@/lib/connectorUse'
import { runOutcome, scheduleLabel } from '@/lib/plain'
import { softenErrorMessage } from '@/lib/softenError'
import { when } from '@/lib/time'

type Tab = 'results' | 'activity' | 'about'

/** Enter or Space on a focused row opens it, the same as a click. */
function onRowKey(event: KeyboardEvent<HTMLElement>, open: () => void) {
  if (event.target !== event.currentTarget) return
  if (event.key === 'Enter' || event.key === ' ') {
    // Space would otherwise scroll the page.
    event.preventDefault()
    open()
  }
}

function agentStatus(agent: AgentOut, live: AgentRun | undefined) {
  // A run seen in history counts as "running now" even if the agent row is
  // a moment behind; otherwise the shared vocabulary decides.
  if (live) return { tone: 'info' as const, label: 'Running now', detail: live.step || 'Starting' }
  return agentState(agent)
}

export function AgentDetailPage() {
  const { slug = '' } = useParams()
  const navigate = useNavigate()
  const { canWrite, canView } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const writable = canWrite('agents')

  const [agent, setAgent] = useState<AgentOut | null>(null)
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState<Tab>('results')
  const [openRun, setOpenRun] = useState<string | null>(null)
  const [setup, setSetup] = useState<Awaited<ReturnType<typeof api.agentOptions>> | null>(null)

  const load = useCallback(async () => {
    try {
      const [a, r] = await Promise.all([api.agent(slug), api.agentRuns(slug)])
      setAgent(a)
      setRuns(r)
      setError('')
    } catch (caught) {
      setError(friendlyLoadError(caught))
    }
  }, [slug])

  const live = runs.find((run) => run.state === 'running')

  useEffect(() => {
    void load()
  }, [load])

  // Follow a run closely while one is open; otherwise check now and then.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void load()
    }, live ? 3000 : 20000)
    return () => window.clearInterval(timer)
  }, [live, load])

  const act = async (action: () => Promise<unknown>) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusy(true)
    try {
      const result = (await action()) as { toast?: unknown } | null
      if (result && typeof result === 'object' && 'toast' in result) fromResult(result as { toast: null })
      await load()
      // A manual run is started, not awaited: look again shortly.
      window.setTimeout(() => void load(), 1500)
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const openSetup = async () => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    try {
      setSetup(await api.agentOptions())
    } catch (caught) {
      fromError(caught)
    }
  }

  if (error && !agent) return <ErrorState message={error} onRetry={() => void load()} />
  if (!agent) return <Loading label="Loading agent…" />

  const status = agentStatus(agent, live)
  const finished = runs.filter((run) => run.state !== 'running')
  const lastRun = finished[0]
  const count = (state: AgentRun['state']) => finished.filter((run) => run.state === state).length
  const applied = finished.reduce((sum, run) => sum + run.actions_taken, 0)
  const queued = finished.reduce((sum, run) => sum + run.actions_queued, 0)
  const isOn = agent.status === 'running'

  return (
    <>
      <Link to="/agents" className="back-link">
        ← All agents
      </Link>

      <header className="agent-hero">
        <AgentIcon slug={agent.slug} size={48} />
        <div className="agent-hero-text">
          <h1 className="agent-hero-name">{agent.name}</h1>
          <p className="agent-hero-summary">{agent.summary || agent.description}</p>
          <div className="agent-hero-status">
            {live ? (
              <span className="state-pill is-live">
                <span className="live-dot" aria-hidden="true" />
                Running now
              </span>
            ) : (
              <StatusDot tone={status.tone}>{status.label}</StatusDot>
            )}
            {!live && status.detail ? <span className="agent-hero-status-detail">{status.detail}</span> : null}
          </div>
        </div>
        <div className="agent-hero-actions">
          {!agent.configured ? (
            <button type="button" className="btn btn-primary" disabled={!writable} onClick={() => void openSetup()}>
              Set up
            </button>
          ) : (
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={!writable || busy || Boolean(live)}
                title={live ? 'A run is already in progress' : undefined}
                onClick={() => void act(() => api.runAgent(agent.slug))}
              >
                Run now
              </button>
              <button
                type="button"
                className={`btn ${isOn ? 'btn-secondary' : 'btn-primary'}`}
                disabled={!writable || busy}
                onClick={() => void act(() => (isOn ? api.pauseAgent(agent.slug) : api.resumeAgent(agent.slug)))}
              >
                {isOn ? 'Pause' : agent.status === 'error' ? 'Restart' : 'Turn on'}
              </button>
              <button type="button" className="btn btn-secondary" disabled={!writable} onClick={() => void openSetup()}>
                Settings
              </button>
            </>
          )}
        </div>
      </header>

      {agent.status === 'error' && agent.last_error ? (
        <div className="banner is-danger" role="alert">
          <span>
            <strong>It stopped because: </strong>
            {softenErrorMessage(agent.last_error, 'Something went wrong during its last run. Try running it again.')}
          </span>
        </div>
      ) : null}

      <AgentSafetyBar agent={agent} writable={writable} onChanged={setAgent} />

      <section className="panel now-panel" aria-label="Right now">
        <div className="panel-head">
          <div className="panel-title">Right now</div>
          {live ? (
            <button type="button" className="panel-link as-button" onClick={() => setOpenRun(live.id)}>
              Open this run →
            </button>
          ) : null}
        </div>
        <div className="now-body">
          {live ? (
            <div className="now-live">
              <LiveRun run={live} />
              <div className="now-timeline">
                <div className="guide-title">Steps so far</div>
                <RunTimeline run={live} />
              </div>
            </div>
          ) : (
            <div className="now-idle">
              <p className="now-idle-line">
                {!agent.configured
                  ? 'Not set up yet, so it does not run.'
                  : isOn
                    ? agent.next_run === 'ongoing'
                      ? 'Not running this moment. It checks continuously.'
                      : agent.next_run === 'due'
                        ? 'Not running this moment. Its next run is due now.'
                        : agent.next_run === '—' || !agent.next_run
                          ? 'Not running this moment. Its next run has not been scheduled yet.'
                          : `Not running this moment. Next run in ${agent.next_run} (${scheduleLabel(agent.schedule).toLowerCase()}).`
                    : 'Paused — it will not run until you turn it on. You can still run it once with Run now.'}
              </p>
              {lastRun ? (
                <button type="button" className="last-run" onClick={() => setOpenRun(lastRun.id)}>
                  <span className="last-run-head">
                    Last run · {when(lastRun.started_at)} · <RunStateBadge run={lastRun} />
                  </span>
                  <span className="last-run-text">{runOutcome(lastRun) || 'No summary recorded.'}</span>
                </button>
              ) : null}
              {lastRun ? (
                <button type="button" className="panel-link as-button" onClick={() => setTab('results')}>
                  See everything it has produced →
                </button>
              ) : agent.configured ? (
                <p className="guide-text muted">It has not run yet.</p>
              ) : null}
            </div>
          )}
        </div>
      </section>

      <div className="tabs tabs-underline" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'results'}
          className={`tab${tab === 'results' ? ' is-active' : ''}`}
          onClick={() => setTab('results')}
        >
          Results
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'activity'}
          className={`tab${tab === 'activity' ? ' is-active' : ''}`}
          onClick={() => setTab('activity')}
        >
          Run history <span className="tab-count">{runs.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'about'}
          className={`tab${tab === 'about' ? ' is-active' : ''}`}
          onClick={() => setTab('about')}
        >
          About & setup
        </button>
      </div>

      {tab === 'results' ? (
        <>
          {agent.spending ? (
            <PracticeSummaryCard slug={agent.slug} active={Boolean(agent.practice_source)} />
          ) : null}
          <AgentResultsView slug={agent.slug} refreshKey={`${runs[0]?.id ?? ''}:${runs[0]?.state ?? ''}`} />
          {/* Ranking history is SEO data (local business agents write into SEO). */}
          {agent.slug === 'local_search_tracker' && canView('seo') ? (
            <>
              <LocalRankHistory />
              {/* Read-only: ranking checks are compared, never restored. */}
              <VersionHistory
                kind="local_ranks"
                title="Compare this ranking check with an earlier one"
                description="New: a search phrase checked for the first time. Fixed: a phrase that moved up or entered the results. Worse: a phrase that moved down or dropped out."
              />
            </>
          ) : null}
        </>
      ) : tab === 'activity' ? (
        <>
          {finished.length > 0 ? (
            <div className="kpi-grid run-stats">
              <div className="kpi">
                <div className="kpi-label">Completed</div>
                <div className="kpi-value">{count('succeeded')}</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Did not go ahead</div>
                <div className="kpi-value">{count('skipped')}</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Failed or interrupted</div>
                <div className="kpi-value">{count('failed') + count('interrupted')}</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Changes made / sent for approval</div>
                <div className="kpi-value">
                  {applied}
                  <span className="kpi-total"> / {queued}</span>
                </div>
              </div>
              <p className="run-stats-note">Across the {runs.length} most recent runs.</p>
            </div>
          ) : null}

          {runs.length === 0 ? (
            <EmptyState
              title="No runs yet"
              description={
                agent.configured
                  ? 'Runs appear here as soon as it starts, either on its schedule or when you press Run now.'
                  : 'Set it up first. Its runs will be listed here.'
              }
              action={
                !agent.configured && writable ? (
                  <button type="button" className="btn btn-primary" onClick={() => void openSetup()}>
                    Set up
                  </button>
                ) : undefined
              }
            />
          ) : (
            <div className="panel">
              <div className="table-wrap">
                <table className="table data-table">
                  <thead>
                    <tr>
                      <th>Started</th>
                      <th>Result</th>
                      <th>What happened</th>
                      <th>Changes</th>
                      <th>Took</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => (
                      <tr
                        key={run.id}
                        className="is-clickable"
                        tabIndex={0}
                        aria-label={`Open the run started ${when(run.started_at)}`}
                        onClick={() => setOpenRun(run.id)}
                        onKeyDown={(event) => onRowKey(event, () => setOpenRun(run.id))}
                      >
                        <td data-label="Started" className="nowrap">
                          <div>{when(run.started_at)}</div>
                          <div className="cell-sub">{triggerLabel(run)}</div>
                        </td>
                        <td data-label="Result">
                          <RunStateBadge run={run} />
                        </td>
                        <td data-label="What happened" className="cell-wide">
                          <div className={run.error ? 'cell-error' : 'cell-clamp'}>
                            {run.state === 'running'
                              ? run.step || 'Starting…'
                              : runOutcome(run) || '—'}
                          </div>
                        </td>
                        <td data-label="Changes" className="nowrap">
                          {run.actions_taken || run.actions_queued ? (
                            <>
                              {run.actions_taken} made
                              {run.actions_queued ? <div className="cell-sub">{run.actions_queued} for approval</div> : null}
                            </>
                          ) : (
                            <span className="muted">None</span>
                          )}
                        </td>
                        <td data-label="Took" className="nowrap muted">
                          {run.duration_label || '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="about-grid">
          <section className="panel about-panel">
            <div className="guide-title">What it does</div>
            <p className="guide-text">{agent.summary || agent.description}</p>
            {agent.effect ? (
              <>
                <div className="guide-title">{agent.makes_changes ? 'What it changes' : 'Changes to your site'}</div>
                <p className="guide-text">{agent.effect}</p>
              </>
            ) : null}
            {agent.approval_behaviour ? (
              <>
                <div className="guide-title">Approval</div>
                <p className="guide-text">{agent.approval_behaviour}</p>
              </>
            ) : null}
          </section>
          <section className="panel about-panel">
            <div className="about-needs-head">
              <div className="guide-title">What it needs</div>
              {writable ? (
                <button type="button" className="link-button" onClick={() => void openSetup()}>
                  Edit
                </button>
              ) : null}
            </div>
            <ul className="about-needs">
              {agent.needs.map((need) => (
                <li key={need.key} className={need.satisfied ? 'is-done' : need.optional ? 'is-optional' : 'is-missing'}>
                  <span className="about-need-mark">{need.satisfied ? <CheckIcon size={11} /> : need.optional ? '+' : '!'}</span>
                  <span>
                    {need.label}
                    {need.optional ? ' (optional)' : ''}
                    {need.satisfied && need.kind === 'integration' && needUseLines(need).length ? (
                      needUseLines(need).map((line) => (
                        <span
                          key={line.key}
                          className={`about-need-sub${line.stale ? ' is-attention' : ''}`}
                        >
                          {line.text}
                        </span>
                      ))
                    ) : (
                      <span className="about-need-sub">
                        {need.satisfied
                          ? need.kind === 'integration'
                            ? need.key === 'ai_model'
                              ? need.note ||
                                `Uses ${need.options.find((o) => o.slug === agent.llm_connector)?.name ?? 'the AI service chosen in its settings'}.`
                              : `Connected: ${need.options.filter((o) => o.connected).map((o) => o.name).join(', ')}`
                            : 'Done'
                          : need.kind === 'integration'
                            ? 'Not connected'
                            : need.note}
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
            {agent.configured ? (
              <dl className="summary-list">
                <div>
                  <dt>Runs</dt>
                  <dd>{scheduleLabel(agent.schedule)}</dd>
                </div>
                {agent.makes_changes ? (
                  <div>
                    <dt>Most automatic changes per day</dt>
                    <dd>{agent.max_actions_per_day}</dd>
                  </div>
                ) : null}
              </dl>
            ) : null}
            <button type="button" className="btn btn-secondary" disabled={!writable} onClick={() => void openSetup()}>
              {agent.configured ? 'Change settings' : 'Set up'}
            </button>
          </section>
        </div>
      )}

      {/* Settings versions are kept under the agent's record id. Without one
          the list would not be limited to this agent, so it is not shown. */}
      {tab === 'about' && agent.id ? (
        <div style={{ marginTop: 'var(--space-4)' }}>
          <VersionHistory
            kind="agent_config"
            subject={agent.id}
            title="Settings history"
            description={`A numbered version of ${agent.name}'s settings is kept each time they change. Open one to see what it held, compare any two${writable ? ', or restore an earlier one' : ''}.`}
            refreshKey={`${agent.configured}:${agent.config_summary}:${agent.schedule}:${agent.max_actions_per_day}`}
            onRestored={() => {
              void load()
            }}
          />
        </div>
      ) : null}

      {openRun ? <RunDrawer slug={agent.slug} runId={openRun} onClose={() => setOpenRun(null)} /> : null}

      {setup ? (
        <AgentSetup
          key={agent.slug}
          agent={agent}
          schedules={setup.schedules}
          channels={setup.notify_channels}
          llmConnectors={setup.llm_connectors}
          onClose={() => {
            setSetup(null)
            void load()
          }}
          onSaved={async () => {
            setSetup(null)
            await load()
            window.setTimeout(() => void load(), 1500)
          }}
          onOpenAgent={(next) => {
            setSetup(null)
            navigate(`/agents/${next}`)
          }}
        />
      ) : null}
    </>
  )
}
