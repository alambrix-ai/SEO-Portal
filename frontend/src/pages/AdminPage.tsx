/**
 * Admin - team and role-based access, invitations, billing, audit log, the
 * activity log download, the review setting's version history and how long
 * versions are kept.
 *
 * The role select is disabled where the server would refuse the change (the
 * owner, and yourself), so the UI does not offer an action that cannot succeed.
 */
import { useEffect, useRef, useState } from 'react'

import { api } from '@/api/client'
import { needsConfirmation } from '@/api/history'
import type { AuditExportFormat, Role } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { saveFile } from '@/components/AnalysisReport'
import { RetentionSettings, VersionHistory } from '@/components/AuditHistory'
import { useConfirm } from '@/components/Confirm'
import { SpendingSafetySettings } from '@/components/SafetyBits'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  Dialog,
  ErrorState,
  Field,
  Loading,
  Meter,
  PageHeader,
  SectionHeading,
  Segmented,
  Tag,
} from '@/components/ui'
import { invalidateResourceCache, useDebounced, useResource } from '@/hooks/useResource'
import {
  GUARDRAIL_DETAIL,
  GUARDRAIL_NAME,
  PUBLISH_AUTOMATICALLY_CONFIRM,
  REVIEW_SETTING_NAME,
  isReviewSetting,
  type ReviewSetting,
} from '@/lib/plain'

/** Most careful first, the same order onboarding offers them in. */
const REVIEW_CHOICES: ReviewSetting[] = ['human', 'hybrid', 'full']

