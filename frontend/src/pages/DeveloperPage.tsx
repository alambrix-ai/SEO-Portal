/**
 * Public API tokens and outbound webhooks.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { WorkflowApiTokenOut, WorkflowWebhookOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  SectionHeading,
  Tag,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { asList } from '@/lib/safeData'

export function DeveloperPage() {
  const { canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const writable = canWrite('admin')

  const tokens = useResource((s) => api.workflowApiTokens(s), [], 'workflow-tokens')
  const webhooks = useResource((s) => api.workflowWebhooks(s), [], 'workflow-webhooks')

  const [tokenLabel, setTokenLabel] = useState('API token')
  const [minted, setMinted] = useState('')
  const [hookName, setHookName] = useState('')
  const [hookUrl, setHookUrl] = useState('')
  const [hookSecret, setHookSecret] = useState('')
  const [busy, setBusy] = useState(false)

  if ((tokens.loading && !tokens.data) || (webhooks.loading && !webhooks.data)) {
    return <Loading label="Loading developer settings…" />
  }

  if (tokens.forbidden || webhooks.forbidden) {
    return <ErrorState message="You do not have permission to view API & webhook settings." />
  }

  const err = tokens.error || webhooks.error
  if (err && !tokens.data && !webhooks.data) {
    return (
      <ErrorState
        message={err}
        onRetry={() => {
          void tokens.reload()
          void webhooks.reload()
        }}
      />
    )
  }

  const tokenRows = asList<WorkflowApiTokenOut>(tokens.data?.tokens)
  const hookRows = asList<WorkflowWebhookOut>(webhooks.data?.webhooks)

  const mint = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    setBusy(true)
    try {
      const out = await api.mintWorkflowApiToken({ label: tokenLabel.trim() || 'API token' })
      setMinted(out.token)
      push('API token minted — copy it now', 'success')
      await tokens.reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (id: string) => {
    if (!writable) return
    setBusy(true)
    try {
      await api.revokeWorkflowApiToken(id)
      push('Token revoked', 'success')
      await tokens.reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const registerHook = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    if (!hookName.trim() || !hookUrl.trim() || !hookSecret.trim()) {
      return push('Name, URL and secret are required', 'warning')
    }
    setBusy(true)
    try {
      await api.registerWorkflowWebhook({
        name: hookName.trim(),
        url: hookUrl.trim(),
        secret: hookSecret.trim(),
        events: ['approval.decided'],
      })
      setHookName('')
      setHookUrl('')
      setHookSecret('')
      push('Webhook registered', 'success')
      await webhooks.reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="API & webhooks"
        description="Mint public API tokens and register outbound webhooks for approval and workflow events."
      />

      {err && (tokens.data || webhooks.data) ? (
        <div className="banner banner-warning" role="status" style={{ marginBottom: 16 }}>
          {err}
        </div>
      ) : null}

      <div className="split-1-1">
        <Blueprint className="card elev-sm">
          <SectionHeading title="API tokens" description="Raw tokens are shown once." />
          {tokenRows.length === 0 ? (
            <EmptyState title="No tokens" description="Mint a token to call the public workflow API." />
          ) : (
            <ul className="stack-sm">
              {tokenRows.map((token) => (
                <li key={token.id} className="list-row">
                  <div className="list-row-main">
                    <div className="list-row-title">{token.label}</div>
                    <div className="list-row-meta muted mono-sub">{token.prefix}…</div>
                  </div>
                  <div className="list-row-side">
                    <Tag tone={token.revoked ? 'neutral' : 'accent'}>
                      {token.revoked ? 'Revoked' : 'Active'}
                    </Tag>
                    {writable && !token.revoked ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        disabled={busy}
                        onClick={() => void revoke(token.id)}
                      >
                        Revoke
                      </button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {writable ? (
            <div className="stack">
              <Field label="Label" htmlFor="token-label">
                <input
                  id="token-label"
                  className="input"
                  value={tokenLabel}
                  onChange={(e) => setTokenLabel(e.target.value)}
                />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void mint()}>
                Mint token
              </button>
              {minted ? (
                <Field label="Copy this token now" htmlFor="minted-token">
                  <textarea id="minted-token" className="input mono-sub" readOnly rows={3} value={minted} />
                </Field>
              ) : null}
            </div>
          ) : null}
        </Blueprint>

        <Blueprint className="card elev-sm">
          <SectionHeading title="Webhooks" />
          {hookRows.length === 0 ? (
            <EmptyState title="No webhooks" description="Register an HTTPS endpoint to receive events." />
          ) : (
            <ul className="stack-sm">
              {hookRows.map((hook) => (
                <li key={hook.id} className="list-row">
                  <div className="list-row-main">
                    <div className="list-row-title">{hook.name}</div>
                    <div className="list-row-meta muted">{hook.url}</div>
                  </div>
                  <Tag tone={hook.enabled ? 'accent-2' : 'neutral'}>
                    {hook.enabled ? 'Enabled' : 'Off'}
                  </Tag>
                </li>
              ))}
            </ul>
          )}
          {writable ? (
            <div className="stack">
              <Field label="Name" htmlFor="hook-name">
                <input id="hook-name" className="input" value={hookName} onChange={(e) => setHookName(e.target.value)} />
              </Field>
              <Field label="URL" htmlFor="hook-url">
                <input id="hook-url" className="input" value={hookUrl} onChange={(e) => setHookUrl(e.target.value)} placeholder="https://…" />
              </Field>
              <Field label="Signing secret" htmlFor="hook-secret">
                <input id="hook-secret" className="input" value={hookSecret} onChange={(e) => setHookSecret(e.target.value)} />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void registerHook()}>
                Register webhook
              </button>
            </div>
          ) : null}
        </Blueprint>
      </div>
    </>
  )
}
