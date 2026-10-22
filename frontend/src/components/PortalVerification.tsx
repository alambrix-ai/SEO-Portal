/**
 * Portal Admin → Verification: which connectors are proven against a real account.
 *
 * Each connector type can have a live check (a few small, safe calls to the
 * vendor). A check runs with one workspace's connection; a pass counts for a
 * fixed number of days, because vendors change their APIs. Connectors that
 * have not passed show a "Beta" pill to customers, or can be hidden from
 * them entirely with the switch at the top.
 */
import { Fragment, useEffect, useMemo, useState } from 'react'

import { api } from '@/api/client'
import type { VerificationRow, VerificationWorkspace } from '@/api/types'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { ChevronDownIcon } from '@/components/icons'
import { Dialog, ErrorState, Field, Loading, SectionHeading, Tag } from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { when } from '@/lib/time'

function day(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

function StatusCell({ row }: { row: VerificationRow }) {
  if (row.running) return <Tag tone="accent">Checking…</Tag>
  switch (row.state) {
    case 'verified':
      return (
        <span className="verify-status">
          <Tag tone="accent-2">Verified</Tag>
          <span className="small muted">
            {day(row.last_run_at)} · {row.environment_label}
          </span>
        </span>
      )
    case 'failed':
      return (
        <span className="verify-status">
          <Tag tone="danger">Failed</Tag>
          <span className="small muted verify-reason" title={row.reason}>
            {row.reason || 'See the report'}
          </span>
        </span>
      )
    case 'expired':
      return (
        <span className="verify-status">
          <Tag tone="warning">Expired</Tag>
          <span className="small muted">Passed {day(row.last_run_at)}. Check it again.</span>
        </span>
      )
    case 'never':
      return <Tag tone="outline">Never checked</Tag>
    default:
      return <Tag tone="neutral">No live check written yet</Tag>
  }
}

function Report({ row }: { row: VerificationRow }) {
  const run = row.latest
  if (!run) return <p className="small muted">No report yet.</p>
  return (
    <div className="verify-report">
      <div className="small muted verify-report-meta">
        {run.ok ? 'Passed' : 'Failed'} {when(run.finished_at)} · {run.environment_label}
        {run.workspace_name ? ` · ${run.workspace_name}'s connection` : ''}
        {run.run_by ? ` · run by ${run.run_by}` : ''}
      </div>
      <ol className="verify-steps">
        {run.steps.map((step) => (
          <li key={step.key} className="verify-step">
            <Tag tone={step.skipped ? 'outline' : step.ok ? 'accent-2' : 'danger'}>
              {step.skipped ? 'Skipped' : step.ok ? 'Passed' : 'Failed'}
            </Tag>
            <div className="verify-step-copy">
              <div className="cell-title">
                {step.label}
                <span className="small muted">
                  {' '}
                  · {step.kind === 'write' ? 'Changes something, then undoes it' : 'Read only'}
                  {!step.skipped ? ` · ${step.seconds.toFixed(1)}s` : ''}
                </span>
              </div>
              {step.detail ? <div className="small">{step.detail}</div> : null}
              {Object.keys(step.evidence ?? {}).length ? (
                <code className="verify-evidence">{JSON.stringify(step.evidence)}</code>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
      {run.note ? <p className="small muted">{run.note}</p> : null}
    </div>
  )
}

/** "verifyKey=value" lines to an options object. The server checks them again. */
function parseOptions(text: string): { options: Record<string, string>; error?: string } {
  const options: Record<string, string> = {}
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const at = line.indexOf('=')
    if (at < 1) return { options, error: `"${line}" needs a name and a value, as name=value.` }
    const key = line.slice(0, at).trim()
    if (!/^verify[A-Za-z0-9_]+$/.test(key)) {
      return { options, error: `"${key}" is not allowed. Option names start with verify.` }
    }
    options[key] = line.slice(at + 1).trim()
  }
  return { options }
}

function RunDialog({ row, onClose, onStarted }: { row: VerificationRow; onClose: () => void; onStarted: () => void }) {
  const { fromResult, fromError } = useToasts()
  const workspaces = useResource((signal) => api.portalVerificationWorkspaces(row.slug, signal), [row.slug])
  const [workspaceId, setWorkspaceId] = useState('')
  const [markedTest, setMarkedTest] = useState(false)
  const [allowWrites, setAllowWrites] = useState(false)
  const [busy, setBusy] = useState(false)
  /** Run-only options as typed: one "verifyKey=value" per line. */
  const [optionText, setOptionText] = useState('')
  const parsed = useMemo(() => parseOptions(optionText), [optionText])

  const list = workspaces.data ?? []
  useEffect(() => {
    const first = list[0]
    if (!workspaceId && first) setWorkspaceId(first.id)
  }, [list, workspaceId])
  const chosen: VerificationWorkspace | undefined = list.find((w) => w.id === workspaceId)
  const connectionIsTest = chosen ? chosen.environment !== 'live' : false
  const effectiveTest = connectionIsTest || markedTest

  useEffect(() => {
    setMarkedTest(false)
    setAllowWrites(false)
  }, [workspaceId])

  const start = async () => {
    if (!chosen || parsed.error) return
    setBusy(true)
    try {
      fromResult(
        await api.runPortalVerification(row.slug, {
          workspace_id: chosen.id,
          allow_writes: effectiveTest && allowWrites,
          environment: markedTest && !connectionIsTest ? 'test_account' : '',
          options: parsed.options,
        }),
      )
      onStarted()
      onClose()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      title={`Check ${row.name}`}
      width={560}
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!chosen || busy || Boolean(parsed.error)}
            onClick={start}
          >
            {busy ? 'Starting…' : 'Run the check'}
          </button>
        </>
      }
    >
      <p className="small muted">
        The check uses one workspace's saved connection to make a few small calls to {row.name}. Read steps change
        nothing. Write steps create something harmless and remove it again, and run only on a test or sandbox
        account.
      </p>
      {workspaces.loading && !workspaces.data ? <Loading label="Finding workspaces…" /> : null}
      {workspaces.error ? <ErrorState message={workspaces.error} onRetry={workspaces.reload} /> : null}
      {workspaces.data && !list.length ? (
        <p className="small">
          No workspace has {row.name} connected. Connect it to a free test account in a workspace first.
        </p>
      ) : null}
      {list.length ? (
        <>
          <Field label="Use the connection from" htmlFor="verify-workspace">
            <select
              id="verify-workspace"
              className="input"
              value={workspaceId}
              onChange={(event) => setWorkspaceId(event.target.value)}
            >
              {list.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name} ({w.environment_label})
                </option>
              ))}
            </select>
          </Field>
          {!connectionIsTest ? (
            <label className="verify-toggle">
              <input type="checkbox" checked={markedTest} onChange={(event) => setMarkedTest(event.target.checked)} />
              <span>
                This connection is a test account
                <span className="small muted block">
                  Tick only if the account behind this connection is a test or sandbox account. It applies to this
                  check only.
                </span>
              </span>
            </label>
          ) : null}
          {effectiveTest ? (
            <label className="verify-toggle">
              <input type="checkbox" checked={allowWrites} onChange={(event) => setAllowWrites(event.target.checked)} />
              <span>
                Allow test writes
                <span className="small muted block">
                  Also run the steps that create something (paused, unpublished or a draft) and delete it again.
                </span>
              </span>
            </label>
          ) : null}
          <details className="verify-advanced">
            <summary>Advanced options</summary>
            <Field
              label="Options for this check"
              htmlFor="verify-options"
              hint="One per line, as name=value. Names start with verify, for example verifyImageUrl=https://… or verifyAllowPaidPost=yes. Used for this check only and never saved."
              error={parsed.error}
            >
              <textarea
                id="verify-options"
                className="input"
                rows={3}
                spellCheck={false}
                placeholder="verifyImageUrl=https://example.com/picture.jpg"
                value={optionText}
                onChange={(event) => setOptionText(event.target.value)}
              />
            </Field>
          </details>
        </>
      ) : null}
    </Dialog>
  )
}

export function PortalVerification() {
  const { fromResult, fromError } = useToasts()
  const data = useResource((signal) => api.portalVerification(signal), [], 'portal:verification')
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [running, setRunning] = useState<VerificationRow | null>(null)
  const [saving, setSaving] = useState(false)

  const rows = useMemo(() => data.data?.items ?? [], [data.data])
  const anyRunning = rows.some((r) => r.running)

  // While a check runs in the background, look again every few seconds.
  const reload = data.reload
  useEffect(() => {
    if (!anyRunning) return
    const timer = window.setInterval(() => void reload(), 3000)
    return () => window.clearInterval(timer)
  }, [anyRunning, reload])

  const verified = rows.filter((r) => r.verified).length
  const withChecks = rows.filter((r) => r.has_steps).length

  const toggleHide = async (next: boolean) => {
    setSaving(true)
    try {
      fromResult(await api.savePortalVerificationSettings(next))
      await data.reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="portal-panel" aria-label="Verification">
      <SectionHeading
        title="Verification"
        description={`Live checks of each connector against a real account. A pass counts for ${
          data.data?.verified_for_days ?? 30
        } days. Customers see a Beta label on any connector that has not passed.`}
      />
      {data.loading && !data.data ? <Loading label="Loading verification…" /> : null}
      {data.error && !data.data ? <ErrorState message={data.error} onRetry={data.reload} /> : null}

      {data.data ? (
        <>
          {!data.data.storage_ready ? (
            <p className="verify-banner small">
              Results cannot be saved yet: the database needs its latest update (migration 0018). Ask whoever runs
              the server to apply it.
            </p>
          ) : null}
          <div className="card elev-sm portal-surface verify-settings">
            <label className="verify-toggle">
              <input
                type="checkbox"
                checked={data.data.hide_unverified_connectors}
                disabled={saving || !data.data.storage_ready}
                onChange={(event) => void toggleHide(event.target.checked)}
              />
              <span>
                Hide unverified connectors from customers
                <span className="small muted block">
                  When on, connectors without a current pass are not offered in any workspace. Workspaces that
                  already connected one keep it working.
                </span>
              </span>
            </label>
            <div className="small muted">
              {verified} of {rows.length} verified · {withChecks} have a live check
            </div>
          </div>

          <div className="card elev-sm portal-surface">
            <div className="table-scroll">
              <table className="table verify-table">
                <thead>
                  <tr>
                    <th>Connector</th>
                    <th>Status</th>
                    <th>Last checked</th>
                    <th>
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const expanded = Boolean(open[row.slug])
                    return (
                      <Fragment key={row.slug}>
                        <tr>
                          <td>
                            <div className="entity-cell">
                              <ConnectorIcon slug={row.slug} name={row.name} size={28} />
                              <div className="entity-cell-text">
                                <div className="entity-cell-title">{row.name}</div>
                                <div className="small muted">{row.category}</div>
                              </div>
                            </div>
                          </td>
                          <td>
                            <StatusCell row={row} />
                            {row.run_error ? <div className="small verify-reason">{row.run_error}</div> : null}
                          </td>
                          <td className="small muted">{row.last_run_at ? when(row.last_run_at) : '—'}</td>
                          <td className="table-actions">
                            {row.latest ? (
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm"
                                aria-expanded={expanded}
                                onClick={() => setOpen((prev) => ({ ...prev, [row.slug]: !expanded }))}
                              >
                                Report <ChevronDownIcon size={14} />
                              </button>
                            ) : null}
                            <button
                              type="button"
                              className="btn btn-secondary btn-sm"
                              disabled={!row.has_steps || row.running}
                              title={row.has_steps ? undefined : 'No live check has been written for this connector yet'}
                              onClick={() => setRunning(row)}
                            >
                              {row.running ? 'Running…' : 'Run'}
                            </button>
                          </td>
                        </tr>
                        {expanded ? (
                          <tr className="verify-report-row">
                            <td colSpan={4}>
                              <Report row={row} />
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ) : null}

      {running ? (
        <RunDialog row={running} onClose={() => setRunning(null)} onStarted={() => void data.reload()} />
      ) : null}
    </section>
  )
}
