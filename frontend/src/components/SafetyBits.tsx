/**
 * Spending-agent safety in the console: the banner across every screen, the
 * workspace's limits and "Stop all agents", the practice badge and weekly
 * summary, and the platform emergency stop for Portal Admin.
 *
 * The rules themselves live on the server (app/agents/base/safety.py); these
 * components only show them and flip the switches.
 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { safetyApi } from '@/api/client'
import type { AgentOut, PlatformSafety, PracticeSummary, SafetyAction, SafetyStatus } from '@/api/types'
import { useConfirm } from '@/components/Confirm'
import { useToasts } from '@/components/Toasts'
import { Field, SectionHeading, Tag } from '@/components/ui'
import '@/styles/safety.css'

/** Fired after any switch changes, so the banner refreshes at once. */
export const SAFETY_EVENT = 'safety:changed'

function announce() {
  window.dispatchEvent(new Event(SAFETY_EVENT))
}

function when(iso: string): string {
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
}

/** The workspace's safety status, refreshed when a switch changes and every minute. */
export function useSafetyStatus() {
  const [status, setStatus] = useState<SafetyStatus | null>(null)
  const load = useCallback(async () => {
    try {
      setStatus(await safetyApi.status())
    } catch {
      // The banner is a convenience; a failed read leaves the console usable.
    }
  }, [])
  useEffect(() => {
    void load()
    const onChange = () => void load()
    window.addEventListener(SAFETY_EVENT, onChange)
    const timer = window.setInterval(onChange, 60_000)
    return () => {
      window.removeEventListener(SAFETY_EVENT, onChange)
      window.clearInterval(timer)
    }
  }, [load])
  return { status, setStatus, reload: load }
}

