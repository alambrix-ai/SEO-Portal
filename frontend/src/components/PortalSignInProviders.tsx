/**
 * Portal Admin → Sign-in providers: a setup wizard per provider.
 *
 * Each provider (Google, Meta, LinkedIn, Microsoft, …) is set up once for the
 * whole platform. The card says whether it is ready, walks through the
 * vendor's console in numbered steps, gives the exact redirect URI to copy,
 * lists the permissions to switch on, and takes the values the console
 * gives you. Saving stores them encrypted and, where the vendor allows a
 * quick check, checks them straight away.
 *
 * Secrets are write-only: the API returns only "Set · ends in 1234".
 */
import { useEffect, useRef, useState } from 'react'

import { api } from '@/api/client'
import type { SignInCheck, SignInProvider } from '@/api/types'
import { useConfirm } from '@/components/Confirm'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { ChevronDownIcon, ExternalIcon } from '@/components/icons'
import { ErrorState, Field, Loading, SectionHeading, Tag } from '@/components/ui'
import { useResource } from '@/hooks/useResource'

const STATUS: Record<SignInProvider['status'], { label: string; tone: 'accent-2' | 'warning' | 'outline' }> = {
  ready: { label: 'Ready', tone: 'accent-2' },
  partial: { label: 'Partly ready', tone: 'warning' },
  not_set_up: { label: 'Not set up', tone: 'outline' },
  built_in: { label: 'Nothing to set up', tone: 'outline' },
}

export function PortalSignInProviders({ focus }: { focus?: string }) {
  const providers = useResource((signal) => api.portalSignInProviders(signal), [], 'portal:sign-in')
  const [list, setList] = useState<SignInProvider[] | null>(null)
  useEffect(() => {
    if (providers.data) setList(providers.data)
  }, [providers.data])

  const rows = list ?? providers.data ?? []
  const settable = rows.filter((p) => p.status !== 'built_in')
  const ready = settable.filter((p) => p.status === 'ready').length

  return (
    <section className="portal-panel" aria-label="Sign-in providers">
      <SectionHeading
        title="Sign-in providers"
        description="Set up each sign-in once. After that, every customer connects with one click. What you save here is encrypted and takes the place of any server setting."
      />
      {providers.loading && !providers.data ? <Loading label="Loading sign-in providers…" /> : null}
      {providers.error && !providers.data ? <ErrorState message={providers.error} onRetry={providers.reload} /> : null}

      {rows.length ? (
        <p className="guide-text muted small signin-summary">
          {ready} of {settable.length} ready
        </p>
      ) : null}

      <div className="signin-providers">
        {rows.map((provider) => (
          <ProviderCard
            key={provider.name}
            provider={provider}
            focused={focus === provider.name}
            onChange={(next) => setList((prev) => (prev ?? rows).map((p) => (p.name === next.name ? next : p)))}
          />
        ))}
      </div>
    </section>
  )
}

