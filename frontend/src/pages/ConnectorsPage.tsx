/**
 * Connectors marketplace - the catalogue, filters, and the connect dialog.
 *
 * The dialog's fields come from the connector's own declaration on the server,
 * so a new integration needs no frontend change. Secrets are write-only: the
 * API never returns them, and the form shows the non-secret hints instead.
 */
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '@/api/client'
import type { ConnectorOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { CardSection } from '@/components/CardSection'
import { useConfirm } from '@/components/Confirm'
import { ConnectDialog } from '@/components/ConnectDialog'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { ErrorState, Loading, Tag } from '@/components/ui'
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
  const [searchParams, setSearchParams] = useSearchParams()

  const [category, setCategory] = useState('All')
  const categories = useResource(
    (signal) => api.connectorCategories(signal),
    [],
    'connector-categories',
  )
  const { data, loading, error, reload } = useResource(
    (signal) => api.connectors('', signal),
    [],
    'connectors',
  )

  const [editing, setEditing] = useState<ConnectorOut | null>(null)
  const [busySlug, setBusySlug] = useState<string | null>(null)

  const writable = canWrite('connectors')

  // Deep-link from onboarding (and elsewhere): /connectors?configure=wordpress
  useEffect(() => {
    const slug = searchParams.get('configure')
    if (!slug || !data) return
    const match = data.find((connector) => connector.slug === slug)
    if (match) setEditing(match)
    const next = new URLSearchParams(searchParams)
    next.delete('configure')
    setSearchParams(next, { replace: true })
  }, [data, searchParams, setSearchParams])

  const visible = useMemo(() => {
    if (!data) return []
    if (category === 'All') return data
    return data.filter((connector) => connector.category === category)
  }, [data, category])

  const connectedCount = data?.filter((c) => c.connected).length ?? 0

  const guard = (): boolean => {
    if (!writable) {
      push('View-only access - ask an admin to manage connectors', 'warning')
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
      <div className="row-between filter-tabs-bar">
        <div className="filter-tabs" role="tablist" aria-label="Connector categories">
          {(categories.data ?? ['All']).map((name) => (
            <button
              key={name}
              type="button"
              role="tab"
              aria-selected={category === name}
              className={`filter-tab${category === name ? ' is-active' : ''}`}
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
