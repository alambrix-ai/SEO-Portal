/**
 * Warehouse export jobs (egress-allowlisted destinations).
 */
import { useState } from 'react'

import { api } from '@/api/client'
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
  statusTone,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

export function WarehousePage() {
  const { canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const writable = canWrite('admin')
  const { data, loading, error, reload } = useResource(
    (signal) => api.warehouseExports(signal),
    [],
    'warehouse-exports',
  )

  const [provider, setProvider] = useState('fake')
  const [dataset, setDataset] = useState('metrics')
  const [destination, setDestination] = useState('')
  const [busy, setBusy] = useState(false)

  if (loading && !data) return <Loading label="Loading warehouse exports…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const enqueue = async () => {
    if (!writable) return push('View-only access for your role', 'warning')
    if (destination.trim().length < 3) return push('Enter a destination', 'warning')
    setBusy(true)
    try {
      await api.enqueueWarehouseExport({
        provider: provider.trim() || 'fake',
        dataset: dataset.trim() || 'metrics',
        destination: destination.trim(),
      })
      push('Export queued', 'success')
      setDestination('')
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Warehouse exports"
        description="Push metrics datasets to an egress-allowlisted destination. Sensitive jobs may require a fresh MFA step-up."
      />

      <div className="split-1-1">
        <Blueprint className="card elev-sm">
          <SectionHeading title="Recent jobs" />
          {data.jobs.length === 0 ? (
            <EmptyState title="No exports yet" description="Enqueue a job to see status and row counts here." />
          ) : (
            <ul className="stack-sm">
              {data.jobs.map((job) => (
                <li key={job.id} className="list-row">
                  <div className="list-row-main">
                    <div className="list-row-title">
                      {job.dataset} → {job.provider}
                    </div>
                    <div className="list-row-meta muted">
                      {job.row_count != null ? `${job.row_count} rows` : '—'}
                      {job.error_code ? ` · ${job.error_code}` : ''}
                    </div>
                  </div>
                  <Tag tone={statusTone(job.status)}>{job.status}</Tag>
                </li>
              ))}
            </ul>
          )}
        </Blueprint>

        <Blueprint className="card elev-sm">
          <SectionHeading title="Enqueue export" />
          {writable ? (
            <div className="stack">
              <Field label="Provider" htmlFor="wh-provider">
                <input id="wh-provider" className="input" value={provider} onChange={(e) => setProvider(e.target.value)} />
              </Field>
              <Field label="Dataset" htmlFor="wh-dataset">
                <input id="wh-dataset" className="input" value={dataset} onChange={(e) => setDataset(e.target.value)} />
              </Field>
              <Field label="Destination" htmlFor="wh-dest" required>
                <input
                  id="wh-dest"
                  className="input"
                  value={destination}
                  onChange={(e) => setDestination(e.target.value)}
                  placeholder="s3://bucket/path or https://…"
                />
              </Field>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void enqueue()}>
                Run export
              </button>
            </div>
          ) : (
            <EmptyState title="View only" description="Admin write access is required to enqueue exports." />
          )}
        </Blueprint>
      </div>
    </>
  )
}
