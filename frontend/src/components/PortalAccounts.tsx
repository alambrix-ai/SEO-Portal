/**
 * Synaprank operator directory: people first, workspaces nested underneath.
 *
 * Customers never open this. It is the platform tree of every login and the
 * seats that login holds.
 */
import { useMemo, useState } from 'react'

import { api } from '@/api/client'
import type { PortalAccount, PortalOrganization, PortalUser } from '@/api/types'
import { ListPager } from '@/components/ListPager'
import { ChevronDownIcon } from '@/components/icons'
import {
  Blueprint,
  EmptyState,
  ErrorState,
  Loading,
  SectionHeading,
  Segmented,
  Tag,
} from '@/components/ui'
import { usePagedList } from '@/hooks/usePagedList'
import { useResource } from '@/hooks/useResource'

type Directory = 'people' | 'workspaces'

function formatLogin(iso: string | null): string {
  if (!iso) return 'Never signed in'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return 'Never signed in'
  return when.toLocaleString()
}

function signInLabel(provider: string): string {
  if (!provider) return 'Email code'
  const labels: Record<string, string> = {
    google: 'Google',
    microsoft: 'Microsoft',
    meta: 'Meta',
    linkedin: 'LinkedIn',
    okta: 'Okta',
  }
  return labels[provider] ?? provider
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  const first = parts[0]?.[0] ?? '?'
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? '') : ''
  return (first + last).toUpperCase()
}

function personMatches(account: PortalAccount, query: string): boolean {
  if (!query) return true
  if (account.name.toLowerCase().includes(query) || account.email.toLowerCase().includes(query)) {
    return true
  }
  return account.seats.some(
    (seat) =>
      seat.organization_name.toLowerCase().includes(query) ||
      seat.organization_slug.toLowerCase().includes(query),
  )
}

