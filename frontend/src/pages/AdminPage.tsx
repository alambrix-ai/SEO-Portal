/**
 * Admin - team and role-based access, invitations, billing, audit log.
 *
 * The role select is disabled where the server would refuse the change (the
 * owner, and yourself), so the UI does not offer an action that cannot succeed.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { Role } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  Dialog,
  ErrorState,
  Field,
  Loading,
  Meter,
  SectionHeading,
  Segmented,
  Tag,
} from '@/components/ui'
import { useDebounced, useResource } from '@/hooks/useResource'

export function AdminPage() {
  const { canWrite, session, refresh } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const writable = canWrite('admin')

  // Read from the session rather than kept locally, so it agrees with
  // whatever the guardrail is actually enforcing after a refresh.
  const autonomous = Boolean(session?.organization.global_autonomy)

  const setAutonomy = async (next: boolean) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    try {
      fromResult(await api.setGlobalAutonomy(next))
      await refresh()
    } catch (caught) {
      fromError(caught)
    }
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

  const deactivate = async (id: string) => {
    if (!guard()) return
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
      {/* Moved here from the topbar, where it appeared on every screen next
          to controls it had nothing to do with - and, on the Agents screen,
          directly above twelve cards each carrying the same two words, which
          made it read as a filter for them. It is not a filter. It is the
          ceiling: an agent set to Autonomous only acts on its own while this
          allows it. */}
      <SectionHeading
        title="Autonomy policy"
        description="Applies to every agent in the workspace."
      />
      <div className="card" style={{ marginBottom: 'var(--space-4)' }}>
        <div className="row-between">
          <div>
            <div className="card-title" style={{ fontSize: 15 }}>
              {autonomous ? 'Autonomous execution' : 'Human review'}
            </div>
            <p className="card-meta" style={{ marginTop: 4 }}>
              {autonomous
                ? 'Agents set to Autonomous apply low-impact changes themselves. High-impact ones still queue for approval.'
                : 'Every change waits for a person, whatever an individual agent is set to.'}
            </p>
          </div>
          <Segmented
            name="autonomy-global"
            value={autonomous ? 'auto' : 'human'}
            options={[
              { value: 'auto', label: 'Autonomous' },
              { value: 'human', label: 'Human review' },
            ]}
            disabled={!writable}
            onChange={(next) => void setAutonomy(next === 'auto')}
          />
        </div>
      </div>

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
                      onClick={() => void deactivate(member.id)}
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
