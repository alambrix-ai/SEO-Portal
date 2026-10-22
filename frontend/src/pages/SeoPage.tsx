/**
 * SEO & AEO workspace - the page table and the traffic-quality guard.
 *
 * The table shows what each page needs; opening one shows the rewrite the
 * agent proposed, decrypted server-side, before anyone sends it for approval.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { SeoPageDetail, SeoPageOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { ScoreHistory, VersionHistory } from '@/components/AuditHistory'
import { PageTabs } from '@/components/PageTabs'
import { useToasts } from '@/components/Toasts'
import { ListPager } from '@/components/ListPager'
import {
  Dialog,
  EmptyState,
  ErrorState,
  Loading,
  PageHeader,
  SectionHeading,
  statusTone,
  Tag,
} from '@/components/ui'
import { usePagedList } from '@/hooks/usePagedList'
import { useDebounced, useResource } from '@/hooks/useResource'

/** One line on what the comparison words mean, for a non-technical reader. */
const COMPARE_HELP =
  'Pick any two scoring runs to see the difference. New: a page scored for the first time. Fixed: a page whose topic gap went down. Worse: a page whose topic gap went up.'

export function SeoPage() {
  const { canView, canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()

  const [view, setView] = useState<'pages' | 'history'>('pages')
  const [search, setSearch] = useState('')
  const debounced = useDebounced(search)
  const { data, loading, error, reload } = useResource(
    (signal) => api.seo(debounced, signal),
    [debounced],
    `seo:${debounced}`,
  )

  const [detail, setDetail] = useState<SeoPageDetail | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const writable = canWrite('seo')
  const pagesPager = usePagedList(data?.pages ?? [], { storageKey: 'seo-pages', resetKey: debounced })
  const spamPager = usePagedList(data?.referral_spam ?? [], { storageKey: 'seo-spam' })

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
      <PageHeader
        title="SEO & AEO"
        description="How well each page on your site covers its topic and answers the questions people ask search engines and AI assistants."
      />
      <PageTabs
        value={view}
        options={[
          { value: 'pages', label: 'Pages' },
          { value: 'history', label: 'Score history' },
        ]}
        onChange={setView}
      />
      {view === 'history' ? (
        <>
          <ScoreHistory />
          {/* Scores are read-only records: the server marks them not
              restorable, so the kit shows no restore button. */}
          {canView('seo') ? (
            <VersionHistory
              kind="seo_scores"
              title="Compare these scores with an earlier run"
              description={COMPARE_HELP}
            />
          ) : null}
        </>
      ) : (
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
          {data.total} pages · kept up to date from your website automatically
        </div>
      </div>

      {data.pages.length === 0 ? (
        <EmptyState
          title="No pages synced yet"
          description="Connect your website on the Integrations screen, then run On-Page SEO Sync. Pages appear here as soon as we find them."
        />
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Page</th>
                <th title="How much of the page's topic is not yet covered. Lower is better.">
                  Topic gaps
                </th>
                <th title="Questions and answers written for search engines and AI assistants">
                  Questions answered
                </th>
                <th title="Extra details added to the page so search engines understand it">
                  Search engine details
                </th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {pagesPager.slice.map((page) => (
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
                  <td className="small">{page.schema_text || '-'}</td>
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
      {data.pages.length > 0 ? (
        <ListPager
          total={pagesPager.total}
          from={pagesPager.from}
          to={pagesPager.to}
          page={pagesPager.page}
          pageCount={pagesPager.pageCount}
          pageSize={pagesPager.pageSize}
          onPage={pagesPager.setPage}
          onPageSize={pagesPager.setPageSize}
          label="SEO page list"
        />
      ) : null}

      <SectionHeading
        title="Referral spam & traffic quality guard"
        description="Blocks fake and spam visits from other sites before they skew your traffic numbers and make your search reports less reliable."
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
              {spamPager.slice.map((event, index) => (
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
      {data.referral_spam.length > 0 ? (
        <ListPager
          total={spamPager.total}
          from={spamPager.from}
          to={spamPager.to}
          page={spamPager.page}
          pageCount={spamPager.pageCount}
          pageSize={spamPager.pageSize}
          onPage={spamPager.setPage}
          onPageSize={spamPager.setPageSize}
          label="Referral spam pages"
        />
      ) : null}

      </>
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
          <Tag tone="neutral">Topic gaps {page.gap_score}%</Tag>
          <Tag tone="neutral">{page.aeo_pairs} questions answered</Tag>
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

        {/* The knowledge-graph agent's output. The type and status are in
            plain view; the JSON-LD itself sits under "Advanced" so a marketer
            is not faced with code, while anyone who wants to check it before
            approving still can. */}
        {page.schema_patches.length > 0 ? (
          <div>
            <div className="card-kicker">
              Search engine details ({page.schema_patches.length})
            </div>
            {page.schema_patches.map((patch, index) => (
              <div className="schema-patch" key={`${patch.schema_type}-${index}`}>
                <div className="schema-patch-head">
                  <strong>{patch.schema_type}</strong>
                  <span className="qa-state">{patch.status}</span>
                </div>
                <details className="advanced-disclosure">
                  <summary>Advanced: show the code (JSON-LD)</summary>
                  <pre className="schema-json">
                    {JSON.stringify(patch.json_ld, null, 2)}
                  </pre>
                </details>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </Dialog>
  )
}
