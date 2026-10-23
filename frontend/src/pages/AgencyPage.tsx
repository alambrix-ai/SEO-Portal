/**
 * Agency tier and white-label branding.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  EmptyState,
  Field,
  PageHeader,
  SectionHeading,
} from '@/components/ui'

export function AgencyPage() {
  const { canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const writable = canWrite('admin')

  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [agencyId, setAgencyId] = useState('')
  const [domain, setDomain] = useState('')
  const [fromName, setFromName] = useState('')
  const [busy, setBusy] = useState(false)

  const create = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    if (!name.trim() || !slug.trim()) return push('Name and slug are required', 'warning')
    setBusy(true)
    try {
      const out = await api.createAgency({ name: name.trim(), slug: slug.trim() })
      setAgencyId(out.id)
      push(`Agency ${out.name} created`, 'success')
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const saveWhiteLabel = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    if (!agencyId.trim()) return push('Create or enter an agency id first', 'warning')
    setBusy(true)
    try {
      await api.saveAgencyWhiteLabel(agencyId.trim(), {
        custom_domain: domain.trim(),
        email_from_name: fromName.trim(),
        enabled: true,
      })
      push('White-label settings saved', 'success')
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Agency"
        description="Parent agency accounts, client links and white-label chrome for resellers."
      />

      <div className="split-1-1">
        <Blueprint className="card elev-sm">
          <SectionHeading
            title="Create agency"
            description="Creates a parent agency record owned by your signed-in address."
          />
          {writable ? (
            <div className="stack">
              <Field label="Name" htmlFor="agency-name" required>
                <input id="agency-name" className="input" value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field label="Slug" htmlFor="agency-slug" required>
                <input id="agency-slug" className="input" value={slug} onChange={(e) => setSlug(e.target.value)} />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void create()}>
                Create agency
              </button>
              {agencyId ? (
                <p className="muted">
                  Agency id: <code className="mono-sub">{agencyId}</code>
                </p>
              ) : null}
            </div>
          ) : (
            <EmptyState title="View only" description="Ask an admin to create an agency." />
          )}
        </Blueprint>

        <Blueprint className="card elev-sm">
          <SectionHeading
            title="White-label"
            description="Custom domain and email from-name for client-facing surfaces."
          />
          {writable ? (
            <div className="stack">
              <Field label="Agency id" htmlFor="wl-agency">
                <input id="wl-agency" className="input" value={agencyId} onChange={(e) => setAgencyId(e.target.value)} />
              </Field>
              <Field label="Custom domain" htmlFor="wl-domain">
                <input id="wl-domain" className="input" value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="console.partner.com" />
              </Field>
              <Field label="Email from name" htmlFor="wl-from">
                <input id="wl-from" className="input" value={fromName} onChange={(e) => setFromName(e.target.value)} />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void saveWhiteLabel()}>
                Save white-label
              </button>
            </div>
          ) : (
            <EmptyState title="View only" description="White-label settings require admin write access." />
          )}
        </Blueprint>
      </div>
    </>
  )
}
