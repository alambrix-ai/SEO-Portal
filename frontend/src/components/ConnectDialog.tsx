/**
 * Connect / edit credentials for one connector.
 *
 * Fields come from the connector's server declaration. Secrets are write-only:
 * the API never returns them; the form shows non-secret hints instead.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { ConnectorOut } from '@/api/types'
import { useConfirm } from '@/components/Confirm'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { Dialog, Field } from '@/components/ui'

export function ConnectDialog({
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
        These are checked against {connector.name} when you save - nothing is stored
        unless they work. They are encrypted under this workspace&rsquo;s own key before
        they reach the database, and are never returned by the API.
      </p>

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
            id={`cx-${connector.slug}-${field.key}`}
            name={`cx-${connector.slug}-${field.key}`}
            className="input"
            type={field.type === 'password' ? 'password' : 'text'}
            inputMode={field.type === 'number' ? 'numeric' : undefined}
            placeholder={field.placeholder}
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