/** Mirrors the server's default `audit_export_max_days`; the server enforces it. */
const AUDIT_EXPORT_MAX_DAYS = 366
const AUDIT_EXPORT_DEFAULT_DAYS = 30
const DAY_MS = 86_400_000
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export function AdminPage() {
  const { canWrite, canView, session, refresh } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const confirm = useConfirm()
  const writable = canWrite('admin')
  // One call saves the workspace choice (onboarding) and the agents' on/off
  // switch (agents) together; the server requires write access to both.
  const reviewWritable = writable && canWrite('onboarding') && canWrite('agents')

  // Read from the session rather than kept locally, so it agrees with
  // whatever the runner is actually enforcing after a refresh.
  const autonomous = Boolean(session?.organization.global_autonomy)
  const orgId = session?.organization.id ?? ''
  const canSeeSetting = canView('onboarding')
  const onboarding = useResource(
    (signal) => (canSeeSetting ? api.onboarding(signal) : Promise.resolve(null)),
    [orgId, canSeeSetting],
    orgId && canSeeSetting ? `onboarding:${orgId}` : undefined,
  )
  const stored = onboarding.data?.guardrail ?? ''
  // With the agents switched off every change waits, whatever was chosen.
  const reviewSetting: ReviewSetting = !autonomous
    ? 'human'
    : isReviewSetting(stored) && stored !== 'human'
      ? stored
      : 'hybrid'
  const [savingReview, setSavingReview] = useState(false)
  // Bumped after a save or restore so the history list shows the new version.
  const [policyVersion, setPolicyVersion] = useState(0)

  const setReviewSetting = async (next: ReviewSetting) => {
    if (!reviewWritable) {
      push('View-only access for your role', 'warning')
      return
    }
    if (next === reviewSetting || savingReview) return
    if (next === 'full') {
      const ok = await confirm(PUBLISH_AUTOMATICALLY_CONFIRM)
      if (!ok) return
    }
    setSavingReview(true)
    try {
      // One atomic, versioned call: the workspace choice and the agents'
      // switch change together or not at all. The server only flips the
      // agents' own switches when the workspace switch actually changes.
      const body = { guardrail: next, autonomous: next !== 'human', confirm: next === 'full' }
      try {
        await api.setWorkspacePolicy(body)
      } catch (caught) {
        // The server still wants a confirmation (e.g. the setting changed
        // meanwhile): ask the same question once more, then retry once.
        if (body.confirm || !needsConfirmation(caught)) throw caught
        if (!(await confirm(PUBLISH_AUTOMATICALLY_CONFIRM))) {
          await onboarding.reload()
          return
        }
        await api.setWorkspacePolicy({ ...body, confirm: true })
      }
      push(`${REVIEW_SETTING_NAME}: ${GUARDRAIL_NAME[next]}`, 'success')
      await policyChanged()
    } catch (caught) {
      fromError(caught)
      await onboarding.reload()
    } finally {
      setSavingReview(false)
    }
  }

  /** After a save or a restore: re-read the setting, the session switch and the agent cards. */
  const policyChanged = async () => {
    invalidateResourceCache('agents')
    setPolicyVersion((n) => n + 1)
    await Promise.all([onboarding.reload(), refresh()])
  }

  const [search, setSearch] = useState('')
  const debounced = useDebounced(search)
  const { data, loading, error, reload } = useResource(
    (signal) => api.admin(debounced, signal),
    [debounced],
    `admin:${debounced}`,
  )

  const [inviting, setInviting] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)

  const guard = (): boolean => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return false
    }
    return true
  }

  const changeRole = async (id: string, role: Role) => {
    if (!guard()) return
    setBusyId(id)
    try {
      await api.changeMemberRole(id, role)
      push('Role updated - their other sessions were signed out', 'success')
      await reload()
    } catch (caught) {
      fromError(caught)
      await reload()
    } finally {
      setBusyId(null)
    }
  }

  const deactivate = async (id: string, name: string) => {
    if (!guard()) return
    const ok = await confirm({
      title: 'Deactivate this member?',
      message: `${name || 'This person'} will be signed out and will no longer be able to open this workspace.`,
      confirmLabel: 'Deactivate',
      cancelLabel: 'Keep them',
      tone: 'danger',
    })
    if (!ok) return
    setBusyId(id)
    try {
      fromResult(await api.deactivateMember(id))
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  const revoke = async (id: string) => {
    if (!guard()) return
    setBusyId(id)
    try {
      fromResult(await api.revokeInvitation(id))
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  if (loading && !data) return <Loading label="Loading the workspace…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <PageHeader
        title="Team & Access"
        description="Members, roles and when agents ask you before publishing."
      />
      {/* Moved here from the topbar, where it appeared on every screen next
          to controls it had nothing to do with - and, on the Agents screen,
          directly above twelve cards each carrying the same two words, which
          made it read as a filter for them. It is not a filter. It is the
          ceiling: an agent set to publish on its own only does so while this
          allows it. */}
      <SectionHeading
        title={REVIEW_SETTING_NAME}
        description="Applies to every agent in the workspace."
      />
      <div className="card" style={{ marginBottom: 'var(--space-4)' }}>
        <div className="row-between">
          <div>
            <div className="card-title" style={{ fontSize: 15 }}>
              {GUARDRAIL_NAME[reviewSetting]}
            </div>
            <p className="card-meta" style={{ marginTop: 4 }}>
              {GUARDRAIL_DETAIL[reviewSetting]}
            </p>
          </div>
          <Segmented
            name="review-before-publishing"
            value={reviewSetting}
            options={REVIEW_CHOICES.map((value) => ({ value, label: GUARDRAIL_NAME[value] }))}
            disabled={!reviewWritable || savingReview || (canSeeSetting && !onboarding.data)}
            onChange={(next) => void setReviewSetting(next)}
          />
        </div>
      </div>

      <VersionHistory
        kind="workspace_policy"
        title="Review setting history"
        description={`A numbered version is kept each time "${REVIEW_SETTING_NAME}" changes. Open one to see who changed it and when, compare any two, or go back to an earlier setting.`}
        refreshKey={String(policyVersion)}
        onRestored={() => {
          void policyChanged()
        }}
      />

      <SpendingSafetySettings />

      <SectionHeading
        title="Team & role-based access"
        spaced
        action={
          writable ? (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setInviting(true)}
            >
              Invite teammate
            </button>
          ) : undefined
        }
      />

      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Email</th>
              <th>Role</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.team.map((member) => (
              <tr key={member.id}>
                <td>
                  <span className="cell-title">{member.name}</span>
                  <div className="row" style={{ marginTop: 4 }}>
                    {member.is_owner ? <Tag tone="outline">Owner</Tag> : null}
                    {!member.is_active ? <Tag tone="neutral">Deactivated</Tag> : null}
                    {!member.email_verified ? (
                      <Tag tone="neutral">Unconfirmed</Tag>
                    ) : null}
                  </div>
                </td>
                <td className="small muted">{member.email}</td>
                <td>
                  <select
                    className="input w-auto"
                    value={member.role}
                    disabled={!member.editable || busyId === member.id}
                    onChange={(event) =>
                      void changeRole(member.id, event.target.value as Role)
                    }
                    aria-label={`Role for ${member.name}`}
                  >
                    {data.role_options.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="table-actions">
                  {member.editable && member.is_active ? (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={busyId === member.id}
                      onClick={() => void deactivate(member.id, member.name)}
                    >
                      Deactivate
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {data.invitations.length > 0 ? (
        <>
          <SectionHeading
            title="Pending invitations"
            description="The link proves their address - there is nothing for them to set."
            spaced
          />
          <div className="stack-sm">
            {data.invitations.map((invitation) => (
              <div
                key={invitation.id}
                className="card"
                style={{
                  flexDirection: 'row',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                }}
              >
                <div>
                  <div style={{ fontSize: 13 }}>{invitation.email}</div>
                  <div className="card-meta">
                    {invitation.role_label} · expires{' '}
                    {new Date(invitation.expires_at).toLocaleDateString()}
                  </div>
                </div>
                {writable ? (
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={busyId === invitation.id}
                    onClick={() => void revoke(invitation.id)}
                  >
                    Revoke
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        </>
      ) : null}

      <div className="split-1-1" style={{ marginTop: 24 }}>
        <Blueprint className="card elev-sm">
          <div className="card-kicker">Billing</div>
          <div className="card-title">
            {data.billing.plan_name} - {data.billing.seats_total} seats
          </div>
          <Meter percent={data.billing.seats_percent} label="Seats used" />
          <div className="card-meta">
            {data.billing.seats_used} of {data.billing.seats_total} seats used
            {data.billing.renews_on
              ? ` · renews ${new Date(data.billing.renews_on).toLocaleDateString(undefined, {
                  month: 'short',
                  day: 'numeric',
                })}`
              : ''}
          </div>
        </Blueprint>

        <div>
          <h3 style={{ marginTop: 0 }}>Audit log</h3>
          <input
            className="input"
            placeholder="Filter audit log…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            style={{ marginBottom: 8 }}
            aria-label="Filter audit log"
          />
          <div className="audit-list">
            {data.audit.map((entry, index) => (
              <div className="audit-entry" key={`${entry.time}-${index}`}>
                <span className="audit-time">{entry.time}</span>
                {' - '}
                <span
                  className={entry.actor_type === 'agent' ? 'audit-actor-agent' : undefined}
                >
                  {entry.actor}
                </span>{' '}
                {entry.action}
              </div>
            ))}
            {data.audit.length === 0 ? (
              <div className="audit-entry muted">Nothing matches that filter.</div>
            ) : null}
          </div>
        </div>
      </div>

      {writable ? <AuditExportPanel /> : null}

      <RetentionSettings />

      {inviting ? (
        <InviteDialog
          roles={data.role_options}
          onClose={() => setInviting(false)}
          onInvited={async () => {
            setInviting(false)
            await reload()
          }}
        />
      ) : null}
    </>
  )
}

function InviteDialog({
  roles,
  onClose,
  onInvited,
}: {
  roles: { value: Role; label: string }[]
  onClose: () => void
  onInvited: () => Promise<void>
}) {
  const { push, fromError } = useToasts()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Role>('seo')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    try {
      await api.invite(email.trim(), role)
      push(`Invitation sent to ${email.trim()}`, 'success')
      await onInvited()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      title="Invite a teammate"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !email.trim()}
            onClick={() => void submit()}
          >
            {busy ? 'Sending…' : 'Send invitation'}
          </button>
        </>
      }
    >
      <p className="small" style={{ marginTop: 0 }}>
        They will receive a link, give their name, and they are in. After that they
        sign in the same way you do - a one-time code emailed to this address. No
        password is ever set, generated, or sent.
      </p>

      <Field label="Work email" htmlFor="invite-email" required>
        <input
          id="invite-email"
          className="input"
          type="email"
          placeholder="teammate@yourcompany.com"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </Field>

      <Field label="Role" htmlFor="invite-role" required>
        <select
          id="invite-role"
          className="input"
          value={role}
          onChange={(event) => setRole(event.target.value as Role)}
        >
          {roles.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </Field>
    </Dialog>
  )
}

/** A calendar date as YYYY-MM-DD in the browser's own time zone. */
function isoDate(value: Date): string {
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${value.getFullYear()}-${month}-${day}`
}

/** Days from `from` to `to`, both included; null when either is not a date. */
function inclusiveDays(from: string, to: string): number | null {
  if (!ISO_DATE.test(from) || !ISO_DATE.test(to)) return null
  const start = Date.parse(`${from}T00:00:00Z`)
  const end = Date.parse(`${to}T00:00:00Z`)
  if (Number.isNaN(start) || Number.isNaN(end)) return null
  return Math.round((end - start) / DAY_MS) + 1
}

/** A plain-English reason the range cannot be downloaded, or '' when it can. */
function rangeProblem(from: string, to: string): string {
  const days = inclusiveDays(from, to)
  if (days === null) return 'Choose both a start and an end date.'
  if (days < 1) return 'The end date must be on or after the start date.'
  if (days > AUDIT_EXPORT_MAX_DAYS) {
    return `Choose a range of ${AUDIT_EXPORT_MAX_DAYS} days or fewer.`
  }
  return ''
}

/**
 * "Download activity log" - admin writers only (the server checks too).
 *
 * Large ranges arrive in parts: the server sends a cursor with each part
 * while more entries remain, and the admin asks for the next part
 * explicitly, so one click never starts an unbounded series of downloads.
 * Changing the range or file type starts again from part 1.
 */
function AuditExportPanel() {
  const { push, fromError } = useToasts()
  const today = isoDate(new Date())
  const [from, setFrom] = useState(() =>
    isoDate(new Date(Date.now() - (AUDIT_EXPORT_DEFAULT_DAYS - 1) * DAY_MS)),
  )
  const [to, setTo] = useState(today)
  const [format, setFormat] = useState<AuditExportFormat>('csv')
  const [busy, setBusy] = useState(false)
  // The cursor for the next part, and how many parts are already saved.
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [partsSaved, setPartsSaved] = useState(0)
  const controller = useRef<AbortController | null>(null)

  // Stop an unfinished download when the page is left.
  useEffect(() => () => controller.current?.abort(), [])

  const problem = rangeProblem(from, to)

  const restart = () => {
    setNextCursor(null)
    setPartsSaved(0)
  }

  const download = async (cursor: string | null) => {
    if (busy || problem) return
    const abort = new AbortController()
    controller.current = abort
    setBusy(true)
    try {
      const file = await api.downloadAuditExport(
        { from, to, format, ...(cursor ? { cursor } : {}) },
        abort.signal,
      )
      saveFile(file)
      const saved = cursor ? partsSaved + 1 : 1
      setPartsSaved(saved)
      setNextCursor(file.nextCursor)
      if (file.nextCursor) {
        push(`Part ${saved} downloaded. More entries remain.`, 'info')
      } else {
        push(saved > 1 ? `Last part (${saved}) downloaded` : 'Activity log downloaded', 'success')
      }
    } catch (caught) {
      fromError(caught, 'The activity log could not be downloaded. Please try again.')
    } finally {
      if (controller.current === abort) controller.current = null
      setBusy(false)
    }
  }

  return (
    <>
      <SectionHeading
        title="Download activity log"
        description="Every recorded action in this workspace - who did what, and when. Entries cannot be edited or deleted, so the file is a tamper-proof record."
        spaced
      />
      <div className="card">
        <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 16 }}>
          <Field label="From" htmlFor="audit-export-from" required>
            <input
              id="audit-export-from"
              className="input w-auto"
              type="date"
              value={from}
              max={to || today}
              disabled={busy}
              onChange={(event) => {
                setFrom(event.target.value)
                restart()
              }}
            />
          </Field>
          <Field label="To" htmlFor="audit-export-to" required>
            <input
              id="audit-export-to"
              className="input w-auto"
              type="date"
              value={to}
              min={from || undefined}
              max={today}
              disabled={busy}
              onChange={(event) => {
                setTo(event.target.value)
                restart()
              }}
            />
          </Field>
          <Field label="File type">
            <Segmented
              name="audit-export-format"
              value={format}
              options={[
                { value: 'csv', label: 'Spreadsheet (CSV)' },
                { value: 'json', label: 'JSON' },
              ]}
              disabled={busy}
              onChange={(next) => {
                setFormat(next)
                restart()
              }}
            />
          </Field>
        </div>

        {problem ? (
          <p className="field-error" role="alert" style={{ marginTop: 0 }}>
            {problem}
          </p>
        ) : (
          <p className="card-meta" style={{ marginTop: 0 }}>
            Up to {AUDIT_EXPORT_MAX_DAYS} days at a time. Large ranges download in parts.
          </p>
        )}

        <div className="row" style={{ gap: 8, marginTop: 8 }} aria-live="polite">
          {nextCursor ? (
            <>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy || Boolean(problem)}
                onClick={() => void download(nextCursor)}
              >
                {busy ? 'Downloading…' : `Download next part (part ${partsSaved + 1})`}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={busy}
                onClick={restart}
              >
                Start again
              </button>
              <span className="small muted">
                {partsSaved === 1 ? 'Part 1 is' : `Parts 1-${partsSaved} are`} saved. More
                entries remain in this range.
              </span>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy || Boolean(problem)}
                onClick={() => void download(null)}
              >
                {busy ? 'Downloading…' : 'Download activity log'}
              </button>
              {partsSaved > 0 && !busy ? (
                <span className="small muted">
                  {partsSaved > 1 ? `All ${partsSaved} parts saved.` : 'Saved.'}
                </span>
              ) : null}
            </>
          )}
        </div>
      </div>
    </>
  )
}
