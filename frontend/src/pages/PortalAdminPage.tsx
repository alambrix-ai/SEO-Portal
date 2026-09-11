/**
 * Platform Portal Admin - workspaces across the product, and catalogue toggles.
 *
 * Only emails listed in PORTAL_ADMIN_EMAILS see this screen. Disabling a
 * connector, agent, or module hides it from every customer workspace.
 */
import { useMemo, useState } from 'react'
import { Navigate } from 'react-router-dom'

import { api } from '@/api/client'
import type { PortalFeature, PortalOrganization } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { AgentIcon } from '@/components/AgentIcon'
import { CardSection } from '@/components/CardSection'
import { useConfirm } from '@/components/Confirm'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import {
  AdminIcon,
  AdsIcon,
  AgentsIcon,
  ApprovalsIcon,
  ConnectorsIcon,
  DashboardIcon,
  OffPageIcon,
  OnboardingIcon,
  ReportsIcon,
  SeoIcon,
} from '@/components/icons'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  Dialog,
  EmptyState,
  ErrorState,
  Loading,
  SectionHeading,
  Segmented,
  StatTile,
  Tag,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

type Tab = 'workspaces' | 'features'
type FeatureKindFilter = 'all' | 'connector' | 'agent' | 'module'

const KIND_LABEL: Record<string, string> = {
  connector: 'Connector',
  agent: 'Agent',
  module: 'Module',
}

/** Same marks as Connectors / Agents / sidebar - not letter placeholders. */
const MODULE_ICONS: Record<string, typeof DashboardIcon> = {
  dashboard: DashboardIcon,
  onboarding: OnboardingIcon,
  agents: AgentsIcon,
  seo: SeoIcon,
  offpage: OffPageIcon,
  ads: AdsIcon,
  connectors: ConnectorsIcon,
  approvals: ApprovalsIcon,
  reports: ReportsIcon,
  admin: AdminIcon,
}

function PortalFeatureIcon({ feature }: { feature: PortalFeature }) {
  if (feature.kind === 'connector') {
    return <ConnectorIcon slug={feature.slug} name={feature.name} size={40} />
  }
  if (feature.kind === 'agent') {
    return <AgentIcon slug={feature.slug} size={40} />
  }
  if (feature.kind === 'module') {
    const Icon = MODULE_ICONS[feature.slug] ?? DashboardIcon
    return (
      <span className="connector-icon agent-icon" style={{ width: 40, height: 40 }}>
        <Icon size={22} />
      </span>
    )
  }
  return (
    <span className="connector-icon" style={{ width: 40, height: 40 }}>
      <span className="connector-icon-letters">{(feature.name || feature.slug).slice(0, 1)}</span>
    </span>
  )
}

