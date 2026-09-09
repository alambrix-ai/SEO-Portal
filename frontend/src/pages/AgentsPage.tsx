/**
 * AI Agents hub — the fleet, its switches, and the Configure dialog.
 *
 * Every card's status, autonomy label and countdown come from the API, so what
 * is shown is the agent's real state rather than an optimistic guess.
 */
import { useEffect, useRef, useState } from 'react'

import { api } from '@/api/client'
import type { AgentOut, NotifyChannelOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { AgentIcon } from '@/components/AgentIcon'
import { CardSection } from '@/components/CardSection'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  Dialog,
  ErrorState,
  Field,
  Loading,
  Segmented,
  Tag,
  statusTone,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

/** Which channels cannot be picked, and what to connect for them. */
function unavailableHint(channels: NotifyChannelOut[]): string {
  const blocked = channels.filter((channel) => !channel.available)
  if (blocked.length === 0) return ''
  return `${blocked.map((channel) => channel.value).join(' and ')} need a connector first.`
}

/** The sections, worst-first.
 *
 * An agent that was working and has stopped is the most urgent thing on the
 * screen, which is why it sorts above the ones that are working rather than
 * below them. `group` is computed on the server by the same function that
 * orders the list. */
const AGENT_SECTIONS: {
  key: string
  title: string
  description?: string
  tone?: 'plain' | 'attention'
}[] = [
  {
    key: 'attention',
    title: 'Needs attention',
    description: 'These were running and have stopped.',
    tone: 'attention',
  },
  {
    key: 'running',
    title: 'Active',
    description:
      'Working on their own schedule, on the server — they keep going when ' +
      'you close this page, until you pause them.',
  },
  {
    key: 'ready',
    title: 'Configured, not started',
    description: 'Set up and one click from live.',
  },
  {
    key: 'idle',
    title: 'Not set up yet',
    description: 'Nothing here touches your site until you configure and start it.',
  },
]

export function AgentsPage() {
  const { canWrite, session } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  // Re-fetched whenever the workspace-wide autonomy switch in the header
  // changes. That control writes to the server and refreshes the session, but
  // the cards are a separate request — without this dependency they kept
  // showing the old autonomy labels until a navigation, which read as the
  // toggle doing nothing at all.
  const globalAutonomy = session?.organization.global_autonomy
  const { data, loading, error, reload } = useResource(
    () => api.agents(),
    [globalAutonomy],
  )
  const options = useResource(() => api.agentOptions())

  const [configuring, setConfiguring] = useState<AgentOut | null>(null)
  const [busySlug, setBusySlug] = useState<string | null>(null)

  const writable = canWrite('agents')

  const openConfigure = (agent: AgentOut) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setConfiguring(agent)
  }

  // While any agent has a pass open, refresh often enough that the step
  // line moves. Stops the moment nothing is running, so an idle fleet costs
  // no requests at all.
  const anyBusy = (data ?? []).some((agent) => agent.busy)
  useEffect(() => {
    if (!anyBusy) return
    const timer = window.setInterval(() => {
      if (!document.hidden) void reload()
    }, 4000)
    return () => window.clearInterval(timer)
  }, [anyBusy, reload])

  // Timers for the follow-up reloads after a run is started, cleared on
  // unmount so a reload cannot fire against a page that has gone.
  const followUps = useRef<number[]>([])
  useEffect(
    () => () => {
      followUps.current.forEach(window.clearTimeout)
    },
    [],
  )

  const act = async (
    slug: string,
    action: () => Promise<unknown>,
    { follow = false }: { follow?: boolean } = {},
  ) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusySlug(slug)
    try {
      const result = (await action()) as { toast?: unknown } | null
      if (result && typeof result === 'object' && 'toast' in result) {
        fromResult(result as { toast: null })
      }
      await reload()
      if (follow) {
        // The pass runs on the server and this response only said it began,
        // so the first reload above sees the state from before it started.
        followUps.current.forEach(window.clearTimeout)
        followUps.current = [3000, 9000, 20000].map((delay) =>
          window.setTimeout(() => {
            void reload()
          }, delay),
        )
      }
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusySlug(null)
    }
  }

  if (loading && !data) return <Loading label="Loading the fleet…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <p className="page-intro">
        {data.length} agents run the SEO/AEO and programmatic ad pipeline end to end.
        Configure an agent, then start it — nothing runs against your site until you
        have set its schedule and daily cap. Once running, toggle it between autonomous
        execution and human-in-the-loop review.
      </p>

      {AGENT_SECTIONS.map(({ key, title, description, tone }) => {
        const rows = data.filter((agent) => agent.group === key)
        if (rows.length === 0) return null
        return (
          <CardSection
            key={key}
            title={title}
            count={rows.length}
            description={description}
            tone={tone}
          >
            <div className="grid-cards">
              {rows.map((agent) => {
                const busy = busySlug === agent.slug
                const disabled = !writable || busy
                // An agent that has never been started and never configured has no
                // state worth reporting: its status is "paused", its next run is a
                // dash and its metric line is a placeholder. Showing all of that
                // makes eleven identical cards of nothing and buries the one thing
                // that matters, which is how to set it up. So the operational block
                // appears once there is something true to say — the agent is live,
                // it has been configured, or it has errored.
                const live = agent.status !== 'paused' || agent.configured
                const paused = agent.status !== 'running'
                // Configured and stopped: starting it is the obvious next move, so
                // that button carries the emphasis.
                const startable = paused && agent.configured
                return (
                  <Blueprint key={agent.slug} className="card elev-sm agent-card">
                    {/* The mark sits beside the kicker rather than above it:
                  twelve cards in a grid are scanned by shape first, and an
                  icon on its own line pushes every title down a row. */}
                    <div className="card-head">
                      <AgentIcon slug={agent.slug} />
                      <div className="card-head-text">
                        <div className="card-kicker">{agent.category}</div>
                        <div className="card-title">{agent.name}</div>
                      </div>
                    </div>
                    <p className="card-body">{agent.description}</p>

                    {/* The state block. Everything a card says about itself lives
                  here, so every card has the same three regions — identity,
                  state, actions — and the action row lands at the same height
                  whether the agent has one line to report or four. */}
                    <div className="agent-state">
                      {live ? (
                        <>
                          <div className="row">
                            <Tag tone={statusTone(agent.status)}>
                              {agent.status_label}
                            </Tag>
                            {/* The autonomy switch, not a label plus a button that
                          said the same thing in the imperative. Two controls
                          for one setting is what made this card read as four
                          buttons of unrelated things. */}
                            <Segmented
                              name={`autonomy-${agent.slug}`}
                              value={agent.autonomy ? 'auto' : 'review'}
                              options={[
                                { value: 'auto', label: 'Autonomous' },
                                { value: 'review', label: 'Review' },
                              ]}
                              disabled={disabled}
                              onChange={(next) =>
                                void act(agent.slug, () =>
                                  api.setAgentAutonomy(agent.slug, next === 'auto'),
                                )
                              }
                            />
                          </div>

                    {agent.busy ? (
                      <div className="agent-progress">
                        <div className="agent-progress-step">
                          <span className="agent-progress-dot" />
                          {agent.step || 'Working'}
                          {agent.step_percent === null ? null : (
                            <span className="agent-progress-pct">
                              {agent.step_percent}%
                            </span>
                          )}
                        </div>
                        {/* Only where there is a real total behind it. */}
                        {agent.step_percent === null ? null : (
                          <div
                            className="agent-progress-track"
                            role="progressbar"
                            aria-valuenow={agent.step_percent}
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-label={agent.step}
                          >
                            <div
                              className="agent-progress-fill"
                              style={{ width: `${agent.step_percent}%` }}
                            />
                          </div>
                        )}
                        <div className="card-meta">{agent.busy_for}</div>
                      </div>
                    ) : (
                      <>
                        <div className="card-meta">
                          {agent.metric_label || 'No activity yet'} · next run{' '}
                          {agent.next_run}
                        </div>
                        {/* What the last pass actually did, in its own words.
                            A status without an outcome is what made an agent
                            that had skipped six times look healthy. */}
                        {agent.last_run_summary ? (
                          <div
                            className={
                              agent.last_run_status === 'skipped'
                                ? 'agent-last-run is-skipped'
                                : 'agent-last-run'
                            }
                          >
                            {agent.last_run_status === 'skipped'
                              ? 'Last run did not proceed: '
                              : 'Last run: '}
                            {agent.last_run_summary}
                          </div>
                        ) : null}
                      </>
                    )}

                          {agent.configured ? (
                            <div className="card-meta">{agent.config_summary}</div>
                          ) : null}
                          {agent.last_error ? (
                            <div className="agent-error">{agent.last_error}</div>
                          ) : null}
                        </>
                      ) : (
                        <div className="card-meta">Not configured yet.</div>
                      )}
                    </div>

                    <div className="agent-actions">
                      {/* Configure leads when it is the prerequisite. An agent
                    cannot be started until somebody has looked at its
                    settings, so that button is offered first and styled as
                    the action to take. */}
                      {!agent.configured ? (
                        <button
                          type="button"
                          className="btn btn-primary"
                          disabled={disabled}
                          onClick={() => openConfigure(agent)}
                        >
                          Configure
                        </button>
                      ) : null}

                      <button
                        type="button"
                        className={`btn ${startable ? 'btn-primary' : 'btn-secondary'}`}
                        disabled={disabled || (paused && !agent.configured)}
                        // Shown rather than hidden, so the sequence is discoverable:
                        // a missing button teaches nothing, a disabled one with a
                        // reason teaches the rule once.
                        title={
                          paused && !agent.configured
                            ? 'Configure it first — schedule, scope and daily cap'
                            : undefined
                        }
                        onClick={() =>
                          void act(agent.slug, () =>
                            agent.status === 'running'
                              ? api.pauseAgent(agent.slug)
                              : api.resumeAgent(agent.slug),
                          )
                        }
                      >
                        {agent.status === 'running' ? 'Pause' : 'Start'}
                      </button>

                      {agent.configured ? (
                        <>
                          <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={disabled}
                            onClick={() => openConfigure(agent)}
                          >
                            Configure
                          </button>
                          {/* A manual run is not a preview — it publishes and spends
                        like any other run — so it is gated on configuration
                        with everything else. */}
                          <button
                            type="button"
                            className="btn btn-ghost"
                            disabled={disabled}
                            onClick={() =>
                              void act(agent.slug, () => api.runAgent(agent.slug), {
                                follow: true,
                              })
                            }
                          >
                            {busy ? 'Starting…' : 'Run now'}
                          </button>
                        </>
                      ) : null}
                    </div>
                  </Blueprint>
                )
              })}
            </div>
          </CardSection>
        )
      })}

      {configuring ? (
        <ConfigureDialog
          agent={configuring}
          schedules={options.data?.schedules ?? []}
          channels={options.data?.notify_channels ?? []}
          onClose={() => setConfiguring(null)}
          onSaved={async () => {
            setConfiguring(null)
            await reload()
          }}
        />
      ) : null}
    </>
  )
}

