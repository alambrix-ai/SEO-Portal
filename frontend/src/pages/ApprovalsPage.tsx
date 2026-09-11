/**
 * Approvals queue - reviewing what agents produced under a guardrail.
 *
 * A reviewer can open an item to see the exact change before deciding, which
 * matters because approving applies that stored payload verbatim rather than
 * re-running the agent.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { ApprovalDetail, ApprovalOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { CardSection } from '@/components/CardSection'
import { PayloadView } from '@/components/PayloadView'
import { useToasts } from '@/components/Toasts'
import { Dialog, EmptyState, ErrorState, Loading, Tag } from '@/components/ui'
import { useResource } from '@/hooks/useResource'

export function ApprovalsPage() {
  const { canWrite, refresh } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const { data, loading, error, reload } = useResource(
    (signal) => api.approvals(signal),
    [],
    'approvals',
  )

  const [detail, setDetail] = useState<ApprovalDetail | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const writable = canWrite('approvals')

  const decide = async (item: ApprovalOut, approve: boolean) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusyId(item.id)
    try {
      fromResult(
        approve ? await api.approve(item.id) : await api.reject(item.id),
      )
      setDetail(null)
      await reload()
      // The sidebar badge lives on the session.
      await refresh()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  const open = async (item: ApprovalOut) => {
    try {
      setDetail(await api.approval(item.id))
    } catch (caught) {
      fromError(caught)
    }
  }

  if (loading && !data) return <Loading label="Loading the queue…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <p className="page-intro">
        Every item an agent produced under a human-in-the-loop guardrail lands here
        before it goes live. Approving applies exactly what was proposed.
      </p>

      {data.length === 0 ? (
        <EmptyState
          title="Queue is clear"
          description="Nothing is waiting on human review. Items appear here when an agent proposes a high-impact change under the hybrid guardrail, or any change under human review."
        />
      ) : (
        <CardSection
          title="Waiting on you"
          count={data.length}
          description="Approving applies exactly what the agent proposed, from the payload stored at the time - no second model call."
        >
        <div className="stack">
          {data.map((item) => (
            <div key={item.id} className="card approval-row">
              <div className="approval-body">
                <Tag tone="accent">{item.type}</Tag>
                <div className="approval-title">{item.title}</div>
                <div className="card-meta">
                  from {item.agent} · {item.meta}
                </div>
              </div>
              <div className="row nowrap">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void open(item)}
                >
                  Review
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={!writable || busyId === item.id}
                  onClick={() => void decide(item, false)}
                >
                  Reject
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!writable || busyId === item.id}
                  onClick={() => void decide(item, true)}
                >
                  {busyId === item.id ? 'Applying…' : 'Approve'}
                </button>
              </div>
            </div>
          ))}
        </div>
        </CardSection>
      )}

      {detail ? (
        <Dialog
          title={detail.type}
          width={720}
          onClose={() => setDetail(null)}
          actions={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={!writable || busyId === detail.id}
                onClick={() => void decide(detail, false)}
              >
                Reject
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!writable || busyId === detail.id}
                onClick={() => void decide(detail, true)}
              >
                Approve
              </button>
            </>
          }
        >
          <div className="stack-sm">
            <div style={{ fontSize: 14 }}>{detail.title}</div>
            <div className="card-meta">
              from {detail.agent} · {detail.meta}
            </div>
            <div>
              <div className="card-kicker">The change this applies</div>
              {/* Presented rather than printed. This was a <pre> of
                  JSON.stringify, which asked somebody deciding whether copy
                  goes on their own website to read a braced object with
                  database ids in it to find out what the copy was. */}
              <PayloadView payload={detail.payload} />
            </div>
          </div>
        </Dialog>
      ) : null}
    </>
  )
}