function ProviderCard({
  provider,
  focused,
  onChange,
}: {
  provider: SignInProvider
  focused: boolean
  onChange: (next: SignInProvider) => void
}) {
  const { push, fromError } = useToasts()
  const confirm = useConfirm()
  const ref = useRef<HTMLElement>(null)
  const [open, setOpen] = useState(focused)
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<'' | 'save' | 'check' | 'clear'>('')
  const [check, setCheck] = useState<SignInCheck | null>(null)
  const status = STATUS[provider.status]
  const builtIn = provider.status === 'built_in'

  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [focused])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(provider.redirect_uri)
      push('Redirect URI copied', 'success')
    } catch {
      push('Copy it from the box instead', 'warning')
    }
  }

  const typed = Object.values(values).some((value) => value.trim())

  const save = async () => {
    setBusy('save')
    setCheck(null)
    try {
      const result = await api.savePortalSignInProvider(provider.name, values)
      setValues({})
      setCheck(result.check)
      onChange(result.provider)
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy('')
    }
  }

  const recheck = async () => {
    setBusy('check')
    try {
      setCheck(await api.checkPortalSignInProvider(provider.name))
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy('')
    }
  }

  const clear = async () => {
    const ok = await confirm({
      title: `Remove Sign in with ${provider.label}?`,
      message: 'The values saved here are deleted.',
      detail:
        'Customers can no longer start new sign-ins with it, unless the same values are set on the server. Connections already made stop renewing.',
      confirmLabel: 'Remove',
      tone: 'danger',
    })
    if (!ok) return
    setBusy('clear')
    try {
      const result = await api.clearPortalSignInProvider(provider.name)
      setValues({})
      setCheck(result.check)
      onChange(result.provider)
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy('')
    }
  }

  const bodyId = `signin-${provider.name}`

  return (
    <article ref={ref} className={`signin-provider${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="signin-provider-head"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="signin-provider-title">
          <span className="signin-provider-name">{provider.label}</span>
          {provider.name === 'linkedin_pages' ? <span className="small muted">Company pages</span> : null}
          {provider.name === 'linkedin' ? <span className="small muted">Ads</span> : null}
        </span>
        <span className="signin-unlocks" aria-label="Unlocks">
          {provider.connectors.map((connector) => (
            <span key={connector.slug} className="signin-unlock" title={connector.name}>
              <ConnectorIcon slug={connector.slug} name={connector.name} size={18} />
              <span>{connector.name}</span>
            </span>
          ))}
        </span>
        <Tag tone={status.tone}>{status.label}</Tag>
        <ChevronDownIcon size={16} />
      </button>

      {open ? (
        <div id={bodyId} className="signin-provider-body">
          {provider.status_detail ? <p className="guide-text small">{provider.status_detail}</p> : null}

          {builtIn ? (
            <p className="guide-text muted small">{provider.note}</p>
          ) : (
            <>
              {provider.review_note ? <p className="guide-text muted small">{provider.review_note}</p> : null}

              <ol className="connect-step-list" aria-label={`How to set up ${provider.label}`}>
                {provider.steps.map((step, index) => (
                  <li key={step} className="connect-step">
                    <span className="connect-step-number" aria-hidden="true">
                      {index + 1}
                    </span>
                    <div className="connect-step-body">
                      <p>{step}</p>
                      {index === 0 && provider.console_url ? (
                        <a
                          className="btn btn-secondary btn-sm"
                          href={provider.console_url}
                          target="_blank"
                          rel="noreferrer noopener"
                        >
                          Open {provider.console_label || `${provider.label} console`}
                          <ExternalIcon size={14} />
                        </a>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ol>

              <div className="signin-block">
                <div className="guide-title">Redirect URI</div>
                <div className="signin-uri">
                  <code>{provider.redirect_uri}</code>
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => void copy()}>
                    Copy
                  </button>
                </div>
              </div>

              {provider.permissions.length ? (
                <div className="signin-block">
                  <div className="guide-title">
                    Permissions to switch on
                    {provider.permissions_where ? (
                      <span className="small muted"> · {provider.permissions_where}</span>
                    ) : null}
                  </div>
                  <ul className="signin-permissions">
                    {provider.permissions.map((permission) => (
                      <li key={permission}>
                        <code>{permission}</code>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              <form
                className="signin-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  void save()
                }}
              >
                {provider.fields.map((field) => {
                  const id = `${bodyId}-${field.key}`
                  const keep = field.is_set ? `${field.preview} — leave blank to keep` : field.placeholder
                  const hint = [
                    field.needed_for ? `Only needed for ${field.needed_for}.` : '',
                    field.source === 'server' ? 'Currently taken from the server settings.' : '',
                    field.help,
                  ]
                    .filter(Boolean)
                    .join(' ')
                  return (
                    <Field key={field.key} label={field.label} htmlFor={id} hint={hint || undefined}>
                      {field.multiline ? (
                        <textarea
                          id={id}
                          className="input signin-textarea"
                          rows={4}
                          spellCheck={false}
                          autoComplete="off"
                          placeholder={keep}
                          value={values[field.key] ?? ''}
                          onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
                        />
                      ) : (
                        <input
                          id={id}
                          className="input"
                          type={field.secret ? 'password' : 'text'}
                          autoComplete={field.secret ? 'new-password' : 'off'}
                          spellCheck={false}
                          placeholder={keep}
                          value={values[field.key] ?? ''}
                          onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
                        />
                      )}
                    </Field>
                  )
                })}

                {check ? (
                  <div
                    className={`banner ${check.status === 'ok' ? 'is-success' : check.status === 'failed' ? 'is-danger' : 'is-info'}`}
                    role="status"
                  >
                    {check.message}
                  </div>
                ) : null}

                <div className="connect-row">
                  <button type="submit" className="btn btn-primary" disabled={!typed || busy !== ''}>
                    {busy === 'save' ? 'Saving and checking…' : 'Save'}
                  </button>
                  {provider.configured && provider.checkable ? (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={busy !== ''}
                      onClick={() => void recheck()}
                    >
                      {busy === 'check' ? 'Checking…' : 'Check again'}
                    </button>
                  ) : null}
                  <div className="dialog-actions-spacer" />
                  {provider.saved_here ? (
                    <button
                      type="button"
                      className="btn btn-ghost danger"
                      disabled={busy !== ''}
                      onClick={() => void clear()}
                    >
                      {busy === 'clear' ? 'Removing…' : 'Remove'}
                    </button>
                  ) : null}
                </div>
                {provider.saved_here && provider.updated_at ? (
                  <p className="guide-text muted small">
                    Saved by {provider.updated_by || 'a portal admin'} on{' '}
                    {new Date(provider.updated_at).toLocaleString()}
                  </p>
                ) : null}
              </form>
            </>
          )}

        </div>
      ) : null}
    </article>
  )
}