function ConfigureDialog({
  agent,
  schedules,
  channels,
  onClose,
  onSaved,
}: {
  agent: AgentOut
  schedules: string[]
  channels: NotifyChannelOut[]
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { push, fromError } = useToasts()
  const [schedule, setSchedule] = useState(agent.schedule)
  const [scope, setScope] = useState(agent.scope)
  // A channel the workspace can no longer deliver falls back to None, so
  // the dialog opens in a state that will actually save. The notice below
  // says what happened rather than changing it quietly.
  const deadChannel = channels.find(
    (channel) => channel.value === agent.notify_channel && !channel.available,
  )
  const [notify, setNotify] = useState(
    deadChannel ? 'None' : agent.notify_channel,
  )
  const [maxActions, setMaxActions] = useState(String(agent.max_actions_per_day))
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState(false)

  const remove = async () => {
    // Confirmed, because it also stops the agent — an irreversible-feeling
    // action deserves one question, and the question names the consequence.
    if (
      !window.confirm(
        `Remove the configuration for ${agent.name}? It will stop, and you will ` +
          'have to configure it again before it can run. Its run history is kept.',
      )
    ) {
      return
    }
    setRemoving(true)
    try {
      await api.resetAgentConfig(agent.slug)
      push(`${agent.name} configuration removed`, 'info')
      await onSaved()
    } catch (caught) {
      fromError(caught)
    } finally {
      setRemoving(false)
    }
  }

  const save = async () => {
    const parsed = Number(maxActions)
    if (!Number.isFinite(parsed) || parsed < 1) {
      push('Maximum actions must be at least 1', 'warning')
      return
    }
    setBusy(true)
    try {
      const saved = await api.configureAgent(agent.slug, {
        schedule,
        scope,
        notify_channel: notify,
        max_actions_per_day: Math.floor(parsed),
      })
      push('Configuration saved', 'success')
      // A warning means it saved and will not do everything the operator
      // probably expects — a notify channel with nothing behind it, say.
      // Shown after the success so the order matches what happened. Errors
      // never reach here: the API refuses those, and fromError shows why.
      for (const warning of saved.warnings ?? []) {
        push(warning, 'warning')
      }
      await onSaved()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      title={`Configure ${agent.name}`}
      onClose={onClose}
      actions={
        <>
          {/* The counterpart of saving. Without it the dialog is a one-way
              door: you can change a schedule but never take back the decision
              that let the agent run, which makes the configuration gate feel
              like a trap rather than a switch. Only offered once there is a
              configuration to remove. */}
          {agent.configured ? (
            <button
              type="button"
              className="btn btn-ghost danger"
              disabled={busy || removing}
              onClick={() => void remove()}
            >
              {removing ? 'Removing…' : 'Remove configuration'}
            </button>
          ) : null}
          <div className="dialog-actions-spacer" />
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void save()}
          >
            {busy ? 'Saving…' : 'Save configuration'}
          </button>
        </>
      }
    >
      <Field label="Run schedule" htmlFor="cfg-schedule" required>
        <select
          id="cfg-schedule"
          className="input"
          value={schedule}
          onChange={(event) => setSchedule(event.target.value)}
        >
          {(schedules.length ? schedules : [agent.schedule]).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </Field>

      {/* Not required, and deliberately so: blank means the whole site,
          which is the right default. The placeholder comes from the agent
          itself — scope means a path here, a list of domains there, and a
          set of schema types somewhere else. */}
      <Field
        label="Scope / target"
        htmlFor="cfg-scope"
        hint="Leave blank to let the agent decide its own scope."
      >
        <input
          id="cfg-scope"
          className="input"
          placeholder={agent.scope_placeholder || 'Leave blank for everything'}
          value={scope}
          onChange={(event) => setScope(event.target.value)}
        />
      </Field>

      <Field
        label="Notify channel on action"
        htmlFor="cfg-notify"
        required
        hint={
          // The reason for whichever channel is selected, or for the ones
          // that cannot be picked, so the dialog explains itself without a
          // round trip.
          channels.find((channel) => channel.value === notify)?.reason ||
          unavailableHint(channels)
        }
      >
        <select
          id="cfg-notify"
          className="input"
          value={notify}
          onChange={(event) => setNotify(event.target.value)}
        >
          {(channels.length
            ? channels
            : [{ value: agent.notify_channel, available: true, reason: '' }]
          ).map((option) => (
            <option
              key={option.value}
              value={option.value}
              // Shown and unselectable rather than hidden: a missing choice
              // teaches nothing about why it is missing.
              disabled={!option.available}
            >
              {option.value}
              {option.available ? '' : ' — needs a connector'}
            </option>
          ))}
        </select>
      </Field>

      {deadChannel ? (
        <div className="notice">
          This agent was set to notify by {deadChannel.value}, but nothing is
          connected that can send it — so those notifications were never
          arriving. Reset to None. {deadChannel.reason}
        </div>
      ) : null}

      <Field
        label="Max autonomous actions / day"
        htmlFor="cfg-max"
        required
        hint="Actions beyond this are held until the next day rather than queued."
      >
        <input
          id="cfg-max"
          className="input"
          type="number"
          min={1}
          value={maxActions}
          onChange={(event) => setMaxActions(event.target.value)}
        />
      </Field>
    </Dialog>
  )
}
