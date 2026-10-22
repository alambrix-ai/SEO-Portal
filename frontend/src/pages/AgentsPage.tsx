/**
 * Agents, grouped by goal, with one clear state each.
 *
 * "Running now" is shown only while a pass is actually in progress; an agent
 * that is switched on and idle is "On" with its next run. See
 * lib/agentState.ts for the one place that decides which is which.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '@/api/client'
import type { AgentOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { AgentIcon } from '@/components/AgentIcon'
import { AgentSetup } from '@/components/AgentSetup'
import { Menu } from '@/components/Menu'
import { PracticeBadge } from '@/components/SafetyBits'
import { useToasts } from '@/components/Toasts'
import { MoreIcon, SearchIcon } from '@/components/icons'
import { ListPager } from '@/components/ListPager'
import { EmptyState, ErrorState, Loading, PageHeader, StatusDot } from '@/components/ui'
import { usePagedList } from '@/hooks/usePagedList'
import { useResource } from '@/hooks/useResource'
import { STATE_FILTERS, STATE_ORDER, agentState, filterKey } from '@/lib/agentState'
import type { AgentStateKey } from '@/lib/agentState'
import { AGENT_GROUPS } from '@/lib/plain'
import { softenErrorMessage } from '@/lib/softenError'

export function AgentsPage() {
  const { canWrite, session } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const navigate = useNavigate()
  const globalAutonomy = session?.organization.global_autonomy
  const { data, loading, error, reload } = useResource((signal) => api.agents(signal), [globalAutonomy], 'agents')
  const [options, setOptions] = useState<Awaited<ReturnType<typeof api.agentOptions>> | null>(null)
  const [setupSlug, setSetupSlug] = useState<string | null>(null)
  const [busySlug, setBusySlug] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<AgentStateKey | 'all'>('all')

  const writable = canWrite('agents')

  const openSetup = async (slug: string) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    try {
      setOptions(await api.agentOptions())
      setSetupSlug(slug)
    } catch (caught) {
      fromError(caught)
    }
  }

  // Refresh quickly while anything is running now, so progress moves.
  const anyBusy = (data ?? []).some((agent) => agent.busy)
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void reload()
    }, anyBusy ? 4000 : 30000)
    return () => window.clearInterval(timer)
  }, [anyBusy, reload])

  const followUps = useRef<number[]>([])
  useEffect(() => () => followUps.current.forEach(window.clearTimeout), [])
  const scheduleFollowUps = () => {
    followUps.current.forEach(window.clearTimeout)
    followUps.current = [1500, 5000, 12000].map((delay) => window.setTimeout(() => void reload(), delay))
  }

  const act = async (slug: string, action: () => Promise<unknown>, follow = false) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusySlug(slug)
    try {
      const result = (await action()) as { toast?: unknown } | null
      if (result && typeof result === 'object' && 'toast' in result) fromResult(result as { toast: null })
      await reload()
      if (follow) scheduleFollowUps()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusySlug(null)
    }
  }

  const withState = useMemo(() => (data ?? []).map((agent) => ({ agent, state: agentState(agent) })), [data])

  const counts = useMemo(() => {
    const out: Record<string, number> = { all: withState.length }
    for (const { state } of withState) out[filterKey(state)] = (out[filterKey(state)] ?? 0) + 1
    return out
  }, [withState])

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return withState
      .filter(({ state }) => filter === 'all' || filterKey(state) === filter)
      .filter(
        ({ agent }) =>
          !needle ||
          agent.name.toLowerCase().includes(needle) ||
          (agent.summary || agent.description).toLowerCase().includes(needle),
      )
      .sort((a, b) => STATE_ORDER.indexOf(a.state.key) - STATE_ORDER.indexOf(b.state.key))
  }, [withState, filter, query])

  const pager = usePagedList(visible, { storageKey: 'agents', resetKey: `${filter}|${query}` })

  if (loading && !data) return <Loading label="Loading agents…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const setupAgent = setupSlug ? data.find((a) => a.slug === setupSlug) ?? null : null
  const known = new Set(AGENT_GROUPS.map((g) => g.category.toLowerCase()))
  const groups = [
    ...AGENT_GROUPS,
    ...[...new Set(data.map((a) => a.category))]
      .filter((c) => !known.has(c.toLowerCase()))
      .map((c) => ({ category: c, title: c, description: '' })),
  ]

  return (
    <>
      <PageHeader
        title="Agents"
        description="Agents work for you on a schedule. Open one to see what it is doing, what it has produced, and its settings."
        actions={
          <label className="search-input">
            <SearchIcon size={16} />
            <input
              type="search"
              placeholder="Search agents"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Search agents"
            />
          </label>
        }
      />

      <div className="state-strip" role="tablist" aria-label="Filter agents by state">
        {STATE_FILTERS.map((option) => {
          const count = counts[option.key] ?? 0
          if (option.key !== 'all' && count === 0) return null
          return (
            <button
              key={option.key}
              type="button"
              role="tab"
              aria-selected={filter === option.key}
              className={`state-chip is-${option.key}${filter === option.key ? ' is-active' : ''}`}
              onClick={() => setFilter(option.key)}
            >
              {option.key === 'running_now' ? <span className="live-dot" aria-hidden="true" /> : null}
              <span className="state-chip-count">{count}</span>
              <span className="state-chip-label">{option.label}</span>
            </button>
          )
        })}
      </div>

      {groups.map((group) => {
        const rows = pager.slice.filter(
          ({ agent }) => agent.category.toLowerCase() === group.category.toLowerCase(),
        )
        if (rows.length === 0) return null
        return (
          <section key={group.category} className="agent-group">
            <div className="agent-group-head">
              <h2 className="agent-group-title">{group.title}</h2>
              {group.description ? <p className="agent-group-desc">{group.description}</p> : null}
            </div>
            <ul className="agent-list">
              {rows.map(({ agent, state }) => (
                <AgentRow
                  key={agent.slug}
                  agent={agent}
                  state={state}
                  busy={busySlug === agent.slug}
                  writable={writable}
                  onOpen={() => navigate(`/agents/${agent.slug}`)}
                  onSetup={() => void openSetup(agent.slug)}
                  onToggle={() =>
                    void act(agent.slug, () =>
                      agent.status === 'running' ? api.pauseAgent(agent.slug) : api.resumeAgent(agent.slug),
                    )
                  }
                  onRunNow={() => void act(agent.slug, () => api.runAgent(agent.slug), true)}
                />
              ))}
            </ul>
          </section>
        )
      })}

      {visible.length > 0 ? (
        <ListPager
          total={pager.total}
          from={pager.from}
          to={pager.to}
          page={pager.page}
          pageCount={pager.pageCount}
          pageSize={pager.pageSize}
          onPage={pager.setPage}
          onPageSize={pager.setPageSize}
          label="Agent pages"
        />
      ) : null}

      {data.length === 0 ? (
        <EmptyState
          title="No agents in this workspace yet"
          description="Agents are added when a workspace is created. If this list stays empty, ask a workspace admin to check that the Agents module is turned on."
        />
      ) : visible.length === 0 ? (
        <EmptyState
          title="No agents match"
          description={
            query.trim()
              ? `Nothing matches “${query.trim()}”${filter === 'all' ? '' : ' with this filter'}. Try a shorter word, or show every agent.`
              : 'No agents are in this state right now. Show every agent to see the rest.'
          }
          action={
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setQuery('')
                setFilter('all')
              }}
            >
              Show all agents
            </button>
          }
        />
      ) : null}

      {setupAgent ? (
        <AgentSetup
          key={setupAgent.slug}
          agent={setupAgent}
          schedules={options?.schedules ?? []}
          channels={options?.notify_channels ?? []}
          llmConnectors={options?.llm_connectors ?? []}
          onClose={() => {
            setSetupSlug(null)
            void reload()
          }}
          onSaved={async (started) => {
            setSetupSlug(null)
            await reload()
            if (started) scheduleFollowUps()
          }}
          onOpenAgent={(slug) => {
            void reload()
            void openSetup(slug)
          }}
        />
      ) : null}
    </>
  )
}

function AgentRow({
  agent,
  state,
  busy,
  writable,
  onOpen,
  onSetup,
  onToggle,
  onRunNow,
}: {
  agent: AgentOut
  state: ReturnType<typeof agentState>
  busy: boolean
  writable: boolean
  onOpen: () => void
  onSetup: () => void
  onToggle: () => void
  onRunNow: () => void
}) {
  const isOn = agent.status === 'running'
  // A stopped agent's detail is its last error; never show it raw.
  const detail =
    state.tone === 'danger' ? softenErrorMessage(state.detail, 'It stopped. Open it to see why.') : state.detail
  return (
    <li className={`agent-row is-${state.key}`}>
      <button type="button" className="agent-row-main" onClick={onOpen}>
        <AgentIcon slug={agent.slug} size={36} />
        <span className="agent-row-text">
          <span className="agent-row-name">
            {agent.name}
            {agent.practice_source ? <> <PracticeBadge title={agent.practice_note} /></> : null}
          </span>
          <span className="agent-row-summary">{agent.summary || agent.description}</span>
        </span>
      </button>

      <div className="agent-row-state">
        {state.key === 'running_now' ? (
          <span className="state-pill is-live">
            <span className="live-dot" aria-hidden="true" />
            Running now
          </span>
        ) : (
          <StatusDot tone={state.tone}>{state.label}</StatusDot>
        )}
        <div className={`agent-row-detail${state.tone === 'danger' ? ' is-error' : ''}`} title={detail}>
          {detail}
        </div>
        {agent.busy ? (
          <div
            className={`progress-track is-small${agent.step_percent === null ? ' is-indeterminate' : ''}`}
            role="progressbar"
            aria-label={agent.step || 'Working'}
            aria-valuenow={agent.step_percent ?? undefined}
          >
            <div className="progress-fill" style={agent.step_percent === null ? undefined : { width: `${agent.step_percent}%` }} />
          </div>
        ) : null}
      </div>

      <div className="agent-row-actions">
        {!agent.configured ? (
          <button type="button" className="btn btn-secondary btn-sm" disabled={!writable} onClick={onSetup}>
            Set up
          </button>
        ) : agent.busy ? (
          <button type="button" className="btn btn-secondary btn-sm" onClick={onOpen}>
            View progress
          </button>
        ) : (
          <button type="button" className="btn btn-secondary btn-sm" disabled={!writable || busy} onClick={onToggle}>
            {isOn ? 'Pause' : agent.status === 'error' ? 'Restart' : 'Turn on'}
          </button>
        )}
        <Menu
          label={`More actions for ${agent.name}`}
          trigger={<MoreIcon />}
          items={[
            { label: 'Open (activity & results)', onSelect: onOpen },
            { label: agent.configured ? 'Settings' : 'Set up', disabled: !writable, onSelect: onSetup },
            {
              label: busy ? 'Starting…' : 'Run once now',
              hint: agent.makes_changes ? 'Changes follow your approval setting' : 'Only reports, changes nothing',
              disabled: !writable || busy || !agent.configured || agent.busy,
              onSelect: onRunNow,
            },
            ...(agent.configured && isOn && agent.busy
              ? [{ label: 'Pause after this run', onSelect: onToggle, disabled: !writable }]
              : []),
          ]}
        />
      </div>
    </li>
  )
}
