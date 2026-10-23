/**
 * Commercial billing: plan catalog, account, usage and entitlements.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  EmptyState,
  ErrorState,
  Loading,
  PageHeader,
  SectionHeading,
  StatTile,
  Tag,
  statusTone,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

export function BillingPage() {
  const { canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const writable = canWrite('admin')

  const catalog = useResource((s) => api.billingCatalog(s), [], 'billing-catalog')
  const account = useResource((s) => api.billingAccount(s), [], 'billing-account')
  const usage = useResource((s) => api.billingUsage(s), [], 'billing-usage')
  const entitlements = useResource((s) => api.billingEntitlements(s), [], 'billing-entitlements')
  const [busy, setBusy] = useState(false)

  const loading =
    (catalog.loading && !catalog.data) ||
    (account.loading && !account.data) ||
    (usage.loading && !usage.data)
  if (loading) return <Loading label="Loading billing…" />
  const err = catalog.error || account.error || usage.error
  if (err && !catalog.data && !account.data) {
    return <ErrorState message={err} onRetry={() => void catalog.reload()} />
  }

  const row = account.data?.account
  const plans = catalog.data?.plans ?? []

  const ensure = async (planSlug: string) => {
    if (!writable) return push('View-only access for your role', 'warning')
    setBusy(true)
    try {
      if (row) await api.changeBillingPlan({ plan_slug: planSlug })
      else await api.ensureBillingAccount({ plan_slug: planSlug })
      push('Billing updated', 'success')
      await account.reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Billing"
        description="Plan, seats and usage meters for this workspace. Prices come from the billing provider."
      />

      <div className="grid-tiles">
        <StatTile kicker="Plan" value={row?.plan_slug ?? 'None'} />
        <StatTile kicker="Seats" value={row ? String(row.seats_purchased) : '—'} />
        <StatTile kicker="Status" value={row?.status ?? '—'} />
      </div>

      <SectionHeading title="Account" />
      <Blueprint className="card elev-sm">
        {!row ? (
          <EmptyState
            title="No billing account"
            description="Create a trial or paid account to unlock entitlements."
            action={
              writable ? (
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => void ensure('trial')}
                >
                  Start trial
                </button>
              ) : undefined
            }
          />
        ) : (
          <ul className="stack-sm">
            <li className="list-row">
              <div className="list-row-main">
                <div className="list-row-title">{row.plan_slug}</div>
                <div className="list-row-meta muted">
                  Provider {row.provider}
                  {row.current_period_end ? ` · renews ${row.current_period_end}` : ''}
                </div>
              </div>
              <Tag tone={statusTone(row.status)}>{row.status}</Tag>
            </li>
          </ul>
        )}
      </Blueprint>

      <SectionHeading title="Plans" spaced />
      {plans.length === 0 ? (
        <EmptyState title="No plans in catalog" description="The billing provider returned an empty list." />
      ) : (
        <div className="grid-cards">
          {plans.map((plan) => (
            <Blueprint key={plan.slug} className="card elev-sm">
              <div className="card-kicker">{plan.slug}</div>
              <div className="card-title">{String(plan.name ?? plan.slug)}</div>
              {writable ? (
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busy || row?.plan_slug === plan.slug}
                  onClick={() => void ensure(plan.slug)}
                >
                  {row?.plan_slug === plan.slug ? 'Current plan' : 'Select'}
                </button>
              ) : null}
            </Blueprint>
          ))}
        </div>
      )}

      <SectionHeading title="Usage" spaced />
      {(usage.data?.meters?.length ?? 0) === 0 ? (
        <EmptyState title="No usage meters" description="Meters appear once the workspace consumes billable resources." />
      ) : (
        <Blueprint className="card elev-sm">
          <pre className="mono-sub" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
            {JSON.stringify(usage.data?.meters, null, 2)}
          </pre>
        </Blueprint>
      )}

      <SectionHeading title="Entitlements" spaced />
      {!entitlements.data?.entitlements ||
      (Array.isArray(entitlements.data.entitlements)
        ? entitlements.data.entitlements.length === 0
        : Object.keys(entitlements.data.entitlements).length === 0) ? (
        <EmptyState title="No entitlements listed" />
      ) : (
        <Blueprint className="card elev-sm">
          <pre className="mono-sub" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
            {JSON.stringify(entitlements.data.entitlements, null, 2)}
          </pre>
        </Blueprint>
      )}
    </>
  )
}
