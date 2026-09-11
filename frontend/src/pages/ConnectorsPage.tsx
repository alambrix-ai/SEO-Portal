/**
 * Connectors marketplace — the catalogue, filters, and the connect dialog.
 *
 * The dialog's fields come from the connector's own declaration on the server,
 * so a new integration needs no frontend change. Secrets are write-only: the
 * API never returns them, and the form shows the non-secret hints instead.
 */
import { useMemo, useState } from 'react'

import { api } from '@/api/client'
import type { ConnectorOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { CardSection } from '@/components/CardSection'
import { useConfirm } from '@/components/Confirm'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { Dialog, ErrorState, Field, Loading, Tag } from '@/components/ui'
import { useResource } from '@/hooks/useResource'

/** The sections, in the order somebody scans them.
 *
 * `group` comes from the server, from the same rule that sorts the list, so
 * the headings and the order cannot disagree. */
const CONNECTOR_SECTIONS: {
  key: string
  title: string
  description?: string
  tone?: 'plain' | 'attention'
}[] = [
  {
    key: 'attention',
    title: 'Needs attention',
    description:
      'Connected, and not working. Every agent relying on one of these is ' +
      'either failing or quietly doing nothing.',
    tone: 'attention',
  },
  {
    key: 'connected',
    title: 'Connected',
    description: 'Live, and available to every agent that needs what they do.',
  },
  {
    key: 'available',
    title: 'Available to connect',
    description: 'Nothing here is running yet.',
  },
]

function statusTagTone(
  connector: ConnectorOut,
): 'accent' | 'accent-2' | 'neutral' | 'outline' {
  if (!connector.connected) return 'neutral'
  if (connector.health === 'failing' || connector.health === 'degraded')
    return 'outline'
  return 'accent'
}

export function ConnectorsPage() {
  const { canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const confirm = useConfirm()

  const [category, setCategory] = useState('All')
  const categories = useResource(() => api.connectorCategories())
  const { data, loading, error, reload } = useResource(() => api.connectors(), [])

  const [editing, setEditing] = useState<ConnectorOut | null>(null)
  const [busySlug, setBusySlug] = useState<string | null>(null)

  const writable = canWrite('connectors')

  const visible = useMemo(() => {
    if (!data) return []
    if (category === 'All') return data
    return data.filter((connector) => connector.category === category)
  }, [data, category])

  const connectedCount = data?.filter((c) => c.connected).length ?? 0

  const guard = (): boolean => {
    if (!writable) {
      push('View-only access — ask an admin to manage connectors', 'warning')
      return false
    }
    return true
  }

  const disconnect = async (connector: ConnectorOut) => {
    if (!guard()) return
    const ok = await confirm({
      title: `Disconnect ${connector.name}?`,
      message: 'Stored credentials will be removed from this workspace.',
      detail:
        'Agents that rely on this connector will report that they are waiting on a connection.',
      confirmLabel: 'Disconnect',
      tone: 'danger',
      icon: <ConnectorIcon slug={connector.slug} name={connector.name} size={40} />,
    })
    if (!ok) return
    setBusySlug(connector.slug)
    try {
      await api.disconnect(connector.slug)
      push('Disconnected', 'info')
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusySlug(null)
    }
  }

  if (loading && !data) return <Loading label="Loading the catalogue…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <div className="row-between" style={{ marginBottom: 12 }}>
        <div className="row">
          {(categories.data ?? ['All']).map((name) => (
            <button
              key={name}
              type="button"
              className={`btn ${category === name ? 'btn-primary' : 'btn-secondary'}`}
              onClick={() => setCategory(name)}
            >
              {name}
            </button>
          ))}
        </div>
        <div className="small muted nowrap">
          {connectedCount} of {data.length} connected
        </div>
      </div>

      {CONNECTOR_SECTIONS.map(({ key, title, description, tone }) => {
        const rows = visible.filter((connector) => connector.group === key)
        if (rows.length === 0) return null
        return (
          <CardSection
            key={key}
            title={title}
            count={rows.length}
            description={description}
            tone={tone}
          >
            <div className="grid-cards-sm">
              {rows.map((connector) => (
                <div key={connector.slug} className="card connector-card">
                  <div className="card-head">
                    <ConnectorIcon slug={connector.slug} name={connector.name} />
                    <div className="card-head-text">
                      <div className="card-kicker">{connector.category}</div>
                      <div className="card-title" style={{ fontSize: 15 }}>
                        {connector.name}
                      </div>
                    </div>
                  </div>
                  {connector.description ? (
                    <p className="card-body">{connector.description}</p>
                  ) : null}

                  {/* Same skeleton as the agent cards: identity, state, actions, and
                the slack between state and actions so twenty-five cards of
                different description lengths still line their buttons up.

                Nothing in this block exists until the integration does. An
                unconnected connector has no health, no sync time and no error
                — a "Not connected" tag next to a button that says Connect is
                the same fact stated twice. */}
                  <div className="agent-state">
                    {connector.connected ? (
                      <>
                        <div>
                          <Tag tone={statusTagTone(connector)}>
                            {connector.status_label}
                          </Tag>
                        </div>
                        {connector.activity_label ? (
                          <div className="card-meta">{connector.activity_label}</div>
                        ) : null}
                        {connector.last_error ? (
                          <div className="agent-error">{connector.last_error}</div>
                        ) : null}
                      </>
                    ) : null}
                  </div>

                  <div className="agent-actions">
                    <button
                      type="button"
                      className={`btn ${connector.connected ? 'btn-secondary' : 'btn-primary'}`}
                      disabled={busySlug === connector.slug}
                      onClick={() => {
                        if (connector.connected) {
                          void disconnect(connector)
                        } else {
                          if (!guard()) return
                          setEditing(connector)
                        }
                      }}
                    >
                      {connector.connected ? 'Disconnect' : 'Connect'}
                    </button>

                    {/* No Test button. Credentials are verified when they are
                  submitted — a connector cannot reach this state without
                  having worked — and the scheduler re-probes them on its own,
                  so the health tag is current without anybody pressing
                  anything. A button whose answer is almost always "yes" only
                  teaches people to stop reading it. */}
                    {connector.connected ? (
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => {
                          if (!guard()) return
                          setEditing(connector)
                        }}
                      >
                        Edit
                      </button>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          </CardSection>
        )
      })}

      {editing ? (
        <ConnectDialog
          // Keyed by slug so switching connectors builds a new form rather
          // than reusing the previous one's state.
          key={editing.slug}
          connector={editing}
          onClose={() => setEditing(null)}
          onConnected={async () => {
            setEditing(null)
            await reload()
          }}
        />
      ) : null}
    </>
  )
}

function ConnectDialog({
  connector,
  onClose,
  onConnected,
}: {
  connector: ConnectorOut
  onClose: () => void
  onConnected: () => Promise<void>
}) {
  const { push, fromError } = useToasts()
  const confirm = useConfirm()

  // Non-secret values already stored are pre-filled; secrets start empty,
  // because the API deliberately never returns them.
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const field of connector.fields) {
      if (field.is_oauth) continue
      initial[field.key] = connector.hints[field.key] ?? field.default ?? ''
    }
    return initial
  })
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState(false)

  // Every field is a real field now. The OAuth-button branch that used to
  // live here rendered a control which set a boolean and obtained no token;
  // the connectors ask for the credential the vendor actually issues.
  const normalFields = connector.fields.filter((field) => !field.is_oauth)

  const save = async () => {
    setBusy(true)
    try {
      await api.connect(connector.slug, values)
      push(`${connector.name} connected`, 'success')
      await onConnected()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  // The same thing the card's Disconnect does, offered here as well so the
  // edit dialog is not a dead end: somebody who opened it to change a
  // credential and decided to remove the integration instead should not have
  // to cancel out and find another button.
  const remove = async () => {
    const ok = await confirm({
      title: `Remove ${connector.name} credentials?`,
      message: 'Stored credentials for this connector will be deleted.',
      detail:
        'Agents that rely on it will report that they are waiting on a connector.',
      confirmLabel: 'Remove credentials',
      tone: 'danger',
      icon: <ConnectorIcon slug={connector.slug} name={connector.name} size={40} />,
    })
    if (!ok) return
    setRemoving(true)
    try {
      await api.disconnect(connector.slug)
      push(`${connector.name} disconnected`, 'info')
      await onConnected()
    } catch (caught) {
      fromError(caught)
    } finally {
      setRemoving(false)
    }
  }

  return (
    <Dialog
      title={`${connector.connected ? 'Edit' : 'Connect'} ${connector.name}`}
      onClose={onClose}
      actions={
        <>
          {connector.connected ? (
            <button
              type="button"
              className="btn btn-ghost danger"
              disabled={busy || removing}
              onClick={() => void remove()}
            >
              {removing ? 'Removing…' : 'Remove credentials'}
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
            {busy ? 'Checking…' : connector.connected ? 'Save credentials' : 'Connect'}
          </button>
        </>
      }
    >
      <p className="small" style={{ marginTop: 0 }}>
        These are checked against {connector.name} when you save — nothing is stored
        unless they work. They are encrypted under this workspace&rsquo;s own key before
        they reach the database, and are never returned by the API.
      </p>

      {/* Shown before the attempt, not after it. A plan that does not include
          the API is not something a failed request can be relied on to
          explain, and not something the operator can fix by retyping a
          token — so the requirements are stated up front. */}
      {connector.requirements.length > 0 ? (
        <div className="requirements">
          <div className="requirements-title">What {connector.name} needs</div>
          <ul>
            {connector.requirements.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Said where the secrets are actually typed, not buried in a help
          page. Somebody pasting a token that can publish to their website
          and spend their budget is entitled to know what happens to it, in
          the moment they are deciding whether to paste it.

          Stated as guarantees, not as mechanism. An earlier draft named the
          cipher and described the key hierarchy, which tells an attacker
          what to attack and tells the customer nothing they can act on. What
          they need to know is what is promised, not how it is built. */}
      <p className="credential-notice">
        Every secret on this form is encrypted before it is stored, under a key
        held for your workspace alone. It is never kept in readable form, never
        shown again once saved, never written to our logs, and never sent
        anywhere except the service it belongs to.
      </p>

      {normalFields.map((field) => (
        <Field
          key={field.key}
          label={field.label}
          required={field.required}
          htmlFor={`cx-${connector.slug}-${field.key}`}
          hint={field.help_text}
        >
          <input
            // Scoped to the connector. Shared ids are why a password manager
            // offered the GitHub token it had saved when the Bitbucket form
            // opened: both connectors really do have a field called `token`,
            // so to the browser it was the same field on the same site.
            id={`cx-${connector.slug}-${field.key}`}
            name={`cx-${connector.slug}-${field.key}`}
            className="input"
            type={field.type === 'password' ? 'password' : 'text'}
            // Kept as a text input even for numeric fields: type="number"
            // rejects the digit grouping people actually type into a budget
            // (10,00,000). inputMode gets the numeric keypad without that.
            inputMode={field.type === 'number' ? 'numeric' : undefined}
            placeholder={field.placeholder}
            // Chrome ignores autoComplete="off" on password fields; it does
            // honour new-password, which says this is a value being set
            // rather than one to recall.
            autoComplete={field.type === 'password' ? 'new-password' : 'off'}
            required={field.required}
            aria-required={field.required}
            value={values[field.key] ?? ''}
            onChange={(event) =>
              setValues((current) => ({
                ...current,
                [field.key]: event.target.value,
              }))
            }
          />
        </Field>
      ))}

      {connector.docs_url ? (
        <p className="small" style={{ marginBottom: 0 }}>
          <a href={connector.docs_url} target="_blank" rel="noreferrer noopener">
            {connector.name} API documentation
          </a>
        </p>
      ) : null}
    </Dialog>
  )
}
