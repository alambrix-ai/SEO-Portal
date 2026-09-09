/**
 * SEO & AEO workspace — the page table and the traffic-quality guard.
 *
 * The table shows what each page needs; opening one shows the rewrite the
 * agent proposed, decrypted server-side, before anyone sends it for approval.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { SeoPageDetail, SeoPageOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Dialog,
  EmptyState,
  ErrorState,
  Loading,
  SectionHeading,
  Tag,
  statusTone,
} from '@/components/ui'
import { useDebounced, useResource } from '@/hooks/useResource'

export function SeoPage() {
  const { canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()

  const [search, setSearch] = useState('')
  const debounced = useDebounced(search)
  const { data, loading, error, reload } = useResource(
    () => api.seo(debounced),
    [debounced],
  )

  const [detail, setDetail] = useState<SeoPageDetail | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const writable = canWrite('seo')

  const requestApproval = async (page: SeoPageOut) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusyId(page.id)
    try {
      fromResult(await api.requestPageApproval(page.id))
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  const open = async (page: SeoPageOut) => {
    try {
      setDetail(await api.seoPage(page.id))
    } catch (caught) {
      fromError(caught)
    }
  }

  if (loading && !data) return <Loading label="Loading pages…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <div className="row-between" style={{ marginBottom: 14 }}>
        <input
          className="input w-280"
          placeholder="Search pages…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          aria-label="Search pages"
        />
        <div className="small muted">
          {data.total} pages · webhook-synced to CMS live
        </div>
      </div>

      {data.pages.length === 0 ? (
        <EmptyState
          title="No pages synced yet"
          description="Connect a CMS on the Connectors screen, then run On-Page SEO Sync. Pages appear here as soon as the crawler finds them."
        />
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Page</th>
                <th>Semantic Gap</th>
                <th>AEO Q&amp;A Pairs</th>
                <th>Schema Injected</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.pages.map((page) => (
                <tr key={page.id}>
                  <td>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      style={{ padding: 0, textAlign: 'left' }}
                      onClick={() => void open(page)}
                    >
                      <span className="cell-title">{page.title}</span>
                    </button>
                    <div className="mono-sub">{page.url}</div>
                  </td>
                  <td>{page.gap_score}%</td>
                  <td>{page.aeo_pairs}</td>
                  <td className="small">{page.schema_text || '—'}</td>
                  <td>
                    <Tag tone={statusTone(page.status)}>{page.status_label}</Tag>
                  </td>
                  <td className="table-actions">
                    {page.show_approve ? (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={busyId === page.id}
                        onClick={() => void requestApproval(page)}
                      >
                        {busyId === page.id ? 'Sending…' : 'Approve rewrite'}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <SectionHeading
        title="Referral spam & traffic quality guard"
        description="Blocks ghost, bot, and adult/spam referrers before they inflate bounce rate and pollute SEO signal quality."
        spaced
      />

      {data.referral_spam.length === 0 ? (
        <EmptyState
          title="No blocked referrers yet"
          description="Connect an analytics property and the guard will start assessing referral traffic on its next run."
        />
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Referrer domain</th>
                <th>Category</th>
                <th>Sessions</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {data.referral_spam.map((event, index) => (
                <tr key={`${event.referrer}-${index}`}>
                  <td>{event.time}</td>
                  <td>{event.referrer}</td>
                  <td>{event.category}</td>
                  <td>{event.sessions_affected}</td>
                  <td>
                    <Tag tone="accent">{event.action}</Tag>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detail ? <PageDetailDialog page={detail} onClose={() => setDetail(null)} /> : null}
    </>
  )
}

function PageDetailDialog({
  page,
  onClose,
}: {
  page: SeoPageDetail
  onClose: () => void
}) {
  return (
    <Dialog
      title={page.title}
      width={720}
      onClose={onClose}
      actions={
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Close
        </button>
      }
    >
      <div className="stack-sm">
        <div className="mono-sub">{page.url}</div>
        <div className="row">
          <Tag tone={statusTone(page.status)}>{page.status_label}</Tag>
          <Tag tone="neutral">Gap {page.gap_score}%</Tag>
          <Tag tone="neutral">{page.aeo_pairs} Q&amp;A pairs</Tag>
          {page.schema_text ? <Tag tone="outline">{page.schema_text}</Tag> : null}
        </div>

        {page.missing_topics.length > 0 ? (
          <div>
            <div className="card-kicker">Missing topics</div>
            <ul style={{ margin: '4px 0 0', paddingLeft: 18, fontSize: 13 }}>
              {page.missing_topics.map((topic) => (
                <li key={topic}>{topic}</li>
              ))}
            </ul>
          </div>
        ) : null}

        {page.rewrite_rationale ? (
          <div>
            <div className="card-kicker">Why a rewrite was proposed</div>
            <p className="small" style={{ margin: '4px 0 0' }}>
              {page.rewrite_rationale}
            </p>
          </div>
        ) : null}

        {page.proposed_body ? (
          <div>
            <div className="card-kicker">Proposed copy</div>
            <pre
              className="small"
              style={{
                whiteSpace: 'pre-wrap',
                maxHeight: 260,
                overflow: 'auto',
                padding: 10,
                margin: '4px 0 0',
              }}
            >
              {page.proposed_body}
            </pre>
          </div>
        ) : (
          <div className="small muted">
            No rewrite is currently proposed for this page.
          </div>
        )}

        {/* The AEO injector's output. It wrote these to the database and no
            screen read them, so an agent that had produced two dozen pairs
            for this workspace looked as though it had done nothing. */}
        {page.qa_pairs.length > 0 ? (
          <div>
            <div className="card-kicker">
              Questions &amp; answers ({page.qa_pairs.length})
            </div>
            <ul className="qa-list">
              {page.qa_pairs.map((pair, index) => (
                <li className="qa-item" key={`${pair.question}-${index}`}>
                  <div className="qa-question">{pair.question}</div>
                  <p className="qa-answer">{pair.answer}</p>
                  <span className={`qa-state${pair.injected ? ' is-live' : ''}`}>
                    {pair.status}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* The knowledge-graph agent's output, with the JSON-LD readable —
            approving structured data for your own site without seeing it is
            not something anyone can do responsibly. */}
        {page.schema_patches.length > 0 ? (
          <div>
            <div className="card-kicker">
              Structured data ({page.schema_patches.length})
            </div>
            {page.schema_patches.map((patch, index) => (
              <div className="schema-patch" key={`${patch.schema_type}-${index}`}>
                <div className="schema-patch-head">
                  <strong>{patch.schema_type}</strong>
                  <span className="qa-state">{patch.status}</span>
                </div>
                <pre className="schema-json">
                  {JSON.stringify(patch.json_ld, null, 2)}
                </pre>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </Dialog>
  )
}
