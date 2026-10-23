/**
 * Topic clusters and content briefs from the Topic Cluster Planner agent.
 */
import { api } from '@/api/client'
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

function gapPct(score: number): string {
  return `${Math.round(score * 100)}%`
}

export function TopicsPage() {
  const { data, loading, error, reload } = useResource(
    (signal) => api.topics(signal),
    [],
    'topics',
  )

  if (loading && !data) return <Loading label="Loading topic clusters…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <PageHeader
        title="Topic clusters"
        description="Cluster map and content briefs produced by the Topic Cluster Planner. Gaps show where coverage is thinnest."
      />

      <div className="grid-tiles">
        <StatTile kicker="Clusters" value={String(data.clusters.length)} />
        <StatTile kicker="Briefs" value={String(data.briefs.length)} />
        <StatTile
          kicker="Highest gap"
          value={
            data.clusters.length
              ? gapPct(Math.max(...data.clusters.map((c) => c.gap_score)))
              : '—'
          }
        />
      </div>

      <SectionHeading
        title="Clusters"
        description="Ordered by gap score. Open the Topic Cluster Planner agent to refresh the map."
        spaced
      />
      {data.clusters.length === 0 ? (
        <EmptyState
          title="No clusters yet"
          description="Configure and run the Topic Cluster Planner agent. Clusters and briefs will appear here."
        />
      ) : (
        <Blueprint className="card elev-sm">
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Cluster</th>
                  <th>Seeds</th>
                  <th>Pages</th>
                  <th>Gap</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.clusters.map((cluster) => (
                  <tr key={cluster.id}>
                    <td>
                      <strong>{cluster.name}</strong>
                    </td>
                    <td className="muted">{cluster.seed_terms.join(', ') || '—'}</td>
                    <td>{cluster.page_count}</td>
                    <td>{gapPct(cluster.gap_score)}</td>
                    <td>
                      <Tag tone={statusTone(cluster.status)}>{cluster.status}</Tag>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Blueprint>
      )}

      <SectionHeading title="Content briefs" spaced />
      {data.briefs.length === 0 ? (
        <EmptyState
          title="No briefs yet"
          description="Briefs appear after the planner drafts them for a cluster."
        />
      ) : (
        <div className="stack">
          {data.briefs.map((brief) => (
            <Blueprint key={brief.id} className="card elev-sm">
              <div className="row-between">
                <div>
                  <div className="card-title">{brief.title}</div>
                  <div className="card-meta muted">
                    {brief.target_keywords.join(', ') || 'No keywords'}
                  </div>
                </div>
                <Tag tone={statusTone(brief.status)}>{brief.status}</Tag>
              </div>
              {brief.body ? <p className="card-body">{brief.body}</p> : null}
            </Blueprint>
          ))}
        </div>
      )}
    </>
  )
}