export function PortalAccounts({
  orgs,
  orgsLoading,
  orgsError,
  onRetryOrgs,
  initialDirectory = 'people',
}: {
  orgs: PortalOrganization[] | null
  orgsLoading: boolean
  orgsError: string | null
  onRetryOrgs: () => void
  initialDirectory?: Directory
}) {
  const [directory, setDirectory] = useState<Directory>(initialDirectory)
  const [query, setQuery] = useState('')
  const [openPeople, setOpenPeople] = useState<Set<string>>(() => new Set())
  const [openWorkspaces, setOpenWorkspaces] = useState<Set<string>>(() => new Set())

  const accounts = useResource(
    (signal) => api.portalAccounts(signal),
    [],
    'portal:accounts',
  )

  const filteredPeople = useMemo(() => {
    const rows = accounts.data ?? []
    const q = query.trim().toLowerCase()
    return rows.filter((row) => personMatches(row, q))
  }, [accounts.data, query])

  const filteredOrgs = useMemo(() => {
    const rows = orgs ?? []
    const q = query.trim().toLowerCase()
    if (!q) return rows
    return rows.filter(
      (org) =>
        org.name.toLowerCase().includes(q) ||
        org.slug.toLowerCase().includes(q) ||
        org.plan_name.toLowerCase().includes(q),
    )
  }, [orgs, query])

  const peoplePager = usePagedList(filteredPeople, {
    storageKey: 'portal-accounts',
    resetKey: query,
  })
  const orgPager = usePagedList(filteredOrgs, {
    storageKey: 'portal-orgs',
    resetKey: query,
  })

  const togglePerson = (email: string) => {
    setOpenPeople((current) => {
      const next = new Set(current)
      if (next.has(email)) next.delete(email)
      else next.add(email)
      return next
    })
  }

  const toggleWorkspace = (id: string) => {
    setOpenWorkspaces((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <section className="portal-panel" aria-label="Accounts">
      <SectionHeading
        title="People and workspaces"
        description="Each person is listed once. Open the row to see every workspace they can sign in to, their role, and their last login."
        action={
          <div className="portal-search">
            <input
              className="input"
              type="search"
              placeholder="Search name, email, or workspace…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Search people and workspaces"
            />
          </div>
        }
      />

      <div className="portal-filter-bar">
        <Segmented
          name="portal-directory"
          value={directory}
          options={[
            { value: 'people', label: 'By person' },
            { value: 'workspaces', label: 'By workspace' },
          ]}
          onChange={(next) => setDirectory(next as Directory)}
        />
        <div className="portal-filter-summary muted small">
          {directory === 'people'
            ? `${filteredPeople.length} ${filteredPeople.length === 1 ? 'person' : 'people'}`
            : `${filteredOrgs.length} ${filteredOrgs.length === 1 ? 'workspace' : 'workspaces'}`}
        </div>
      </div>

      {directory === 'people' ? (
        <PeopleTree
          loading={accounts.loading && !accounts.data}
          error={accounts.error}
          onRetry={accounts.reload}
          query={query}
          rows={peoplePager.slice}
          total={filteredPeople.length}
          open={openPeople}
          onToggle={togglePerson}
          pager={peoplePager}
        />
      ) : (
        <WorkspaceTree
          loading={orgsLoading && !orgs}
          error={orgsError}
          onRetry={onRetryOrgs}
          query={query}
          rows={orgPager.slice}
          total={filteredOrgs.length}
          open={openWorkspaces}
          onToggle={toggleWorkspace}
          pager={orgPager}
        />
      )}
    </section>
  )
}

function PeopleTree({
  loading,
  error,
  onRetry,
  query,
  rows,
  total,
  open,
  onToggle,
  pager,
}: {
  loading: boolean
  error: string | null
  onRetry: () => void
  query: string
  rows: PortalAccount[]
  total: number
  open: Set<string>
  onToggle: (email: string) => void
  pager: ReturnType<typeof usePagedList<PortalAccount>>
}) {
  if (loading) return <Loading label="Loading people…" />
  if (error) return <ErrorState message={error} onRetry={onRetry} />
  if (total === 0) {
    return (
      <EmptyState
        title={query ? 'No matching people' : 'No people yet'}
        description={
          query
            ? 'Try a different name, email, or workspace.'
            : 'People appear here as customers create workspaces and invite teammates.'
        }
      />
    )
  }

  return (
    <Blueprint className="card elev-sm portal-surface">
      <ul className="portal-tree" role="tree" aria-label="People">
        {rows.map((account) => {
          const q = query.trim().toLowerCase()
          const seatHit =
            Boolean(q) &&
            account.seats.some(
              (seat) =>
                seat.organization_name.toLowerCase().includes(q) ||
                seat.organization_slug.toLowerCase().includes(q),
            )
          const expanded = open.has(account.email) || seatHit
          return (
            <li key={account.email} className="portal-tree-node" role="treeitem" aria-expanded={expanded} aria-level={1}>
              <div className="portal-tree-row">
                <button
                  type="button"
                  className="portal-tree-toggle"
                  aria-expanded={expanded}
                  aria-label={
                    expanded
                      ? `Hide workspaces for ${account.name}`
                      : `Show workspaces for ${account.name}`
                  }
                  onClick={() => onToggle(account.email)}
                >
                  <ChevronDownIcon size={16} />
                </button>
                <span className="portal-tree-avatar" aria-hidden="true">
                  {initials(account.name)}
                </span>
                <div className="portal-tree-copy">
                  <div className="portal-tree-title">{account.name}</div>
                  <div className="small muted">{account.email}</div>
                </div>
                <div className="portal-tree-meta">
                  <Tag tone={account.is_locked ? 'danger' : 'outline'}>
                    {account.workspace_count}{' '}
                    {account.workspace_count === 1 ? 'workspace' : 'workspaces'}
                  </Tag>
                  {account.is_locked ? <Tag tone="warning">Locked</Tag> : null}
                </div>
                <div className="portal-tree-login">
                  <div className="portal-tree-login-label">{signInLabel(account.sso_provider)}</div>
                  <div className="small muted">{formatLogin(account.last_login_at)}</div>
                </div>
              </div>
              {expanded ? (
                <ul className="portal-tree-children" role="group">
                  {account.seats.map((seat) => (
                    <li key={seat.user_id} className="portal-tree-seat" role="treeitem" aria-level={2}>
                      <div className="portal-tree-seat-copy">
                        <div className="portal-tree-title">{seat.organization_name}</div>
                        <div className="small muted">
                          {seat.organization_slug}
                          <span> · {seat.plan_name}</span>
                        </div>
                      </div>
                      <div className="portal-tree-seat-role">
                        <span>{seat.role_label}</span>
                        {seat.is_owner ? <Tag tone="outline">Owner</Tag> : null}
                      </div>
                      <Tag
                        tone={
                          !seat.organization_active || !seat.is_active
                            ? 'outline'
                            : 'accent-2'
                        }
                      >
                        {!seat.organization_active
                          ? 'Workspace inactive'
                          : seat.locked
                            ? 'Locked'
                            : seat.is_active
                              ? 'Active'
                              : 'Inactive'}
                      </Tag>
                      <div className="portal-tree-login">
                        <div className="portal-tree-login-label">{signInLabel(seat.sso_provider)}</div>
                        <div className="small muted">{formatLogin(seat.last_login_at)}</div>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          )
        })}
      </ul>
      <ListPager
        total={pager.total}
        from={pager.from}
        to={pager.to}
        page={pager.page}
        pageCount={pager.pageCount}
        pageSize={pager.pageSize}
        onPage={pager.setPage}
        onPageSize={pager.setPageSize}
        label="People pages"
      />
    </Blueprint>
  )
}

function WorkspaceTree({
  loading,
  error,
  onRetry,
  query,
  rows,
  total,
  open,
  onToggle,
  pager,
}: {
  loading: boolean
  error: string | null
  onRetry: () => void
  query: string
  rows: PortalOrganization[]
  total: number
  open: Set<string>
  onToggle: (id: string) => void
  pager: ReturnType<typeof usePagedList<PortalOrganization>>
}) {
  if (loading) return <Loading label="Loading workspaces…" />
  if (error) return <ErrorState message={error} onRetry={onRetry} />
  if (total === 0) {
    return (
      <EmptyState
        title={query ? 'No matching workspaces' : 'No workspaces yet'}
        description={
          query
            ? 'Try a different name, slug, or plan.'
            : 'Organizations appear here as customers sign up.'
        }
      />
    )
  }

  return (
    <Blueprint className="card elev-sm portal-surface">
      <ul className="portal-tree" role="tree" aria-label="Workspaces">
        {rows.map((org) => {
          const expanded = open.has(org.id)
          const seatPct =
            org.seats_total > 0 ? Math.round((org.member_count / org.seats_total) * 100) : 0
          return (
            <li key={org.id} className="portal-tree-node" role="treeitem" aria-expanded={expanded} aria-level={1}>
              <div className="portal-tree-row">
                <button
                  type="button"
                  className="portal-tree-toggle"
                  aria-expanded={expanded}
                  aria-label={
                    expanded ? `Hide members of ${org.name}` : `Show members of ${org.name}`
                  }
                  onClick={() => onToggle(org.id)}
                >
                  <ChevronDownIcon size={16} />
                </button>
                <span className="portal-tree-avatar is-workspace" aria-hidden="true">
                  {initials(org.name)}
                </span>
                <div className="portal-tree-copy">
                  <div className="portal-tree-title">{org.name}</div>
                  <div className="small muted">{org.slug}</div>
                </div>
                <div className="portal-tree-meta">
                  <div className="cell-title" style={{ fontSize: 13 }}>
                    {org.plan_name}
                  </div>
                  <div className="small muted">{org.plan_tier}</div>
                </div>
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
                <Tag tone={org.is_active ? 'accent-2' : 'outline'}>
                  {org.is_active ? 'Active' : 'Inactive'}
                </Tag>
              </div>
              {expanded ? <WorkspaceMembers orgId={org.id} /> : null}
            </li>
          )
        })}
      </ul>
      <ListPager
        total={pager.total}
        from={pager.from}
        to={pager.to}
        page={pager.page}
        pageCount={pager.pageCount}
        pageSize={pager.pageSize}
        onPage={pager.setPage}
        onPageSize={pager.setPageSize}
        label="Workspace pages"
      />
    </Blueprint>
  )
}

function WorkspaceMembers({ orgId }: { orgId: string }) {
  const users = useResource(
    (signal) => api.portalOrganizationUsers(orgId, signal),
    [orgId],
    `portal:users:${orgId}`,
  )
  const pager = usePagedList(users.data ?? [], {
    storageKey: 'portal-users',
    resetKey: orgId,
  })

  if (users.loading && !users.data) {
    return (
      <div className="portal-tree-children">
        <Loading label="Loading members…" />
      </div>
    )
  }
  if (users.error) {
    return (
      <div className="portal-tree-children">
        <ErrorState message={users.error} onRetry={users.reload} />
      </div>
    )
  }
  const members: PortalUser[] = users.data ?? []
  if (members.length === 0) {
    return <p className="portal-tree-empty muted">No members in this workspace.</p>
  }

  return (
    <ul className="portal-tree-children" role="group">
      {pager.slice.map((member) => (
        <li key={member.id} className="portal-tree-seat" role="treeitem" aria-level={2}>
          <div className="portal-tree-seat-copy">
            <div className="portal-tree-title">{member.name}</div>
            <div className="small muted">{member.email}</div>
          </div>
          <div className="portal-tree-seat-role">
            <span>{member.role_label}</span>
            {member.is_owner ? <Tag tone="outline">Owner</Tag> : null}
          </div>
          <Tag tone={member.is_active ? 'accent-2' : 'outline'}>
            {member.is_active ? 'Active' : 'Inactive'}
          </Tag>
          <div className="portal-tree-login">
            <div className="small muted">{formatLogin(member.last_login_at)}</div>
          </div>
        </li>
      ))}
      {members.length > pager.pageSize ? (
        <li className="portal-tree-pager">
          <ListPager
            total={pager.total}
            from={pager.from}
            to={pager.to}
            page={pager.page}
            pageCount={pager.pageCount}
            pageSize={pager.pageSize}
            onPage={pager.setPage}
            onPageSize={pager.setPageSize}
            label="Member pages"
          />
        </li>
      ) : null}
    </ul>
  )
}
