/**
 * Approvals queue - reviewing what agents produced while "Review before
 * publishing" asks for a person.
 *
 * A reviewer can open an item to see the exact change before deciding, which
 * matters because approving applies that stored payload verbatim rather than
 * re-running the agent.
 */
import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { brandKitApi } from '@/api/brandKit'
import { api, safetyApi } from '@/api/client'
import type { ApprovalDetail, ApprovalHistoryItem, ApprovalHistoryStatus, ApprovalOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { CardSection } from '@/components/CardSection'
import { PayloadView } from '@/components/PayloadView'
import { PracticeBadge } from '@/components/SafetyBits'
import { useToasts } from '@/components/Toasts'
import { Dialog, EmptyState, ErrorState, Field, Loading, PageHeader, Segmented, Tag } from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { APPROVAL_TYPE_LABEL, approvalTypeLabel } from '@/lib/plain'
import { when } from '@/lib/time'

/** Matches the server's limit on a decision note (DecisionRequest.note). */
const REASON_MAX = 1000

/** Why a change waits for a person (unverified connector, a workspace limit). */
function WhyLines({ lines }: { lines?: string[] }) {
  if (!lines?.length) return null
  return (
    <ul className="practice-why">
      {lines.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </ul>
  )
}

function wouldText(item: ApprovalOut): string {
  return item.would === 'apply'
    ? 'Would have gone ahead automatically.'
    : 'Would have waited for your approval.'
}

type View = 'queue' | 'practice' | 'history'

function viewOf(raw: string | null): View {
  return raw === 'practice' || raw === 'history' ? raw : 'queue'
}

export function ApprovalsPage() {
  const [params, setParams] = useSearchParams()
  const view = viewOf(params.get('view'))
  const agentFilter = params.get('agent') ?? ''
  const onView = (next: View) => setParams(next === 'queue' ? {} : { view: next })
  if (view === 'practice') return <PracticeView agent={agentFilter} onView={onView} />
  if (view === 'history') return <HistoryView onView={onView} />
  return <QueueView onView={onView} />
}

function ViewSwitch({ value, onView }: { value: View; onView: (view: View) => void }) {
  return (
    <div style={{ marginBottom: 'var(--space-4)' }}>
      <Segmented
        name="approvals-view"
        value={value}
        options={[
          { value: 'queue', label: 'Waiting on you' },
          { value: 'practice', label: 'Practice' },
          { value: 'history', label: 'History' },
        ]}
        onChange={onView}
      />
    </div>
  )
}

/** What spending agents in practice mode would have done. Nothing here was sent. */
function PracticeView({ agent, onView }: { agent: string; onView: (view: View) => void }) {
  const { fromError } = useToasts()
  const { data, loading, error, reload } = useResource(
    (signal) => safetyApi.practised(agent, signal),
    [agent],
    `approvals:practice:${agent}`,
  )
  const [detail, setDetail] = useState<ApprovalDetail | null>(null)
  const open = async (item: ApprovalOut) => {
    try {
      setDetail(await api.approval(item.id))
    } catch (caught) {
      fromError(caught)
    }
  }

  return (
    <>
      <PageHeader
        title="Approvals"
        description="Practice: what spending agents in practice mode planned. Nothing here was sent to your ad accounts."
      />
      <ViewSwitch value="practice" onView={onView} />
      {loading && !data ? (
        <Loading label="Loading practice runs…" />
      ) : error && !data ? (
        <ErrorState message={error} onRetry={reload} />
      ) : !data || data.length === 0 ? (
        <EmptyState
          title="Nothing practised yet"
          description="When a spending agent runs in practice mode, every change it plans appears here with its full details, marked Practice. Nothing is sent."
        />
      ) : (
        <CardSection
          title="Practised — not sent"
          count={data.length}
          description="Each change an agent worked out in the last 30 days, and what would have happened to it. The same change planned again within a day is listed once."
        >
          <div className="stack">
            {data.map((item) => (
              <div key={item.id} className="card approval-row">
                <div className="approval-body">
                  <div className="row" style={{ gap: 6 }}>
                    <PracticeBadge />
                    <Tag tone="accent">{approvalTypeLabel(item.type)}</Tag>
                  </div>
                  <div className="approval-title">{item.title}</div>
                  <div className="card-meta">
                    from {item.agent} · {item.meta} · {wouldText(item)}
                  </div>
                  <WhyLines lines={item.why} />
                </div>
                <div className="row nowrap">
                  <button type="button" className="btn btn-ghost" onClick={() => void open(item)}>
                    See the plan
                  </button>
                </div>
              </div>
            ))}
          </div>
        </CardSection>
      )}

      {detail ? (
        <Dialog
          title={`${approvalTypeLabel(detail.type)} — practised, not sent`}
          width={720}
          onClose={() => setDetail(null)}
          actions={
            <button type="button" className="btn btn-secondary" onClick={() => setDetail(null)}>
              Close
            </button>
          }
        >
          <div className="stack-sm">
            <div className="row" style={{ gap: 6 }}>
              <PracticeBadge />
              <span style={{ fontSize: 14 }}>{detail.title}</span>
            </div>
            <div className="card-meta">
              from {detail.agent} · {detail.meta} · {wouldText(detail)}
            </div>
            <WhyLines lines={detail.why} />
            <div>
              <div className="card-kicker">The change it planned</div>
              <PayloadView payload={detail.payload} />
            </div>
          </div>
        </Dialog>
      ) : null}
    </>
  )
}

/** Statuses an item can leave the queue with, in plain words. */
const STATUS_LABEL: Record<string, string> = {
  superseded: 'Replaced by a revised version',
  approved: 'Approved',
  rejected: 'Rejected',
  expired: 'Expired',
}

/** History page size; the server allows at most 100. */
const HISTORY_PAGE = 50

const HISTORY_FILTERS: { value: '' | ApprovalHistoryStatus; label: string }[] = [
  { value: '', label: 'Everything' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'superseded', label: 'Replaced' },
  { value: 'expired', label: 'Expired' },
]

/** The stored approval types people can narrow the history to. */
const HISTORY_TYPES = Object.keys(APPROVAL_TYPE_LABEL).filter((type) => !/^[a-z0-9_]+$/.test(type))

/** Where a superseded item's revised version is now, in plain words. */
function replacedByText(item: ApprovalHistoryItem): string {
  if (item.status !== 'superseded') return ''
  if (!item.replaced_by_id) return 'The revised version is still being made.'
  switch (item.replaced_by_status) {
    case 'pending':
      return 'The revised version is waiting for you.'
    case 'approved':
      return 'The revised version was approved.'
    case 'rejected':
      return 'The revised version was rejected.'
    case 'superseded':
      return 'The revised version was sent back for more changes.'
    case 'expired':
      return 'The revised version expired without a decision.'
    default:
      return 'A revised version was made.'
  }
}

function historyTone(status: string): 'danger' | 'accent-2' | 'neutral' {
  if (status === 'rejected') return 'danger'
  if (status === 'approved') return 'accent-2'
  return 'neutral'
}

/** Everything that has left the queue: who decided, when, and why. */
function HistoryView({ onView }: { onView: (view: View) => void }) {
  const { fromError } = useToasts()
  const [status, setStatus] = useState<'' | ApprovalHistoryStatus>('')
  const [type, setType] = useState('')
  const first = useResource(
    (signal) =>
      api.listApprovalHistory({ status: status || undefined, type: type || undefined, limit: HISTORY_PAGE }, signal),
    [status, type],
  )
  /** Pages fetched with "Show more", for the current filters. */
  const [more, setMore] = useState<{ items: ApprovalHistoryItem[]; cursor: string | null } | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)

  useEffect(() => setMore(null), [status, type])

  const items = [...(first.data?.items ?? []), ...(more?.items ?? [])]
  const cursor = more ? more.cursor : (first.data?.next_cursor ?? null)

  const loadMore = async () => {
    if (!cursor || loadingMore) return
    setLoadingMore(true)
    try {
      const page = await api.listApprovalHistory({
        status: status || undefined,
        type: type || undefined,
        cursor,
        limit: HISTORY_PAGE,
      })
      setMore((current) => ({ items: [...(current?.items ?? []), ...page.items], cursor: page.next_cursor }))
    } catch (caught) {
      fromError(caught)
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Approvals"
        description="History: every change that has left your queue, who decided, and when."
      />
      <ViewSwitch value="history" onView={onView} />
      <div className="row" style={{ gap: 'var(--space-3)', marginBottom: 'var(--space-4)', flexWrap: 'wrap' }}>
        <Segmented name="approvals-history-status" value={status} options={HISTORY_FILTERS} onChange={setStatus} />
        <label className="row" style={{ gap: 6 }}>
          <span className="card-meta">Kind</span>
          <select
            className="input"
            aria-label="Kind of change"
            value={type}
            onChange={(event) => setType(event.target.value)}
          >
            <option value="">All kinds</option>
            {HISTORY_TYPES.map((value) => (
              <option key={value} value={value}>
                {approvalTypeLabel(value)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {first.loading && !first.data ? (
        <Loading label="Loading the history…" />
      ) : first.error && !first.data ? (
        <ErrorState message={first.error} onRetry={first.reload} />
      ) : items.length === 0 ? (
        <EmptyState
          title="Nothing here yet"
          description={
            status || type
              ? 'Nothing matches these filters. Try Everything, or All kinds.'
              : 'Changes you approve, reject or send back for changes are listed here.'
          }
        />
      ) : (
        <CardSection
          title="Decided"
          count={items.length}
          description="Newest first. A change sent back for changes is replaced by a revised version, which comes back to your queue."
        >
          <div className="stack">
            {items.map((item) => (
              <div key={item.id} className="card approval-row">
                <div className="approval-body">
                  <div className="row" style={{ gap: 6 }}>
                    <Tag tone="outline">{approvalTypeLabel(item.type)}</Tag>
                    <Tag tone={historyTone(item.status)}>
                      {item.status_label || STATUS_LABEL[item.status] || item.status}
                    </Tag>
                    {item.revision > 0 ? <Tag tone="neutral">Revision {item.revision}</Tag> : null}
                  </div>
                  <div className="approval-title">{item.title}</div>
                  <div className="card-meta">
                    from {item.agent}
                    {item.decided_by
                      ? ` · ${item.status === 'expired' ? 'closed' : 'decided'} by ${item.decided_by}`
                      : ''}
                    {item.decided_at ? ` · ${when(item.decided_at)}` : ''}
                  </div>
                  {item.note ? <div className="card-meta">Note: {item.note}</div> : null}
                  {item.status === 'superseded' ? (
                    <div className="row" style={{ gap: 6 }}>
                      <span className="card-meta">{replacedByText(item)}</span>
                      {item.replaced_by_status === 'pending' ? (
                        <button type="button" className="btn btn-ghost" onClick={() => onView('queue')}>
                          Open the queue
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
          {cursor ? (
            <div style={{ marginTop: 'var(--space-4)' }}>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={loadingMore}
                onClick={() => void loadMore()}
              >
                {loadingMore ? 'Loading…' : 'Show more'}
              </button>
            </div>
          ) : null}
        </CardSection>
      )}
    </>
  )
}

/** Matches the server's limit on "Ask for changes" feedback (RequestChangesRequest). */
const FEEDBACK_MAX = 2000
/** Matches the server's limit on one edited field (EditTextRequest). */
const EDIT_FIELD_MAX = 2000

/** The words a creative's text can be corrected in, as people read them. */
const EDITABLE_TEXT: { key: string; label: string; rows: number }[] = [
  { key: 'headline', label: 'Headline', rows: 2 },
  { key: 'body', label: 'Main text', rows: 4 },
  { key: 'cta', label: 'Button text', rows: 1 },
]

/** Danger look for Reject, kept local to this page (no global button style yet). */
const REJECT_OUTLINE = { color: 'var(--color-danger)', borderColor: 'var(--color-danger)' }
const REJECT_SOLID = { background: 'var(--color-danger)', borderColor: 'var(--color-danger)', color: '#fff' }

/** A file id as the server issues it; anything else is never requested. */
const FILE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** The creative's current text, read from the stored change when it has one. */
function currentText(payload: Record<string, unknown> | undefined): Record<string, string> {
  const pick = (value: unknown): Record<string, unknown> | null =>
    value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
  const root = pick(payload) ?? {}
  const copy = pick(root.copy) ?? pick(pick(root.content)?.copy) ?? {}
  const out: Record<string, string> = {}
  for (const { key } of EDITABLE_TEXT) {
    const value = copy[key]
    out[key] = typeof value === 'string' ? value : ''
  }
  return out
}

/** A stored preview image, fetched with the session (never a bare URL). */
function usePreview(id: string | null | undefined): { src: string; failed: boolean } {
  const [state, setState] = useState<{ src: string; failed: boolean }>({ src: '', failed: false })
  useEffect(() => {
    setState({ src: '', failed: false })
    if (!id || !FILE_ID.test(id)) return
    const controller = new AbortController()
    let url = ''
    void brandKitApi
      .fileBlob(id, controller.signal)
      .then(({ blob }) => {
        if (controller.signal.aborted) return
        // Only images are shown inline; anything else is not rendered.
        if (!/^image\/(png|jpeg|webp)$/.test(blob.type)) {
          setState({ src: '', failed: true })
          return
        }
        url = URL.createObjectURL(blob)
        setState({ src: url, failed: false })
      })
      .catch(() => {
        if (!controller.signal.aborted) setState({ src: '', failed: true })
      })
    return () => {
      controller.abort()
      if (url) URL.revokeObjectURL(url)
    }
  }, [id])
  return state
}

function CreativePreview({ id, title, compact = false }: { id?: string | null; title: string; compact?: boolean }) {
  const { src, failed } = usePreview(id)
  if (!id) return null
  const size = compact ? { maxWidth: 120, maxHeight: 90 } : { maxWidth: '100%', maxHeight: 420 }
  if (src) {
    return (
      <img
        src={src}
        alt={`Preview: ${title}`}
        style={{ ...size, display: 'block', borderRadius: 6, border: '1px solid var(--color-border)' }}
      />
    )
  }
  return (
    <span className="card-meta" role="img" aria-label={`Preview: ${title}`}>
      {failed ? 'Preview unavailable' : 'Loading preview…'}
    </span>
  )
}

function QueueView({ onView }: { onView: (view: View) => void }) {
  const { canWrite, refresh } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const { data, loading, error, reload } = useResource(
    (signal) => api.approvals(signal),
    [],
    'approvals',
  )

  const [detail, setDetail] = useState<ApprovalDetail | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  /** The item a reviewer is about to reject, and their optional reason. */
  const [rejecting, setRejecting] = useState<ApprovalOut | null>(null)
  const [reason, setReason] = useState('')
  /** "Ask for changes": the item and what the reviewer wants different. */
  const [revising, setRevising] = useState<ApprovalOut | null>(null)
  const [feedback, setFeedback] = useState('')
  /** "Edit text": the item, its text as it was, and the text as edited. */
  const [editing, setEditing] = useState<{ item: ApprovalDetail; before: Record<string, string> } | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})

  const writable = canWrite('approvals')

  const closeReject = useCallback(() => {
    setRejecting(null)
    setReason('')
  }, [])

  const closeRevise = useCallback(() => {
    setRevising(null)
    setFeedback('')
  }, [])

  const closeEdit = useCallback(() => {
    setEditing(null)
    setDraft({})
  }, [])

  /** Reject asks first, with room for an optional reason. */
  const askReject = (item: ApprovalOut) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setDetail(null)
    setReason('')
    setRejecting(item)
  }

  const askChanges = (item: ApprovalOut) => {
    if (!writable || !item.can_request_changes) {
      push('View-only access for your role', 'warning')
      return
    }
    setDetail(null)
    setFeedback('')
    setRevising(item)
  }

  const startEdit = async (item: ApprovalOut) => {
    if (!item.can_edit_text) {
      push('View-only access for your role', 'warning')
      return
    }
    try {
      const full = detail?.id === item.id ? detail : await api.approval(item.id)
      const before = currentText(full.payload)
      setDetail(null)
      setDraft(before)
      setEditing({ item: full, before })
    } catch (caught) {
      fromError(caught)
    }
  }

  const afterChange = async () => {
    await reload()
    // The sidebar badge lives on the session.
    await refresh()
  }

  const decide = async (item: ApprovalOut, approve: boolean, note = '') => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusyId(item.id)
    try {
      fromResult(
        approve
          ? await api.approve(item.id)
          : await api.reject(item.id, note.trim().slice(0, REASON_MAX)),
      )
      setDetail(null)
      if (!approve) closeReject()
      await afterChange()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  const sendChanges = async () => {
    if (!revising) return
    const text = feedback.trim().slice(0, FEEDBACK_MAX)
    if (!text) {
      push('Say what you would like changed.', 'warning')
      return
    }
    setBusyId(revising.id)
    try {
      const result = await api.requestChanges(revising.id, text)
      fromResult(result)
      closeRevise()
      await afterChange()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  const saveText = async () => {
    if (!editing) return
    const changed: Record<string, string> = {}
    for (const { key } of EDITABLE_TEXT) {
      const value = (draft[key] ?? '').slice(0, EDIT_FIELD_MAX)
      if (value !== (editing.before[key] ?? '')) changed[key] = value
    }
    if (Object.keys(changed).length === 0) {
      push('Nothing has changed yet.', 'info')
      return
    }
    setBusyId(editing.item.id)
    try {
      fromResult(await api.editApprovalText(editing.item.id, changed))
      closeEdit()
      await afterChange()
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
      <PageHeader
        title="Approvals"
        description="Changes your agents want to make, waiting for you to approve them. Approving publishes exactly what you see here."
      />
      <ViewSwitch value="queue" onView={onView} />

      {data.length === 0 ? (
        <EmptyState
          title="Queue is clear"
          description="Nothing is waiting for you. Changes appear here when Review before publishing is set to Always ask me, or to Ask me for big changes and an agent wants to make a big one."
        />
      ) : (
        <CardSection
          title="Waiting on you"
          count={data.length}
          description="Open a change to see it in full before you decide. Approving publishes it exactly as shown."
        >
        <div className="stack">
          {data.map((item) => (
            <div key={item.id} className="card approval-row">
              {item.preview_file_id ? (
                <CreativePreview id={item.preview_file_id} title={item.title} compact />
              ) : null}
              <div className="approval-body">
                <Tag tone="accent">{approvalTypeLabel(item.type)}</Tag>
                <div className="approval-title">{item.title}</div>
                <div className="card-meta">
                  from {item.agent} · {item.meta}
                </div>
                <WhyLines lines={item.why} />
              </div>
              <div className="row nowrap">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void open(item)}
                >
                  Review
                </button>
                {item.can_edit_text ? (
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={busyId === item.id}
                    onClick={() => void startEdit(item)}
                  >
                    Edit text
                  </button>
                ) : null}
                {item.can_request_changes ? (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={busyId === item.id}
                    onClick={() => askChanges(item)}
                  >
                    Ask for changes
                  </button>
                ) : null}
                <button
                  type="button"
                  className="btn btn-secondary"
                  style={REJECT_OUTLINE}
                  disabled={!writable || busyId === item.id}
                  onClick={() => askReject(item)}
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
          title={approvalTypeLabel(detail.type)}
          width={720}
          onClose={() => setDetail(null)}
          actions={
            <>
              {detail.can_edit_text ? (
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={busyId === detail.id}
                  onClick={() => void startEdit(detail)}
                >
                  Edit text
                </button>
              ) : null}
              {detail.can_request_changes ? (
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busyId === detail.id}
                  onClick={() => askChanges(detail)}
                >
                  Ask for changes
                </button>
              ) : null}
              <button
                type="button"
                className="btn btn-secondary"
                style={REJECT_OUTLINE}
                disabled={!writable || busyId === detail.id}
                onClick={() => askReject(detail)}
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
            <WhyLines lines={detail.why} />
            {detail.preview_file_id ? (
              <div>
                <div className="card-kicker">How it looks</div>
                <CreativePreview id={detail.preview_file_id} title={detail.title} />
              </div>
            ) : null}
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

      {revising ? (
        <Dialog
          title="Ask for changes"
          width={560}
          onClose={closeRevise}
          actions={
            <>
              <button type="button" className="btn btn-secondary" onClick={closeRevise}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busyId === revising.id || !feedback.trim()}
                onClick={() => void sendChanges()}
              >
                {busyId === revising.id ? 'Sending…' : 'Send for changes'}
              </button>
            </>
          }
        >
          <div className="stack-sm">
            <p style={{ margin: 0 }}>
              <strong>{revising.title}</strong> will be replaced by a revised version, made with your
              notes. The revised version comes back here for you to approve.
            </p>
            <Field
              label="What would you like changed?"
              htmlFor="revise-feedback"
              hint="For example: shorter headline, friendlier tone, mention free delivery."
            >
              <textarea
                id="revise-feedback"
                className="input"
                rows={4}
                maxLength={FEEDBACK_MAX}
                value={feedback}
                onChange={(event) => setFeedback(event.target.value.slice(0, FEEDBACK_MAX))}
              />
            </Field>
          </div>
        </Dialog>
      ) : null}

      {editing ? (
        <Dialog
          title="Edit text"
          width={620}
          onClose={closeEdit}
          actions={
            <>
              <button type="button" className="btn btn-secondary" onClick={closeEdit}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busyId === editing.item.id}
                onClick={() => void saveText()}
              >
                {busyId === editing.item.id ? 'Saving…' : 'Save text'}
              </button>
            </>
          }
        >
          <div className="stack-sm">
            <p style={{ margin: 0 }}>
              Correct the wording of <strong>{editing.item.title}</strong> yourself. No AI is used, and it
              stays here for you to approve.
            </p>
            {EDITABLE_TEXT.map(({ key, label, rows }) => (
              <Field key={key} label={label} htmlFor={`edit-${key}`}>
                <textarea
                  id={`edit-${key}`}
                  className="input"
                  rows={rows}
                  maxLength={EDIT_FIELD_MAX}
                  value={draft[key] ?? ''}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, [key]: event.target.value.slice(0, EDIT_FIELD_MAX) }))
                  }
                />
              </Field>
            ))}
          </div>
        </Dialog>
      ) : null}

      {rejecting ? (
        <Dialog
          title="Reject this change?"
          width={520}
          onClose={closeReject}
          actions={
            <>
              <button type="button" className="btn btn-secondary" onClick={closeReject}>
                Keep it
              </button>
              <button
                type="button"
                className="btn btn-primary"
                style={REJECT_SOLID}
                disabled={!writable || busyId === rejecting.id}
                onClick={() => void decide(rejecting, false, reason)}
              >
                {busyId === rejecting.id ? 'Rejecting…' : 'Reject'}
              </button>
            </>
          }
        >
          <div className="stack-sm">
            <p style={{ margin: 0 }}>
              <strong>{rejecting.title}</strong> will not be published. The agent may suggest
              something different on a later run.
            </p>
            <Field
              label="Reason (optional)"
              htmlFor="reject-reason"
              hint="Helps your team see why it was turned down."
            >
              <textarea
                id="reject-reason"
                className="input"
                rows={3}
                maxLength={REASON_MAX}
                value={reason}
                onChange={(event) => setReason(event.target.value.slice(0, REASON_MAX))}
              />
            </Field>
          </div>
        </Dialog>
      ) : null}
    </>
  )
}
