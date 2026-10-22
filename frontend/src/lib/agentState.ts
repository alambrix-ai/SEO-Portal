/**
 * One vocabulary for an agent's state, used on every screen.
 *
 * The API's `status: 'running'` means *switched on* (it runs on its
 * schedule), not *executing now* — `busy` is what says a pass is open. Showing
 * the first as "Running" made every switched-on agent look busy, so the two
 * are kept apart here: "Running now" only while a pass is in progress, "On"
 * for an agent that is scheduled and idle.
 */
import type { AgentOut } from '@/api/types'
import { scheduleLabel } from '@/lib/plain'

export type AgentStateKey = 'running_now' | 'problem' | 'waiting' | 'on' | 'paused' | 'not_set_up'
export type Tone = 'success' | 'warning' | 'danger' | 'neutral' | 'info'

export interface AgentState {
  key: AgentStateKey
  label: string
  tone: Tone
  /** One line under the label: what it is doing, or when it runs next. */
  detail: string
}

/** Display order: what needs eyes first. */
export const STATE_ORDER: AgentStateKey[] = ['running_now', 'problem', 'waiting', 'on', 'paused', 'not_set_up']

export const STATE_FILTERS: { key: AgentStateKey | 'all'; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'running_now', label: 'Running now' },
  { key: 'problem', label: 'Problems' },
  { key: 'on', label: 'On' },
  { key: 'paused', label: 'Paused' },
  { key: 'not_set_up', label: 'Not set up' },
]

function lower(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1)
}

/** What is missing, as one sentence, or empty. */
export function missingLine(agent: AgentOut): string {
  const missing = agent.needs.filter((n) => !n.optional && !n.satisfied && n.kind === 'integration')
  if (missing.length > 0) return `Needs a connection to: ${missing.map((n) => lower(n.label)).join('; ')}`
  const source = agent.needs.find((n) => !n.optional && !n.satisfied && n.kind === 'agent')
  if (source) return `Waiting for ${source.options[0]?.name ?? 'another agent'} to collect your pages`
  const setting = agent.needs.find((n) => !n.optional && !n.satisfied && n.kind === 'setting')
  if (setting) return `Needs a ${setting.label.toLowerCase()} in its settings`
  return ''
}

export function nextRunText(agent: AgentOut): string {
  if (agent.next_run === 'ongoing') return 'Checks continuously'
  if (agent.next_run === 'due') return 'Next run due now'
  if (!agent.next_run || agent.next_run === '—') return scheduleLabel(agent.schedule)
  return `Next run in ${agent.next_run} · ${scheduleLabel(agent.schedule).toLowerCase()}`
}

export function agentState(agent: AgentOut): AgentState {
  if (agent.busy) {
    const pct = agent.step_percent === null ? '' : ` · ${agent.step_percent}%`
    return { key: 'running_now', label: 'Running now', tone: 'info', detail: `${agent.step || 'Starting'}${pct}` }
  }
  if (agent.status === 'error' || agent.group === 'attention') {
    return {
      key: 'problem',
      label: 'Problem',
      tone: 'danger',
      detail: agent.last_error || 'It stopped. Open it to see why.',
    }
  }
  if (agent.status === 'running') {
    const missing = missingLine(agent)
    if (missing) return { key: 'waiting', label: 'On, but waiting', tone: 'warning', detail: missing }
    return { key: 'on', label: 'On', tone: 'success', detail: nextRunText(agent) }
  }
  if (agent.configured) {
    return { key: 'paused', label: 'Paused', tone: 'neutral', detail: missingLine(agent) || 'Turned off. It will not run until you turn it on.' }
  }
  return {
    key: 'not_set_up',
    label: 'Not set up',
    tone: 'neutral',
    detail: missingLine(agent) || 'Everything it needs is connected — ready to set up.',
  }
}

/** Which filter tab an agent belongs to ("waiting" agents are On). */
export function filterKey(state: AgentState): AgentStateKey {
  return state.key === 'waiting' ? 'on' : state.key
}
