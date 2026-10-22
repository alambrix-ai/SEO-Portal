/**
 * Integrations - what is connected (with health) and the catalogue.
 *
 * The connect drawer's fields come from the connector's own declaration on
 * the server, so a new integration needs no frontend change. Secrets are
 * write-only: the API never returns them.
 */
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '@/api/client'
import type { ConnectorOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useConfirm } from '@/components/Confirm'
import { ConnectDialog } from '@/components/ConnectDialog'
import { BetaPill } from '@/components/BetaPill'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { Menu } from '@/components/Menu'
import { useToasts } from '@/components/Toasts'
import { MoreIcon, SearchIcon } from '@/components/icons'
import { ListPager } from '@/components/ListPager'
import { ErrorState, Loading, PageHeader, StatusDot } from '@/components/ui'
import {
  CONNECTOR_CATEGORY,
  categoryMatches,
  categorySortKey,
  categoryTitle,
  uniqueCategoryChips,
} from '@/lib/plain'
import { softenErrorMessage } from '@/lib/softenError'
import { usePagedList } from '@/hooks/usePagedList'
import { useResource } from '@/hooks/useResource'

type View = 'connected' | 'catalog'

/** Shown instead of a stored error that reads like code. */
const CONNECTION_PROBLEM = 'This integration reported a problem. Open it to check the connection.'

function healthOf(connector: ConnectorOut): {
  tone: 'success' | 'warning' | 'danger' | 'neutral'
  label: string
} {
  if (connector.group === 'attention' || connector.health === 'failing') {
    return { tone: 'danger', label: connector.status_label || 'Failing' }
  }
  if (connector.health === 'degraded') {
    return { tone: 'warning', label: connector.status_label || 'Degraded' }
  }
  return { tone: 'success', label: connector.status_label || 'Healthy' }
}

