/**
 * Public platform status — no tenant data, no auth required.
 */
import { api } from '@/api/client'
import { BrandLogo } from '@/components/BrandLogo'
import {
  Blueprint,
  EmptyState,
  ErrorState,
  Loading,
  Tag,
  statusTone,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { asList } from '@/lib/safeData'

export function StatusPage() {
  const { data, loading, error, reload } = useResource(
    (signal) => api.publicStatus(signal),
    [],
    'public-status',
  )

  if (loading && !data) {
    return (
      <div className="auth-screen" data-testid="status-page">
        <Loading label="Checking platform status…" />
      </div>
    )
  }
  if (error && !data) {
    return (
      <div className="auth-screen" data-testid="status-page">
        <div style={{ maxWidth: 480, width: '100%', margin: '0 auto', padding: 24 }}>
          <ErrorState message={error} onRetry={reload} />
        </div>
      </div>
    )
  }

  const status = data?.status ? String(data.status) : 'unknown'
  const checkedAt = data?.checked_at ? String(data.checked_at) : '—'
  const components = asList<{ name: string; status: string }>(data?.components)

  return (
    <div className="auth-screen" data-testid="status-page">
      <div className="stack" style={{ maxWidth: 560, width: '100%', margin: '0 auto', padding: 24 }}>
        <BrandLogo height={28} />
        <div>
          <h1 className="page-header-title" style={{ margin: '12px 0 6px' }}>
            Platform status
          </h1>
          <p className="muted">
            Live health of Synaprank services. No workspace data is shown here.
          </p>
        </div>

        <Blueprint className="card elev-sm">
          <div className="row-between">
            <div className="card-title">Overall</div>
            <Tag tone={statusTone(status === 'operational' ? 'ok' : status)}>{status}</Tag>
          </div>
          <div className="card-meta muted">Checked {checkedAt}</div>
        </Blueprint>

        {components.length === 0 ? (
          <EmptyState title="No components reported" />
        ) : (
          <Blueprint className="card elev-sm">
            <ul className="stack-sm">
              {components.map((component) => (
                <li key={component.name} className="list-row">
                  <div className="list-row-main">
                    <div className="list-row-title">{component.name}</div>
                  </div>
                  <div className="list-row-side">
                    <Tag
                      tone={statusTone(
                        component.status === 'operational' ? 'ok' : String(component.status ?? ''),
                      )}
                    >
                      {component.status}
                    </Tag>
                  </div>
                </li>
              ))}
            </ul>
          </Blueprint>
        )}
      </div>
    </div>
  )
}