// ── The banner across the console ──────────────────────────────────────────
export function SafetyBanner() {
  const { status, setStatus } = useSafetyStatus()
  const { fromResult, fromError } = useToasts()
  const [busy, setBusy] = useState(false)
  if (!status) return null

  const resume = async () => {
    setBusy(true)
    try {
      const out = await safetyApi.resumeAll()
      setStatus(out.status)
      fromResult(out)
      announce()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const items = []
  if (status.platform_stop) {
    items.push(
      <div key="platform-stop" className="banner is-danger" role="alert">
        <strong>Emergency stop is on.</strong> No agent runs or sends changes in any workspace until the
        platform team switches it off.
      </div>,
    )
  }
  if (status.stopped) {
    items.push(
      <div key="stopped" className="banner is-danger" role="alert">
        <strong>All agents are stopped.</strong>
        {status.stopped_by ? ` Stopped by ${status.stopped_by}${status.stopped_at ? `, ${when(status.stopped_at)}` : ''}.` : ''}{' '}
        Nothing runs or is sent until someone presses Resume.
        {status.can_manage ? (
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void resume()}>
            {busy ? 'Resuming…' : 'Resume'}
          </button>
        ) : null}
      </div>,
    )
  }
  if (status.platform_practice || status.practice_mode) {
    items.push(
      <div key="practice" className="banner is-warning">
        <strong>Practice mode:</strong>&nbsp;spending agents plan changes but don&rsquo;t send them.
        {status.platform_practice ? ' Set by the platform team.' : ''}
        <Link className="banner-link" to="/approvals?view=practice">
          See what they planned
        </Link>
      </div>,
    )
  }
  if (!items.length) return null
  return <div className="safety-banners">{items}</div>
}

// ── Badges ─────────────────────────────────────────────────────────────────
export function PracticeBadge({ title }: { title?: string }) {
  return (
    <Tag tone="warning" title={title ?? 'Practised: planned and recorded, not sent'}>
      Practice
    </Tag>
  )
}

// ── Workspace settings: practice, stop, limits ─────────────────────────────
export function SpendingSafetySettings() {
  const { status, setStatus } = useSafetyStatus()
  const { push, fromResult, fromError } = useToasts()
  const confirm = useConfirm()
  const [caps, setCaps] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')

  useEffect(() => {
    if (status) {
      setCaps(Object.fromEntries(Object.entries(status.caps).map(([key, value]) => [key, String(value)])))
    }
  }, [status])

  if (!status) return null
  const writable = status.can_manage

  const run = async (key: string, action: () => Promise<SafetyAction>) => {
    if (!writable) {
      push('Only workspace admins can change this', 'warning')
      return
    }
    setBusy(key)
    try {
      const out = await action()
      setStatus(out.status)
      fromResult(out)
      announce()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy('')
    }
  }

  const stop = async () => {
    const ok = await confirm({
      title: 'Stop all agents?',
      message: 'Every agent in this workspace is paused now, and nothing more is sent to your website or ad accounts.',
      detail:
        'A run already in progress sends nothing further: its remaining changes wait for approval. Content scheduled to publish later is held. Press Resume to restart the agents that were running.',
      confirmLabel: 'Stop all agents',
      tone: 'danger',
    })
    if (ok) await run('stop', safetyApi.stopAll)
  }

  const saveCaps = () => {
    const values: Record<string, number> = {}
    for (const field of status.cap_fields) {
      const raw = (caps[field.key] ?? '').trim()
      const number = Number(raw)
      if (raw === '' || !Number.isFinite(number) || number < 0) {
        push(`${field.label}: enter a number of 0 or more`, 'warning')
        return
      }
      values[field.key] = number
    }
    void run('caps', () => safetyApi.saveCaps(values))
  }

  return (
    <>
      <SectionHeading
        title="Spending agents"
        description="Safety controls for the agents that move budget, block placements, upload ads, create campaigns or push audiences."
        spaced
      />
      <div className="card" style={{ marginBottom: 'var(--space-4)' }}>
        <div className="safety-row">
          <div>
            <div className="card-title" style={{ fontSize: 15 }}>
              {status.stopped ? 'All agents are stopped' : 'Stop all agents'}
            </div>
            <p className="card-meta" style={{ marginTop: 4 }}>
              {status.stopped
                ? 'Nothing runs or is sent until you resume. Resume restarts the agents that were running.'
                : 'Pauses every agent in this workspace at once. Use it if something looks wrong.'}
            </p>
          </div>
          {status.stopped ? (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={!writable || busy !== ''}
              onClick={() => void run('resume', safetyApi.resumeAll)}
            >
              {busy === 'resume' ? 'Resuming…' : 'Resume'}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-stop"
              disabled={!writable || busy !== ''}
              onClick={() => void stop()}
            >
              {busy === 'stop' ? 'Stopping…' : 'Stop all agents'}
            </button>
          )}
        </div>

        <div className="safety-row">
          <div>
            <div className="card-title" style={{ fontSize: 15 }}>
              Practice mode for all spending agents
            </div>
            <p className="card-meta" style={{ marginTop: 4 }}>
              They read your accounts and plan every change, then record it on the Approvals screen
              under Practice instead of sending it.
              {status.platform_practice ? ' The platform team has switched this on for every workspace.' : ''}
            </p>
          </div>
          <label className="row" style={{ gap: 8 }}>
            <input
              type="checkbox"
              checked={status.practice_mode || status.platform_practice}
              disabled={!writable || busy !== '' || status.platform_practice}
              onChange={(event) => {
                const on = event.target.checked
                void run('practice', () => safetyApi.setPractice(on))
              }}
            />
            <span>{status.practice_mode || status.platform_practice ? 'On' : 'Off'}</span>
          </label>
        </div>

        <div className="safety-row" style={{ display: 'block' }}>
          <div className="card-title" style={{ fontSize: 15 }}>
            Limits on automatic changes
          </div>
          <p className="card-meta" style={{ marginTop: 4, marginBottom: 12 }}>
            A change over a limit is never made automatically. It waits on the Approvals screen with
            the reason, for example &ldquo;Moves 35% of budget; your limit is 20% per change.&rdquo; Set a
            limit to 0 to approve every such change yourself.
          </p>
          <div className="safety-caps">
            {status.cap_fields.map((field) => (
              <Field
                key={field.key}
                label={
                  field.key === 'max_budget_moved_per_day' && status.currency_symbol
                    ? `${field.label} (${status.currency_symbol})`
                    : field.label
                }
                htmlFor={`cap-${field.key}`}
                hint={`${field.help} Default: ${status.default_caps[field.key]}.`}
              >
                <input
                  id={`cap-${field.key}`}
                  className="input"
                  type="number"
                  min={0}
                  step={field.key === 'max_budget_moved_per_day' ? '0.01' : '1'}
                  value={caps[field.key] ?? ''}
                  disabled={!writable}
                  onChange={(event) => setCaps((current) => ({ ...current, [field.key]: event.target.value }))}
                />
              </Field>
            ))}
          </div>
          {writable ? (
            <div className="row" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="btn btn-primary" disabled={busy !== ''} onClick={saveCaps}>
                {busy === 'caps' ? 'Saving…' : 'Save limits'}
              </button>
            </div>
          ) : (
            <p className="card-meta">Only workspace admins can change these.</p>
          )}
        </div>
      </div>
    </>
  )
}

// ── Agent setup: the per-agent practice switch ─────────────────────────────
export function PracticeToggle({
  checked,
  onChange,
  source,
  disabled = false,
}: {
  checked: boolean
  onChange: (on: boolean) => void
  /** Practice set above the agent ("platform" / "workspace") overrides this switch. */
  source?: string
  disabled?: boolean
}) {
  const forced = source === 'platform' || source === 'workspace'
  return (
    <label className={`choice-card is-tall${checked || forced ? ' is-picked' : ''}`}>
      <input
        type="checkbox"
        checked={checked || forced}
        disabled={disabled || forced}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="choice-card-body">
        <span className="choice-card-title">Practice mode — plan changes but don&rsquo;t send them</span>
        <span className="choice-card-text">
          {forced
            ? source === 'platform'
              ? 'On for every workspace, set by the platform team. The agent records what it would change and sends nothing.'
              : 'On for all spending agents in this workspace (Team & Access). The agent records what it would change and sends nothing.'
            : 'The agent reads your accounts and works out every change, then records it under Practice on the Approvals screen instead of sending it.'}
        </span>
      </span>
    </label>
  )
}

// ── Agent detail: practice mode and the verified-connector note ────────────
export function AgentSafetyBar({
  agent,
  writable,
  onChanged,
}: {
  agent: AgentOut
  writable: boolean
  onChanged: (agent: AgentOut) => void
}) {
  const [busy, setBusy] = useState(false)
  const { fromError, push } = useToasts()
  if (!agent.spending) return null
  const source = agent.practice_source ?? ''
  const forced = source === 'platform' || source === 'workspace'

  const flip = async () => {
    setBusy(true)
    try {
      const next = await safetyApi.setAgentPractice(agent.slug, !agent.practice_mode)
      onChanged(next)
      push(
        next.practice_mode
          ? `${agent.name} is in practice mode: it plans changes but doesn't send them`
          : `${agent.name} now sends the changes it makes`,
        next.practice_mode ? 'info' : 'success',
      )
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {source ? (
        <div className="banner is-warning">
          <PracticeBadge />
          &nbsp;{agent.practice_note}
          {!forced && writable ? (
            <button type="button" className="banner-link" disabled={busy} onClick={() => void flip()}>
              Switch off practice mode
            </button>
          ) : null}
        </div>
      ) : writable && agent.configured ? (
        <div className="banner">
          {agent.practice_note}
          <button type="button" className="banner-link" disabled={busy} onClick={() => void flip()}>
            Switch on practice mode
          </button>
        </div>
      ) : null}
      {agent.automatic_note ? (
        <div className="banner is-info">
          {agent.automatic_note} Until then, every change it makes waits for your approval.
        </div>
      ) : null}
    </>
  )
}

// ── Agent results: what practice mode would have done ──────────────────────
export function PracticeSummaryCard({ slug, active }: { slug: string; active: boolean }) {
  const [summary, setSummary] = useState<PracticeSummary | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    safetyApi
      .practiceSummary(slug, controller.signal)
      .then(setSummary)
      .catch(() => setSummary(null))
    return () => controller.abort()
  }, [slug])
  if (!summary || (!active && summary.changes === 0)) return null
  return (
    <div className="banner is-warning" style={{ marginBottom: 'var(--space-4)' }}>
      <PracticeBadge />
      &nbsp;{summary.sentence}
      <Link className="banner-link" to={`/approvals?view=practice&agent=${encodeURIComponent(slug)}`}>
        Compare the changes
      </Link>
    </div>
  )
}

