/**
 * Platform Portal Admin — workspaces across the product, and catalogue toggles.
 *
 * Only emails listed in PORTAL_ADMIN_EMAILS see this screen. Disabling a
 * connector, agent, or module hides it from every customer workspace.
 */
import { useMemo, useState } from 'react'
import { Navigate } from 'react-router-dom'

import { api } from '@/api/client'
import type { PortalFeature, PortalOrganization } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  ErrorState,
  Loading,
  SectionHeading,
  Segmented,
  Tag,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

type Tab = 'workspaces' | 'features'

export function PortalAdminPage() {
  const { session, refresh } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const [tab, setTab] = useState<Tab>('workspaces')
  const [selectedOrg, setSelectedOrg] = useState<PortalOrganization | null>(null)
  const [featureFilter, setFeatureFilter] = useState<'all' | 'connector' | 'agent' | 'module'>(
    'all',
  )
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const isPortalAdmin = Boolean(session?.is_portal_admin)

  const overview = useResource(
    () => (isPortalAdmin ? api.portalOverview() : Promise.resolve(null)),
    [isPortalAdmin],
  )
  const orgs = useResource(
    () => (isPortalAdmin ? api.portalOrganizations() : Promise.resolve([])),
    [isPortalAdmin],
  )
  const features = useResource(
    () => (isPortalAdmin ? api.portalFeatures() : Promise.resolve([])),
    [isPortalAdmin],
  )
  const users = useResource(
    () =>
      isPortalAdmin && selectedOrg
        ? api.portalOrganizationUsers(selectedOrg.id)
        : Promise.resolve([]),
    [isPortalAdmin, selectedOrg?.id],
  )

  const filteredFeatures = useMemo(() => {
    const rows = features.data ?? []
    if (featureFilter === 'all') return rows
    return rows.filter((row) => row.kind === featureFilter)
  }, [features.data, featureFilter])

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

  return (
    <>
      <p className="page-intro">
        Platform control for every workspace. Turn connectors, agents and modules off
        here and they disappear from the customer console until you turn them back on.
      </p>

      <div className="grid-cards" style={{ marginBottom: 24 }}>
        <Blueprint className="card elev-sm">
          <div className="card-kicker">Workspaces</div>
          <div className="card-title">{overview.data?.organizations ?? '—'}</div>
        </Blueprint>
        <Blueprint className="card elev-sm">
          <div className="card-kicker">Users</div>
          <div className="card-title">{overview.data?.users ?? '—'}</div>
        </Blueprint>
        <Blueprint className="card elev-sm">
          <div className="card-kicker">Features off</div>
          <div className="card-title">
            {overview.data
              ? `${overview.data.features_disabled} / ${overview.data.features_total}`
              : '—'}
          </div>
        </Blueprint>
      </div>

      <Segmented
        name="portal-tab"
        value={tab}
        options={[
          { value: 'workspaces', label: 'Workspaces' },
          { value: 'features', label: 'Features' },
        ]}
        onChange={(next) => {
          setTab(next as Tab)
          setSelectedOrg(null)
        }}
      />

      {tab === 'workspaces' ? (
        <div style={{ marginTop: 20 }}>
          <SectionHeading title="Workspaces" description={`${orgs.data?.length ?? 0} total`} />
          {orgs.loading && !orgs.data ? <Loading label="Loading workspaces…" /> : null}
          {orgs.error ? <ErrorState message={orgs.error} onRetry={orgs.reload} /> : null}
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Plan</th>
                  <th>Members</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(orgs.data ?? []).map((org) => (
                  <tr key={org.id}>
                    <td>
                      <strong>{org.name}</strong>
                      <div className="small muted">{org.slug}</div>
                    </td>
                    <td>
                      {org.plan_name}
                      <div className="small muted">{org.plan_tier}</div>
                    </td>
                    <td>
                      {org.member_count} / {org.seats_total}
                    </td>
                    <td>
                      <Tag tone={org.is_active ? 'accent' : 'outline'}>
                        {org.is_active ? 'Active' : 'Inactive'}
                      </Tag>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => setSelectedOrg(org)}
                      >
                        Users
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {selectedOrg ? (
            <div style={{ marginTop: 28 }}>
              <SectionHeading
                title={`Users — ${selectedOrg.name}`}
                description={`${users.data?.length ?? 0} members`}
              />
              <button
                type="button"
                className="btn btn-secondary"
                style={{ marginBottom: 12 }}
                onClick={() => setSelectedOrg(null)}
              >
                Close
              </button>
              {users.loading && !users.data ? <Loading label="Loading users…" /> : null}
              {users.error ? (
                <ErrorState message={users.error} onRetry={users.reload} />
              ) : null}
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Email</th>
                      <th>Role</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(users.data ?? []).map((member) => (
                      <tr key={member.id}>
                        <td>
                          {member.name}
                          {member.is_owner ? (
                            <span className="small muted"> · owner</span>
                          ) : null}
                        </td>
                        <td>{member.email}</td>
                        <td>{member.role_label}</td>
                        <td>
                          <Tag tone={member.is_active ? 'accent' : 'outline'}>
                            {member.is_active ? 'Active' : 'Inactive'}
                          </Tag>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <div style={{ marginTop: 20 }}>
          <SectionHeading
            title="Catalogue & modules"
            description={`${filteredFeatures.length} shown`}
          />
          <p className="muted" style={{ marginBottom: 12 }}>
            Off items are hidden from every workspace. Already-connected data stays in
            the database but customers cannot see or use them until you enable again.
          </p>
          <Segmented
            name="feature-kind"
            value={featureFilter}
            options={[
              { value: 'all', label: 'All' },
              { value: 'connector', label: 'Connectors' },
              { value: 'agent', label: 'Agents' },
              { value: 'module', label: 'Modules' },
            ]}
            onChange={(next) =>
              setFeatureFilter(next as 'all' | 'connector' | 'agent' | 'module')
            }
          />
          {features.loading && !features.data ? (
            <Loading label="Loading features…" />
          ) : null}
          {features.error ? (
            <ErrorState message={features.error} onRetry={features.reload} />
          ) : null}
          <div className="table-wrap" style={{ marginTop: 16 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Name</th>
                  <th>Slug</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {filteredFeatures.map((feature) => {
                  const key = `${feature.kind}/${feature.slug}`
                  return (
                    <tr key={key}>
                      <td>{feature.kind}</td>
                      <td>
                        <strong>{feature.name}</strong>
                      </td>
                      <td className="muted">{feature.slug}</td>
                      <td>
                        <Tag tone={feature.enabled ? 'accent' : 'outline'}>
                          {feature.enabled ? 'On' : 'Off'}
                        </Tag>
                      </td>
                      <td>
                        <button
                          type="button"
                          className={`btn ${feature.enabled ? 'btn-secondary' : 'btn-primary'}`}
                          disabled={busyKey === key}
                          onClick={() => {
                            if (
                              feature.enabled &&
                              !window.confirm(
                                `Turn off ${feature.name}? Customers will no longer see it.`,
                              )
                            ) {
                              return
                            }
                            void toggle(feature)
                          }}
                        >
                          {busyKey === key
                            ? 'Saving…'
                            : feature.enabled
                              ? 'Disable'
                              : 'Enable'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {!filteredFeatures.length && features.data ? (
            <p className="muted">No features in this filter.</p>
          ) : null}
          {!features.data?.length && !features.loading ? (
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => push('Nothing to toggle yet', 'info')}
            >
              Refresh
            </button>
          ) : null}
        </div>
      )}
    </>
  )
}
