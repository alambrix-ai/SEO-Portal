/**
 * Enterprise identity: SSO, SCIM, custom roles, IP allowlists, approval scopes.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { CustomRoleOut, EnterpriseSsoOut, IpAllowlistOut } from '@/api/types'
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

export function EnterprisePage() {
  const { canWrite, session } = useAuth()
  const { push, fromError } = useToasts()
  const writable = canWrite('admin')

  const sso = useResource((signal) => api.enterpriseSso(signal), [], 'enterprise-sso')
  const roles = useResource((signal) => api.enterpriseRoles(signal), [], 'enterprise-roles')
  const ip = useResource((signal) => api.enterpriseIpAllowlist(signal), [], 'enterprise-ip')

  const [cidr, setCidr] = useState('')
  const [ipLabel, setIpLabel] = useState('')
  const [roleSlug, setRoleSlug] = useState('')
  const [roleName, setRoleName] = useState('')
  const [scimLabel, setScimLabel] = useState('SCIM token')
  const [mintedToken, setMintedToken] = useState('')
  const [scopeUserId, setScopeUserId] = useState('')
  const [scopeBrands, setScopeBrands] = useState('')
  const [busy, setBusy] = useState(false)

  if ((sso.loading && !sso.data) || (roles.loading && !roles.data) || (ip.loading && !ip.data)) {
    return <Loading label="Loading enterprise settings…" />
  }

  if (sso.forbidden || roles.forbidden || ip.forbidden) {
    return <ErrorState message="You do not have permission to view enterprise settings." />
  }

  const firstError = sso.error || roles.error || ip.error
  if (firstError && !sso.data && !roles.data && !ip.data) {
    return (
      <ErrorState
        message={firstError}
        onRetry={() => {
          void sso.reload()
          void roles.reload()
          void ip.reload()
        }}
      />
    )
  }

  const connections = asList<EnterpriseSsoOut>(sso.data?.connections)
  const customRoles = asList<CustomRoleOut>(roles.data?.roles)
  const ipData = ip.data as IpAllowlistOut | null
  const entries = asList<IpAllowlistOut['entries'][number]>(ipData?.entries)

  const addIp = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    if (!cidr.trim()) return push('Enter a CIDR', 'warning')
    setBusy(true)
    try {
      await api.addEnterpriseIp({ cidr: cidr.trim(), label: ipLabel.trim() })
      setCidr('')
      setIpLabel('')
      push('IP entry added', 'success')
      await ip.reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const toggleIp = async (enabled: boolean) => {
    if (!writable) return
    setBusy(true)
    try {
      await api.toggleEnterpriseIp(enabled)
      push(enabled ? 'IP allowlist enabled' : 'IP allowlist disabled', 'success')
      await ip.reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const createRole = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    if (!roleSlug.trim() || !roleName.trim()) return push('Slug and name are required', 'warning')
    setBusy(true)
    try {
      await api.createEnterpriseRole({ slug: roleSlug.trim(), name: roleName.trim() })
      setRoleSlug('')
      setRoleName('')
      push('Custom role created', 'success')
      await roles.reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const mintScim = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    setBusy(true)
    try {
      const out = await api.mintScimToken(scimLabel.trim() || 'SCIM token')
      setMintedToken(out.token)
      push('SCIM token minted — copy it now', 'success')
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const saveScope = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    if (!scopeUserId.trim()) return push('Enter a user id', 'warning')
    setBusy(true)
    try {
      await api.setApprovalScope({
        user_id: scopeUserId.trim(),
        brands: scopeBrands.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean),
      })
      push('Approval scope saved', 'success')
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Enterprise"
        description="SSO connections, SCIM provisioning tokens, custom roles, IP allowlists and scoped approvals."
      />

      {firstError && (sso.data || roles.data || ip.data) ? (
        <div className="banner banner-warning" role="status" style={{ marginBottom: 16 }}>
          {firstError}
        </div>
      ) : null}

      <SectionHeading title="SSO connections" spaced={false} />
      <Blueprint className="card elev-sm">
        {connections.length === 0 ? (
          <EmptyState
            title="No SSO connections"
            description="Add an IdP connection via the API or ask support to wire your organisation's SAML/OIDC provider. SSO login stays off until SSO_LOGIN_ENABLED is set on the API."
          />
        ) : (
          <ul className="stack-sm">
            {connections.map((row) => (
              <li key={row.id} className="list-row">
                <div className="list-row-main">
                  <div className="list-row-title">{row.name}</div>
                  <div className="list-row-meta muted">
                    {row.protocol} · {row.slug}
                    {row.issuer ? ` · ${row.issuer}` : ''}
                  </div>
                </div>
                <Tag tone={row.enabled ? 'accent' : 'neutral'}>
                  {row.enabled ? 'Enabled' : 'Disabled'}
                </Tag>
              </li>
            ))}
          </ul>
        )}
      </Blueprint>

      <div className="split-1-1">
        <Blueprint className="card elev-sm">
          <SectionHeading title="Custom roles" />
          {customRoles.length === 0 ? (
            <EmptyState title="No custom roles" description="Built-in roles still apply." />
          ) : (
            <ul className="stack-sm">
              {customRoles.map((role) => (
                <li key={role.id} className="list-row">
                  <div className="list-row-main">
                    <div className="list-row-title">{role.name}</div>
                    <div className="list-row-meta muted">{role.slug}</div>
                  </div>
                  <Tag tone={role.is_active ? 'accent-2' : 'neutral'}>
                    {role.is_active ? 'Active' : 'Inactive'}
                  </Tag>
                </li>
              ))}
            </ul>
          )}
          {writable ? (
            <div className="stack">
              <Field label="Slug" htmlFor="role-slug">
                <input id="role-slug" className="input" value={roleSlug} onChange={(e) => setRoleSlug(e.target.value)} />
              </Field>
              <Field label="Name" htmlFor="role-name">
                <input id="role-name" className="input" value={roleName} onChange={(e) => setRoleName(e.target.value)} />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void createRole()}>
                Create role
              </button>
            </div>
          ) : null}
        </Blueprint>

        <Blueprint className="card elev-sm">
          <SectionHeading
            title="IP allowlist"
            description={ipData?.enabled ? 'Only listed CIDRs can reach the console.' : 'Allowlist is off.'}
            action={
              writable ? (
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={busy}
                  onClick={() => void toggleIp(!ipData?.enabled)}
                >
                  {ipData?.enabled ? 'Disable' : 'Enable'}
                </button>
              ) : null
            }
          />
          {entries.length === 0 ? (
            <EmptyState title="No entries" description="Add a CIDR before enabling the allowlist." />
          ) : (
            <ul className="stack-sm">
              {entries.map((entry) => (
                <li key={entry.id} className="list-row">
                  <div className="list-row-main">
                    <div className="list-row-title mono-sub">{entry.cidr}</div>
                    <div className="list-row-meta muted">{entry.label || '—'}</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {writable ? (
            <div className="stack">
              <Field label="CIDR" htmlFor="ip-cidr">
                <input id="ip-cidr" className="input" value={cidr} onChange={(e) => setCidr(e.target.value)} placeholder="203.0.113.0/24" />
              </Field>
              <Field label="Label" htmlFor="ip-label">
                <input id="ip-label" className="input" value={ipLabel} onChange={(e) => setIpLabel(e.target.value)} />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void addIp()}>
                Add CIDR
              </button>
            </div>
          ) : null}
        </Blueprint>
      </div>

      <div className="split-1-1">
        <Blueprint className="card elev-sm">
          <SectionHeading title="SCIM tokens" description="Tokens are shown once when minted." />
          {writable ? (
            <div className="stack">
              <Field label="Label" htmlFor="scim-label">
                <input id="scim-label" className="input" value={scimLabel} onChange={(e) => setScimLabel(e.target.value)} />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void mintScim()}>
                Mint SCIM token
              </button>
              {mintedToken ? (
                <Field label="Copy this token now" htmlFor="scim-token">
                  <textarea id="scim-token" className="input mono-sub" readOnly rows={3} value={mintedToken} />
                </Field>
              ) : null}
            </div>
          ) : (
            <p className="muted">View-only access for your role.</p>
          )}
        </Blueprint>

        <Blueprint className="card elev-sm">
          <SectionHeading
            title="Scoped approvals"
            description={`Limit which brands a reviewer can approve. Signed in as ${session?.user.email ?? 'you'}.`}
          />
          {writable ? (
            <div className="stack">
              <Field label="User id" htmlFor="scope-user">
                <input id="scope-user" className="input" value={scopeUserId} onChange={(e) => setScopeUserId(e.target.value)} />
              </Field>
              <Field label="Brands (one per line)" htmlFor="scope-brands">
                <textarea id="scope-brands" className="input" rows={4} value={scopeBrands} onChange={(e) => setScopeBrands(e.target.value)} />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void saveScope()}>
                Save scope
              </button>
            </div>
          ) : (
            <p className="muted">View-only access for your role.</p>
          )}
        </Blueprint>
      </div>
    </>
  )
}
