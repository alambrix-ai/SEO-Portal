/**
 * Technical SEO — the site's health as a working list.
 *
 * Ordered worst-first and grouped by kind, because the point is not to admire
 * the total but to work through it. Every row carries what to do, not just
 * what is wrong: "missing schema" is an observation, "add Product markup so
 * this page is eligible for rich results" is something a marketing lead can
 * act on or hand to a developer.
 *
 * Two things the screen is careful about:
 *
 * "Fixed this month" is shown as prominently as the open count. It is the
 * number that shows the work happened, and it comes from findings that stopped
 * appearing rather than from anything anybody typed.
 *
 * When no page-speed source is connected, the screen says mobile speed is
 * *unmeasured* rather than showing nothing. "No speed issues" and "speed was
 * never measured" must not look the same.
 */
import { useState } from 'react'

import { ApiError, api } from '@/api/client'
import type { SeoIssueOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  SectionHeading,
  Segmented,
  StatTile,
  Tag,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

type StatusFilter = 'open' | 'fixed' | 'ignored'

const SEVERITY_TONE: Record<string, 'accent' | 'outline' | 'neutral'> = {
  high: 'accent',
  medium: 'outline',
  low: 'neutral',
}

export function TechnicalSeoPage() {
  const { canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()

  const [status, setStatus] = useState<StatusFilter>('open')
  const [kind, setKind] = useState('')
  const { data, loading, error, reload } = useResource(
    () => api.seoAudit({ status, kind }),
    [status, kind],
  )

  const [ignoring, setIgnoring] = useState<SeoIssueOut | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const writable = canWrite('seo')

  const reopen = async (issue: SeoIssueOut) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusyId(issue.id)
    try {
      fromResult(await api.reopenIssue(issue.id))
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  if (loading && !data) return <Loading label="Loading the audit…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <div className="grid-tiles">
        <StatTile kicker="Open Issues" value={data.open_count} meta="Across the site" />
        <StatTile kicker="High Severity" value={data.high_count} meta="Fix these first" />
        <StatTile
          kicker="Fixed This Month"
          value={data.fixed_this_month}
          meta="Stopped appearing"
        />
        <StatTile kicker="Pages Audited" value={data.pages_audited} meta="Crawled and checked" />
      </div>

      {data.pages_audited === 0 ? (
        <div className="panel-empty" style={{ marginTop: 12 }}>
          <p>Nothing has been crawled yet.</p>
          <p className="small muted">
            Connect a CMS or a repository, then configure and start On-Page SEO
            Sync. The auditor runs over the pages it stores.
          </p>
        </div>
      ) : null}

      {data.vitals_unavailable ? (
        <div className="notice" style={{ marginTop: 12 }}>
          {data.vitals_unavailable}
        </div>
      ) : null}

      {data.by_kind.length > 0 ? (
        <>
          <SectionHeading title="By type" spaced />
          <div className="row" style={{ flexWrap: 'wrap' }}>
            <button
              type="button"
              className={`btn ${kind === '' ? 'btn-primary' : 'btn-secondary'}`}
              onClick={() => setKind('')}
            >
              All ({data.open_count})
            </button>
            {data.by_kind.map((group) => (
              <button
                key={group.kind}
                type="button"
                className={`btn ${kind === group.kind ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setKind(group.kind)}
              >
                {group.label} ({group.count})
              </button>
            ))}
          </div>
        </>
      ) : null}

      <SectionHeading
        title="Findings"
        description={data.last_audit_at ? undefined : 'The auditor has not run yet.'}
        spaced
        action={
          <Segmented
            name="issue-status"
            value={status}
            options={[
              { value: 'open', label: 'Open' },
              { value: 'fixed', label: 'Fixed' },
              { value: 'ignored', label: 'Ignored' },
            ]}
            onChange={(next) => {
              setStatus(next as StatusFilter)
              setKind('')
            }}
          />
        }
      />

      {data.issues.length === 0 ? (
        <EmptyState
          title={status === 'open' ? 'No open issues' : `Nothing ${status}`}
          description={
            status === 'open'
              ? 'The audit found nothing outstanding on the pages it checked.'
              : undefined
          }
        />
      ) : (
        <div className="stack-sm">
          {data.issues.map((issue) => (
            <Blueprint key={issue.id} className="card elev-sm issue-card">
              <div className="row-between">
                <div className="row">
                  <Tag tone={SEVERITY_TONE[issue.severity] ?? 'neutral'}>
                    {issue.severity}
                  </Tag>
                  <Tag tone="outline">{issue.kind_label}</Tag>
                </div>
                <span className="card-meta">{issue.age_label}</span>
              </div>

              <div className="issue-url">{issue.url}</div>
              <div className="issue-summary">{issue.summary}</div>
              {/* The recommendation is the product. A list of observations is
                  a report; a list of actions is a service. */}
              <div className="issue-fix">{issue.recommendation}</div>

              {issue.ignore_note ? (
                <div className="card-meta">Ignored: {issue.ignore_note}</div>
              ) : null}

              {writable ? (
                <div className="row" style={{ marginTop: 4 }}>
                  {issue.status === 'ignored' ? (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={busyId === issue.id}
                      onClick={() => void reopen(issue)}
                    >
                      Reopen
                    </button>
                  ) : issue.status === 'open' ? (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => setIgnoring(issue)}
                    >
                      Ignore
                    </button>
                  ) : null}
                </div>
              ) : null}
            </Blueprint>
          ))}
        </div>
      )}

      {ignoring ? (
        <IgnoreDialog
          issue={ignoring}
          onClose={() => setIgnoring(null)}
          onIgnored={async () => {
            setIgnoring(null)
            await reload()
          }}
        />
      ) : null}
    </>
  )
}

function IgnoreDialog({
  issue,
  onClose,
  onIgnored,
}: {
  issue: SeoIssueOut
  onClose: () => void
  onIgnored: () => Promise<void>
}) {
  const { fromError } = useToasts()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      await api.ignoreIssue(issue.id, note.trim())
      await onIgnored()
    } catch (caught) {
      fromError(caught instanceof ApiError ? caught : caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      title={`Ignore: ${issue.kind_label}`}
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || note.trim().length < 3}
            onClick={() => void save()}
          >
            {busy ? 'Saving…' : 'Ignore this issue'}
          </button>
        </>
      }
    >
      <p className="small" style={{ marginTop: 0 }}>
        {issue.url} — {issue.summary}
      </p>
      <Field
        label="Why are you accepting this?"
        htmlFor="ignore-note"
        required
        hint="The auditor will not raise it again, so this note is the only record of the decision."
      >
        <input
          id="ignore-note"
          className="input"
          autoFocus
          placeholder="Deliberate — this is a landing page with no body copy"
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </Field>
    </Dialog>
  )
}