// ── Portal Admin: emergency stop and platform practice ─────────────────────
export function PortalSafetyPanel() {
  const [state, setState] = useState<PlatformSafety | null>(null)
  const [busy, setBusy] = useState('')
  const { fromResult, fromError } = useToasts()
  const confirm = useConfirm()

  useEffect(() => {
    const controller = new AbortController()
    safetyApi
      .platform(controller.signal)
      .then(setState)
      .catch(() => setState(null))
    return () => controller.abort()
  }, [])
  if (!state) return null

  const flip = async (key: 'stop' | 'practice', on: boolean) => {
    if (key === 'stop' && on) {
      const ok = await confirm({
        title: 'Switch on the emergency stop?',
        message: 'Every agent run in every workspace stops. The scheduler skips everything and nothing is sent to any customer’s website or ad accounts.',
        detail: 'Approvals cannot be sent while it is on either. Customers see a red banner. Switch it off here to let agents run again.',
        confirmLabel: 'Stop everything',
        tone: 'danger',
      })
      if (!ok) return
    }
    setBusy(key)
    try {
      const out = key === 'stop' ? await safetyApi.setEmergencyStop(on) : await safetyApi.setPlatformPractice(on)
      setState(out.status)
      fromResult(out)
      announce()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy('')
    }
  }

  const stop = state.emergency_stop
  const practice = state.practice_mode
  return (
    <section className="card" aria-label="Emergency stop" style={{ marginBottom: 'var(--space-4)' }}>
      <div className="safety-row">
        <div>
          <div className="card-title" style={{ fontSize: 15 }}>
            Emergency stop {stop.on ? <Tag tone="danger">On</Tag> : null}
          </div>
          <p className="card-meta" style={{ marginTop: 4 }}>
            {stop.on
              ? `All agent runs are stopped in every workspace${stop.by ? ` (switched on by ${stop.by}${stop.at ? `, ${when(stop.at)}` : ''})` : ''}.`
              : 'Stops every agent run in every workspace at once. The scheduler skips everything while it is on.'}
          </p>
        </div>
        {stop.on ? (
          <button type="button" className="btn btn-secondary" disabled={busy !== ''} onClick={() => void flip('stop', false)}>
            {busy === 'stop' ? 'Switching off…' : 'Switch off'}
          </button>
        ) : (
          <button type="button" className="btn btn-stop" disabled={busy !== ''} onClick={() => void flip('stop', true)}>
            {busy === 'stop' ? 'Stopping…' : 'Emergency stop'}
          </button>
        )}
      </div>
      <div className="safety-row">
        <div>
          <div className="card-title" style={{ fontSize: 15 }}>
            Practice mode for spending agents, everywhere {practice.on ? <Tag tone="warning">On</Tag> : null}
          </div>
          <p className="card-meta" style={{ marginTop: 4 }}>
            Agents that move budget, block placements, upload ads, create campaigns or push audiences plan
            their changes and record them, in every workspace, but send nothing.
          </p>
        </div>
        <label className="row" style={{ gap: 8 }}>
          <input
            type="checkbox"
            checked={practice.on}
            disabled={busy !== ''}
            onChange={(event) => void flip('practice', event.target.checked)}
          />
          <span>{practice.on ? 'On' : 'Off'}</span>
        </label>
      </div>
    </section>
  )
}