export function ConnectorsPage() {
  const { canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const confirm = useConfirm()
  const [searchParams, setSearchParams] = useSearchParams()

  const [category, setCategory] = useState('All')
  const [query, setQuery] = useState('')
  const [view, setView] = useState<View | null>(null)
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

  // A sign-in finished elsewhere on the page (see OAuthReturn): refresh.
  useEffect(() => {
    const onChange = () => void reload()
    window.addEventListener('connectors:changed', onChange)
    return () => window.removeEventListener('connectors:changed', onChange)
  }, [reload])

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

  const connected = useMemo(
    () =>
      (data ?? [])
        .filter((c) => c.connected)
        // Problems first.
        .sort((a, b) => Number(b.group === 'attention') - Number(a.group === 'attention')),
    [data],
  )

  const catalog = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return (data ?? []).filter((connector) => {
      if (!categoryMatches(connector.category, category)) return false
      if (!needle) return true
      return (
        connector.name.toLowerCase().includes(needle) ||
        categoryTitle(connector.category).toLowerCase().includes(needle) ||
        connector.category.toLowerCase().includes(needle) ||
        connector.description.toLowerCase().includes(needle)
      )
    })
  }, [data, category, query])

  const connectedPager = usePagedList(connected, { storageKey: 'connectors-connected' })
  const catalogPager = usePagedList(catalog, {
    storageKey: 'connectors-catalog',
    resetKey: `${category}|${query}`,
  })

  const categoryChips = useMemo(
    () => uniqueCategoryChips(categories.data ?? ['All']),
    [categories.data],
  )

  // The catalogue as sections, ordered by what most agents need first.
  // Group by display title so "Analytics" and "Analytics & Search" are one block.
  const sections = useMemo(() => {
    const byTitle = new Map<string, ConnectorOut[]>()
    for (const connector of catalogPager.slice) {
      const title = categoryTitle(connector.category)
      const list = byTitle.get(title) ?? []
      list.push(connector)
      byTitle.set(title, list)
    }
    return [...byTitle.entries()]
      .sort(([a], [b]) => categorySortKey(a) - categorySortKey(b) || a.localeCompare(b))
      .map(([title, items]) => ({
        title,
        description:
          CONNECTOR_CATEGORY[items[0]?.category ?? '']?.description ??
          Object.values(CONNECTOR_CATEGORY).find((entry) => entry.title === title)?.description,
        items,
      }))
  }, [catalogPager.slice])

  const connectedSections = useMemo(() => {
    const byTitle = new Map<string, ConnectorOut[]>()
    for (const connector of connectedPager.slice) {
      const title = categoryTitle(connector.category)
      const list = byTitle.get(title) ?? []
      list.push(connector)
      byTitle.set(title, list)
    }
    return [...byTitle.entries()]
      .sort(([a], [b]) => categorySortKey(a) - categorySortKey(b) || a.localeCompare(b))
      .map(([title, items]) => ({ title, items }))
  }, [connectedPager.slice])

  // Land on what matters: the health table once something is connected,
  // the catalogue for a fresh workspace.
  const activeView: View = view ?? (connected.length > 0 ? 'connected' : 'catalog')
  const attention = connected.filter((c) => c.group === 'attention').length

  const guard = (): boolean => {
    if (!writable) {
      push('View-only access - ask an admin to manage integrations', 'warning')
      return false
    }
    return true
  }

  const disconnect = async (connector: ConnectorOut) => {
    if (!guard()) return
    const ok = await confirm({
      title: `Disconnect ${connector.name}?`,
      message: 'Stored credentials will be removed from this workspace.',
      detail: 'Agents that rely on this integration will pause their work until it is reconnected.',
      confirmLabel: 'Disconnect',
      tone: 'danger',
      icon: <ConnectorIcon slug={connector.slug} name={connector.name} size={40} />,
    })
    if (!ok) return
    setBusySlug(connector.slug)
    try {
      await api.disconnect(connector.slug)
      push(`${connector.name} disconnected`, 'info')
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusySlug(null)
    }
  }

  if (loading && !data) return <Loading label="Loading integrations…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <PageHeader
        title="Integrations"
        description="Integrations give your agents access to your website, data and accounts. Most agents write with an AI service, and the website agents also need your website connected — start with those two."
        actions={
          activeView === 'connected' ? (
            <button type="button" className="btn btn-primary" onClick={() => setView('catalog')}>
              Add integration
            </button>
          ) : null
        }
      />

      {attention ? (
        <div className="banner is-danger" role="alert">
          <strong>
            {attention} integration{attention === 1 ? ' is' : 's are'} failing.
          </strong>{' '}
          Agents that depend on {attention === 1 ? 'it' : 'them'} cannot run until{' '}
          {attention === 1 ? 'it is' : 'they are'} reconnected.
        </div>
      ) : null}

      <div className="tabs tabs-underline" role="tablist" aria-label="Integrations view">
        <button
          type="button"
          role="tab"
          aria-selected={activeView === 'connected'}
          className={`tab${activeView === 'connected' ? ' is-active' : ''}`}
          onClick={() => setView('connected')}
        >
          Connected <span className="tab-count">{connected.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeView === 'catalog'}
          className={`tab${activeView === 'catalog' ? ' is-active' : ''}`}
          onClick={() => setView('catalog')}
        >
          Catalog <span className="tab-count">{data.length}</span>
        </button>
      </div>

      {activeView === 'connected' ? (
        connected.length === 0 ? (
          <div className="empty-panel">
            <div className="empty-panel-title">No integrations connected</div>
            <p className="empty-panel-text">
              Agents work on whatever you connect. Start with your website and your analytics.
            </p>
            <button type="button" className="btn btn-primary" onClick={() => setView('catalog')}>
              Browse catalog
            </button>
          </div>
        ) : (
          <>
            {connectedSections.map((section) => (
              <section key={section.title} className="catalog-section">
                <div className="catalog-section-head">
                  <h2 className="catalog-section-title">{section.title}</h2>
                </div>
                <div className="panel">
                  <div className="table-wrap">
                    <table className="table data-table">
                      <thead>
                        <tr>
                          <th>Integration</th>
                          <th>Health</th>
                          <th>Last activity</th>
                          <th className="col-actions">
                            <span className="sr-only">Actions</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {section.items.map((connector) => {
                          const health = healthOf(connector)
                          return (
                            <tr key={connector.slug}>
                              <td data-label="Integration">
                                <div className="entity-cell">
                                  <ConnectorIcon slug={connector.slug} name={connector.name} size={28} />
                                  <div className="entity-cell-text">
                                    <div className="entity-cell-title">
                                      {connector.name}
                                      <BetaPill verification={connector.verification} />
                                    </div>
                                  </div>
                                </div>
                              </td>
                              <td data-label="Health">
                                <StatusDot tone={health.tone}>{health.label}</StatusDot>
                              </td>
                              <td data-label="Last activity" className="cell-wide">
                                {connector.last_error ? (
                                  <div className="cell-error" title={softenErrorMessage(connector.last_error, CONNECTION_PROBLEM)}>
                                    {softenErrorMessage(connector.last_error, CONNECTION_PROBLEM)}
                                  </div>
                                ) : (
                                  <span className="muted">{connector.activity_label || '—'}</span>
                                )}
                              </td>
                              <td className="col-actions">
                                <div className="row-actions">
                                  <button
                                    type="button"
                                    className="btn btn-secondary btn-sm"
                                    onClick={() => {
                                      if (!guard()) return
                                      setEditing(connector)
                                    }}
                                  >
                                    Manage
                                  </button>
                                  <Menu
                                    label={`More actions for ${connector.name}`}
                                    trigger={<MoreIcon />}
                                    items={[
                                      ...(connector.docs_url
                                        ? [
                                            {
                                              label: 'API documentation',
                                              onSelect: () =>
                                                window.open(connector.docs_url, '_blank', 'noopener'),
                                            },
                                          ]
                                        : []),
                                      {
                                        label: 'Disconnect',
                                        tone: 'danger' as const,
                                        disabled: busySlug === connector.slug,
                                        onSelect: () => void disconnect(connector),
                                      },
                                    ]}
                                  />
                                </div>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              </section>
            ))}
            <ListPager
              total={connectedPager.total}
              from={connectedPager.from}
              to={connectedPager.to}
              page={connectedPager.page}
              pageCount={connectedPager.pageCount}
              pageSize={connectedPager.pageSize}
              onPage={connectedPager.setPage}
              onPageSize={connectedPager.setPageSize}
              label="Connected integration pages"
            />
          </>
        )
      ) : (
        <>
          <div className="catalog-toolbar">
            <label className="search-input">
              <SearchIcon size={16} />
              <input
                type="search"
                placeholder="Search integrations"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                aria-label="Search integrations"
              />
            </label>
            <div className="chip-row" role="tablist" aria-label="Categories">
              {categoryChips.map((chip) => (
                <button
                  key={chip.value}
                  type="button"
                  role="tab"
                  aria-selected={category === chip.value}
                  className={`chip${category === chip.value ? ' is-active' : ''}`}
                  onClick={() => setCategory(chip.value)}
                >
                  {chip.label}
                </button>
              ))}
            </div>
          </div>

          {sections.map((section) => (
          <section key={section.title} className="catalog-section">
            <div className="catalog-section-head">
              <h2 className="catalog-section-title">{section.title}</h2>
              {section.description ? (
                <p className="catalog-section-desc">{section.description}</p>
              ) : null}
            </div>
          <div className="catalog-grid">
            {section.items.map((connector) => (
              <div key={connector.slug} className="catalog-tile">
                <div className="catalog-tile-head">
                  <ConnectorIcon slug={connector.slug} name={connector.name} size={32} />
                  <div className="catalog-tile-text">
                    <div className="catalog-tile-name">
                      {connector.name}
                      <BetaPill verification={connector.verification} />
                    </div>
                  </div>
                </div>
                {connector.description ? (
                  <p className="catalog-tile-desc">{connector.description}</p>
                ) : null}
                {connector.used_by?.length ? (
                  <div className="catalog-tile-used" title={connector.used_by.map((a) => a.name).join(', ')}>
                    Used by {connector.used_by.length} agent{connector.used_by.length === 1 ? '' : 's'}
                  </div>
                ) : connector.abilities?.includes('Read website visits') ? (
                  <div className="catalog-tile-used">Used for your reports</div>
                ) : null}
                <div className="catalog-tile-foot">
                  {connector.connected ? (
                    <>
                      <StatusDot tone={healthOf(connector).tone}>
                        {healthOf(connector).tone === 'success' ? 'Connected' : healthOf(connector).label}
                      </StatusDot>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => {
                          if (!guard()) return
                          setEditing(connector)
                        }}
                      >
                        Manage
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => {
                        if (!guard()) return
                        setEditing(connector)
                      }}
                    >
                      Connect
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
          </section>
          ))}
          {catalog.length === 0 ? (
            <div className="empty-panel">
              <div className="empty-panel-title">No integrations match</div>
              <p className="empty-panel-text">Try another search term or category.</p>
            </div>
          ) : (
            <ListPager
              total={catalogPager.total}
              from={catalogPager.from}
              to={catalogPager.to}
              page={catalogPager.page}
              pageCount={catalogPager.pageCount}
              pageSize={catalogPager.pageSize}
              onPage={catalogPager.setPage}
              onPageSize={catalogPager.setPageSize}
              label="Integration catalog pages"
            />
          )}
        </>
      )}

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