export function PortalAdminPage() {
  const { session, refresh } = useAuth()
  const { fromResult, fromError } = useToasts()
  const confirm = useConfirm()
  const [tab, setTab] = useState<Tab>('workspaces')
  const [selectedOrg, setSelectedOrg] = useState<PortalOrganization | null>(null)
  const [featureFilter, setFeatureFilter] = useState<FeatureKindFilter>('all')
  const [workspaceQuery, setWorkspaceQuery] = useState('')
  const [featureQuery, setFeatureQuery] = useState('')
  /** Off items leave the catalogue; turn this on only to restore them. */
  const [showDisabled, setShowDisabled] = useState(false)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const isPortalAdmin = Boolean(session?.is_portal_admin)

  const overview = useResource(
    (signal) => (isPortalAdmin ? api.portalOverview(signal) : Promise.resolve(null)),
    [isPortalAdmin],
    'portal:overview',
  )
  const orgs = useResource(
    (signal) =>
      isPortalAdmin ? api.portalOrganizations(signal) : Promise.resolve([]),
    [isPortalAdmin],
    'portal:orgs',
  )
  const features = useResource(
    (signal) =>
      isPortalAdmin && tab === 'features'
        ? api.portalFeatures(signal)
        : Promise.resolve([]),
    [isPortalAdmin, tab],
    tab === 'features' ? 'portal:features' : undefined,
  )
  const users = useResource(
    (signal) =>
      isPortalAdmin && selectedOrg
        ? api.portalOrganizationUsers(selectedOrg.id, signal)
        : Promise.resolve([]),
    [isPortalAdmin, selectedOrg?.id],
    selectedOrg ? `portal:users:${selectedOrg.id}` : undefined,
  )

  const filteredOrgs = useMemo(() => {
    const rows = orgs.data ?? []
    const q = workspaceQuery.trim().toLowerCase()
    if (!q) return rows
    return rows.filter(
      (org) =>
        org.name.toLowerCase().includes(q) ||
        org.slug.toLowerCase().includes(q) ||
        org.plan_name.toLowerCase().includes(q),
    )
  }, [orgs.data, workspaceQuery])

  const filteredFeatures = useMemo(() => {
    const rows = features.data ?? []
    const q = featureQuery.trim().toLowerCase()
    return rows.filter((row) => {
      if (!showDisabled && !row.enabled) return false
      if (featureFilter !== 'all' && row.kind !== featureFilter) return false
      if (!q) return true
      return (
        row.name.toLowerCase().includes(q) ||
        row.slug.toLowerCase().includes(q) ||
        row.kind.toLowerCase().includes(q)
      )
    })
  }, [features.data, featureFilter, featureQuery, showDisabled])

  const hiddenCount = useMemo(
    () => (features.data ?? []).filter((row) => !row.enabled).length,
    [features.data],
  )

  const featuresByKind = useMemo(() => {
    const groups: { kind: string; rows: PortalFeature[] }[] = []
    const order = ['module', 'connector', 'agent'] as const
    for (const kind of order) {
      const rows = filteredFeatures.filter((row) => row.kind === kind)
      if (rows.length) groups.push({ kind, rows })
    }
    const known = new Set<string>(order)
    const other = filteredFeatures.filter((row) => !known.has(row.kind))
    if (other.length) groups.push({ kind: 'other', rows: other })
    return groups
  }, [filteredFeatures])

  const disabledCount = useMemo(
    () => (features.data ?? []).filter((row) => !row.enabled).length,
    [features.data],
  )

  if (!isPortalAdmin) {
    return <Navigate to="/dashboard" replace />
  }

  const toggle = async (feature: PortalFeature) => {
    const key = `${feature.kind}/${feature.slug}`
    setBusyKey(key)
    try {
      fromResult(await api.setPortalFeature(feature.kind, feature.slug, !feature.enabled))
      await features.reload()
      await overview.reload()
      await refresh()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyKey(null)
    }
  }

  if (overview.loading && !overview.data) {
    return <Loading label="Loading portal…" />
  }
  if (overview.error && !overview.data) {
    return <ErrorState message={overview.error} onRetry={overview.reload} />
  }

  const featuresTotal = overview.data?.features_total ?? features.data?.length ?? 0
  const featuresOff = overview.data?.features_disabled ?? disabledCount

  return (
    <div className="portal-page">
      <Blueprint className="portal-masthead elev-sm">
        <div className="portal-masthead-copy">
          <div className="portal-eyebrow">Platform control</div>
          <h2 className="portal-masthead-title">Portal Admin</h2>
          <p className="portal-masthead-lede">
            Govern every workspace from one console. Disable a connector, agent, or
            module here and it disappears from the customer product until you
            restore it.
          </p>
        </div>
        <div className="portal-masthead-aside">
          <Tag tone="accent">Operator access</Tag>
          <div className="portal-masthead-meta">
            Signed in as
            <strong> {session?.user.email}</strong>
          </div>
        </div>
      </Blueprint>

      <div className="grid-tiles portal-kpis">
        <StatTile
          kicker="Workspaces"
          value={overview.data?.organizations ?? '-'}
          meta="Customer organizations"
        />
        <StatTile
          kicker="Users"
          value={overview.data?.users ?? '-'}
          meta="Across all workspaces"
        />
        <StatTile
          kicker="Catalogue live"
          value={featuresTotal ? Math.max(0, featuresTotal - featuresOff) : '-'}
          meta={`${featuresOff} currently off`}
        />
        <StatTile
          kicker="Features off"
          value={featuresTotal ? `${featuresOff} / ${featuresTotal}` : '-'}
          meta="Hidden from customers"
        />
      </div>

      <div className="portal-toolbar">
        <div className="portal-tabs" role="tablist" aria-label="Portal sections">
          {(
            [
              { value: 'workspaces' as const, label: 'Workspaces', hint: 'Tenants & members' },
              { value: 'features' as const, label: 'Catalogue', hint: 'Connectors · agents · modules' },
            ] as const
          ).map((item) => (
            <button
              key={item.value}
              type="button"
              role="tab"
              aria-selected={tab === item.value}
              className={`portal-tab${tab === item.value ? ' is-active' : ''}`}
              onClick={() => {
                setTab(item.value)
                setSelectedOrg(null)
              }}
            >
              <span className="portal-tab-label">{item.label}</span>
              <span className="portal-tab-hint">{item.hint}</span>
            </button>
          ))}
        </div>
      </div>

      {tab === 'workspaces' ? (
        <section className="portal-panel" aria-label="Workspaces">
          <SectionHeading
            title="Customer workspaces"
            description="Open any tenant to review members, plan, and seat usage."
            action={
              <div className="portal-search">
                <input
                  className="input"
                  type="search"
                  placeholder="Search name, slug, or plan…"
                  value={workspaceQuery}
                  onChange={(event) => setWorkspaceQuery(event.target.value)}
                  aria-label="Search workspaces"
                />
              </div>
            }
          />

          {orgs.loading && !orgs.data ? <Loading label="Loading workspaces…" /> : null}
          {orgs.error ? <ErrorState message={orgs.error} onRetry={orgs.reload} /> : null}

          {!orgs.loading && filteredOrgs.length === 0 ? (
            <EmptyState
              title={workspaceQuery ? 'No matching workspaces' : 'No workspaces yet'}
              description={
                workspaceQuery
                  ? 'Try a different name, slug, or plan.'
                  : 'Organizations will appear here as customers sign up.'
              }
            />
          ) : (
            <Blueprint className="card elev-sm portal-surface">
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Workspace</th>
                      <th>Plan</th>
                      <th>Seats</th>
                      <th>Status</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {filteredOrgs.map((org) => {
                      const seatPct =
                        org.seats_total > 0
                          ? Math.round((org.member_count / org.seats_total) * 100)
                          : 0
                      return (
                        <tr key={org.id}>
                          <td>
                            <div className="cell-title">{org.name}</div>
                            <div className="small muted">{org.slug}</div>
                          </td>
                          <td>
                            <div className="cell-title" style={{ fontSize: 13 }}>
                              {org.plan_name}
                            </div>
                            <div className="small muted">{org.plan_tier}</div>
                          </td>
                          <td>
                            <div className="portal-seat">
                              <span>
                                {org.member_count}
                                <span className="muted"> / {org.seats_total}</span>
                              </span>
                              <div
                                className="portal-seat-meter"
                                role="meter"
                                aria-valuenow={seatPct}
                                aria-valuemin={0}
                                aria-valuemax={100}
                                aria-label={`${seatPct}% of seats used`}
                              >
                                <span style={{ width: `${Math.min(100, seatPct)}%` }} />
                              </div>
                            </div>
                          </td>
                          <td>
                            <Tag tone={org.is_active ? 'accent-2' : 'outline'}>
                              {org.is_active ? 'Active' : 'Inactive'}
                            </Tag>
                          </td>
                          <td className="table-actions">
                            <button
                              type="button"
                              className="btn btn-secondary"
                              onClick={() => setSelectedOrg(org)}
                            >
                              View members
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </Blueprint>
          )}
        </section>
      ) : (
        <section className="portal-panel" aria-label="Catalogue">
          <SectionHeading
            title="Product catalogue"
            description="Turning a row Off removes it from this list and from every customer console. Open Show disabled only when you need to restore one."
            action={
              <div className="portal-search">
                <input
                  className="input"
                  type="search"
                  placeholder="Search catalogue…"
                  value={featureQuery}
                  onChange={(event) => setFeatureQuery(event.target.value)}
                  aria-label="Search catalogue"
                />
              </div>
            }
          />

          <div className="portal-filter-bar">
            <Segmented
              name="feature-kind"
              value={featureFilter}
              options={[
                { value: 'all', label: 'All' },
                { value: 'connector', label: 'Connectors' },
                { value: 'agent', label: 'Agents' },
                { value: 'module', label: 'Modules' },
              ]}
              onChange={(next) => setFeatureFilter(next as FeatureKindFilter)}
            />
            <div className="portal-filter-actions">
              <label className="portal-show-disabled">
                <input
                  type="checkbox"
                  checked={showDisabled}
                  onChange={(event) => setShowDisabled(event.target.checked)}
                />
                <span>
                  Show disabled
                  {hiddenCount > 0 ? ` (${hiddenCount})` : ''}
                </span>
              </label>
              <div className="portal-filter-summary muted small">
                Showing {filteredFeatures.length}
                {features.data
                  ? ` · ${showDisabled ? features.data.length : features.data.length - hiddenCount} live`
                  : ''}
              </div>
            </div>
          </div>

          {features.loading && !features.data ? (
            <Loading label="Loading catalogue…" />
          ) : null}
          {features.error ? (
            <ErrorState message={features.error} onRetry={features.reload} />
          ) : null}

          {!features.loading && filteredFeatures.length === 0 ? (
            <EmptyState
              title={
                !showDisabled && hiddenCount > 0
                  ? 'No live catalogue items in this view'
                  : 'Nothing in this view'
              }
              description={
                !showDisabled && hiddenCount > 0
                  ? 'Turn on Show disabled to restore hidden connectors, agents, or modules.'
                  : 'Change the filter or search to find connectors, agents, or modules.'
              }
              action={
                !showDisabled && hiddenCount > 0 ? (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => setShowDisabled(true)}
                  >
                    Show disabled ({hiddenCount})
                  </button>
                ) : undefined
              }
            />
          ) : null}

          {featuresByKind.map((group) => (
            <CardSection
              key={group.kind}
              title={
                group.kind === 'other'
                  ? 'Other'
                  : `${KIND_LABEL[group.kind] ?? group.kind}s`
              }
              count={group.rows.length}
              description={
                group.kind === 'module'
                  ? 'Console modules customers can open from the sidebar.'
                  : group.kind === 'connector'
                    ? 'Integrations offered on the Connectors screen.'
                    : group.kind === 'agent'
                      ? 'Agents available to configure and run.'
                      : undefined
              }
            >
              <Blueprint className="card elev-sm portal-surface">
                <ul className="portal-feature-list">
                  {group.rows.map((feature) => {
                    const key = `${feature.kind}/${feature.slug}`
                    const busy = busyKey === key
                    return (
                      <li key={key} className="portal-feature-row">
                        <div className="portal-feature-icon">
                          <PortalFeatureIcon feature={feature} />
                        </div>
                        <div className="portal-feature-copy">
                          <div className="portal-feature-title">{feature.name}</div>
                          <div className="portal-feature-meta">
                            <Tag tone="outline">{KIND_LABEL[feature.kind] ?? feature.kind}</Tag>
                            <span className="mono-sub">{feature.slug}</span>
                          </div>
                        </div>
                        <div className="portal-feature-status">
                          <Tag tone={feature.enabled ? 'accent-2' : 'neutral'}>
                            {feature.enabled ? 'Visible' : 'Hidden'}
                          </Tag>
                        </div>
                        <Segmented
                          name={`feature-${key}`}
                          value={feature.enabled ? 'on' : 'off'}
                          disabled={busy}
                          options={[
                            { value: 'on', label: busy ? '…' : 'On' },
                            { value: 'off', label: busy ? '…' : 'Off' },
                          ]}
                          onChange={(next) => {
                            const turnOn = next === 'on'
                            if (turnOn === feature.enabled) return
                            void (async () => {
                              if (!turnOn) {
                                const ok = await confirm({
                                  title: `Turn off ${feature.name}?`,
                                  message:
                                    'Customers will no longer see this in their console.',
                                  detail:
                                    'Existing data stays in the database. You can restore it later with Show disabled.',
                                  confirmLabel: 'Turn off',
                                  tone: 'danger',
                                  icon:
                                    feature.kind === 'connector' ? (
                                      <ConnectorIcon
                                        slug={feature.slug}
                                        name={feature.name}
                                        size={40}
                                      />
                                    ) : feature.kind === 'agent' ? (
                                      <AgentIcon slug={feature.slug} size={40} />
                                    ) : (
                                      <PortalFeatureIcon feature={feature} />
                                    ),
                                })
                                if (!ok) return
                              }
                              await toggle(feature)
                            })()
                          }}
                        />
                      </li>
                    )
                  })}
                </ul>
              </Blueprint>
            </CardSection>
          ))}
        </section>
      )}

      {selectedOrg ? (
        <Dialog
          title={`Members - ${selectedOrg.name}`}
          width={720}
          onClose={() => setSelectedOrg(null)}
          actions={
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setSelectedOrg(null)}
            >
              Close
            </button>
          }
        >
          <div className="portal-dialog-summary">
            <div>
              <div className="card-kicker">Plan</div>
              <div className="cell-title">
                {selectedOrg.plan_name}
                <span className="muted"> · {selectedOrg.plan_tier}</span>
              </div>
            </div>
            <div>
              <div className="card-kicker">Seats</div>
              <div className="cell-title">
                {selectedOrg.member_count} / {selectedOrg.seats_total}
              </div>
            </div>
            <div>
              <div className="card-kicker">Status</div>
              <Tag tone={selectedOrg.is_active ? 'accent-2' : 'outline'}>
                {selectedOrg.is_active ? 'Active' : 'Inactive'}
              </Tag>
            </div>
          </div>

          {users.loading && !users.data ? <Loading label="Loading members…" /> : null}
          {users.error ? <ErrorState message={users.error} onRetry={users.reload} /> : null}

          {users.data?.length ? (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Last sign-in</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {users.data.map((member) => (
                    <tr key={member.id}>
                      <td>
                        <span className="cell-title">{member.name}</span>
                        {member.is_owner ? (
                          <div className="row" style={{ marginTop: 4 }}>
                            <Tag tone="outline">Owner</Tag>
                          </div>
                        ) : null}
                      </td>
                      <td className="small muted">{member.email}</td>
                      <td>{member.role_label}</td>
                      <td className="small muted">
                        {member.last_login_at
                          ? new Date(member.last_login_at).toLocaleString()
                          : 'Never'}
                      </td>
                      <td>
                        <Tag tone={member.is_active ? 'accent-2' : 'outline'}>
                          {member.is_active ? 'Active' : 'Inactive'}
                        </Tag>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}

          {!users.loading && users.data && users.data.length === 0 ? (
            <p className="muted">No members in this workspace.</p>
          ) : null}
        </Dialog>
      ) : null}
    </div>
  )
}
