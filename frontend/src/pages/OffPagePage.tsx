/**
 * Off-Page & PR workspace - discovered placements and competitor alerts.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import type { ActionResult, BacklinkOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { VersionHistory } from '@/components/AuditHistory'
import { PageTabs } from '@/components/PageTabs'
import { useToasts } from '@/components/Toasts'
import { ListPager } from '@/components/ListPager'
import {
  Dialog,
  EmptyState,
  Field,
  ErrorState,
  Loading,
  PageHeader,
  SectionHeading,
  statusTone,
  Tag,
} from '@/components/ui'
import { usePagedList } from '@/hooks/usePagedList'
import { useResource } from '@/hooks/useResource'

/** One line each on what the comparison words mean, for a non-technical reader. */
const BACKLINK_COMPARE_HELP =
  'Compare this scan with an earlier one. New: a site suggested for the first time. Fixed: a site that moved forward, for example from pitched to won. Worse: a site that turned you down, or a link you had won that is gone.'
const COMPETITOR_COMPARE_HELP =
  'Compare this scan with an earlier one. New: a competitor link spotted for the first time. Fixed: a competitor link you have since answered with a pitch. Worse: always 0 here, as nothing in this scan can get worse.'

export function OffPagePage() {
  const { canView, canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const [view, setView] = useState<'opportunities' | 'history'>('opportunities')
  const { data, loading, error, reload } = useResource(
    (signal) => api.offpage(signal),
    [],
    'offpage',
  )
  const [busyId, setBusyId] = useState<string | null>(null)
  const [editingContact, setEditingContact] = useState<BacklinkOut | null>(null)

  const writable = canWrite('offpage')
  const backlinkPager = usePagedList(data?.backlinks ?? [], { storageKey: 'offpage-backlinks' })
  const alertPager = usePagedList(data?.alerts ?? [], { storageKey: 'offpage-alerts' })

  const act = async (id: string, action: () => Promise<ActionResult>) => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    setBusyId(id)
    try {
      fromResult(await action())
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  if (loading && !data) return <Loading label="Loading placements…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  return (
    <>
      <PageHeader
        title="Off-Page & PR"
        description="Backlink opportunities, outreach and competitor link monitoring."
      />
      {canView('offpage') ? (
        <PageTabs
          value={view}
          options={[
            { value: 'opportunities', label: 'Opportunities' },
            { value: 'history', label: 'Scan history' },
          ]}
          onChange={setView}
        />
      ) : null}
      {view === 'history' && canView('offpage') ? (
        <>
          {/* Scans are read-only records: the server marks them not
              restorable, so the kit shows no restore button. */}
          <VersionHistory
            kind="backlink_scan"
            title="Backlink scans"
            description={BACKLINK_COMPARE_HELP}
          />
          <VersionHistory
            kind="competitor_scan"
            title="Competitor scans"
            description={COMPETITOR_COMPARE_HELP}
          />
        </>
      ) : (
      <>
      <SectionHeading
        title="Sites that could link to you"
        description="Suggested link partners. Add a publisher's email to have pitches emailed to them; without one, pitches come to your team as drafts."
      />

      {data.backlinks.length === 0 ? (
        <EmptyState
          title="No placement targets yet"
          description="The Backlink Node Discovery agent works out what your site is about from your pages and the searches people find you with. Connect your website on the Integrations screen, then let the agent run."
        />
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Domain</th>
                <th>Authority</th>
                <th>Relevance</th>
                <th>Placement Type</th>
                <th>Status</th>
                <th>Publisher email</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {backlinkPager.slice.map((target) => (
                <tr key={target.id}>
                  <td>{target.domain}</td>
                  <td>{target.authority_measured === false ? <span className="muted">Not measured</span> : target.authority}</td>
                  <td>{Math.round(target.relevance * 100)}%</td>
                  <td>{target.placement_type}</td>
                  <td>
                    <Tag tone={statusTone(target.status)}>{target.status_label}</Tag>
                  </td>
                  <td>
                    {target.has_contact ? (
                      <span className="nowrap">{target.contact_email_masked}</span>
                    ) : (
                      <span className="muted">None</span>
                    )}
                    {writable ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => setEditingContact(target)}
                      >
                        {target.has_contact ? 'Change' : 'Add'}
                      </button>
                    ) : null}
                  </td>
                  <td className="table-actions">
                    {target.show_launch ? (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={busyId === target.id}
                        onClick={() =>
                          void act(target.id, () => api.launchOutreach(target.id))
                        }
                      >
                        {busyId === target.id ? 'Drafting…' : 'Launch outreach'}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.backlinks.length > 0 ? (
        <ListPager
          total={backlinkPager.total}
          from={backlinkPager.from}
          to={backlinkPager.to}
          page={backlinkPager.page}
          pageCount={backlinkPager.pageCount}
          pageSize={backlinkPager.pageSize}
          onPage={backlinkPager.setPage}
          onPageSize={backlinkPager.setPageSize}
          label="Backlink pages"
        />
      ) : null}

      <SectionHeading
        title="Sites linking to your competitors"
        description="A publisher that just linked to a competitor has proven it covers this subject and will link out - the best moment to approach it."
        spaced
      />

      {data.alerts.length === 0 ? (
        <EmptyState
          title="No competitor activity detected"
          description="On the Agents screen, open the Competitor Link Monitor's Settings and add your competitors. Then connect Google Search Console or Bing Webmaster Tools on the Integrations screen so it has real link data to compare."
        />
      ) : (
        <div className="stack">
          {alertPager.slice.map((alert) => (
            <div
              key={alert.id}
              className="card"
              style={{
                flexDirection: 'row',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 16,
              }}
            >
              <div style={{ fontSize: 13 }}>{alert.text}</div>
              <button
                type="button"
                className="btn btn-secondary nowrap"
                disabled={alert.counter_launched || busyId === alert.id}
                onClick={() =>
                  void act(alert.id, () => api.launchCounterPitch(alert.id))
                }
              >
                {busyId === alert.id ? 'Drafting…' : alert.button_label}
              </button>
            </div>
          ))}
        </div>
      )}
      {data.alerts.length > 0 ? (
        <ListPager
          total={alertPager.total}
          from={alertPager.from}
          to={alertPager.to}
          page={alertPager.page}
          pageCount={alertPager.pageCount}
          pageSize={alertPager.pageSize}
          onPage={alertPager.setPage}
          onPageSize={alertPager.setPageSize}
          label="Competitor alert pages"
        />
      ) : null}
      </>
      )}

      {editingContact ? (
        <ContactDialog
          target={editingContact}
          onClose={() => setEditingContact(null)}
          onSaved={async () => {
            setEditingContact(null)
            await reload()
          }}
        />
      ) : null}
    </>
  )
}


/** Add, change or remove the address pitches to this publisher are emailed to. */
function ContactDialog({
  target,
  onClose,
  onSaved,
}: {
  target: BacklinkOut
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { fromResult, fromError } = useToasts()
  const [email, setEmail] = useState(target.contact_email ?? '')
  const [busy, setBusy] = useState(false)

  const save = async (value: string | null) => {
    setBusy(true)
    try {
      fromResult(await api.setTargetContact(target.id, value))
      await onSaved()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      title={`Publisher email for ${target.domain}`}
      onClose={onClose}
      actions={
        <>
          {target.has_contact ? (
            <button type="button" className="btn btn-ghost danger" disabled={busy} onClick={() => void save(null)}>
              Remove
            </button>
          ) : null}
          <div className="dialog-actions-spacer" />
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !email.trim()}
            onClick={() => void save(email.trim())}
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
        </>
      }
    >
      <Field
        label="Editor or webmaster email"
        htmlFor="contact-email"
        hint="Approved pitches to this site are emailed here, through your email (SMTP) integration. Without an address, pitches come to your team as drafts."
      >
        <input
          id="contact-email"
          className="input"
          type="email"
          autoComplete="off"
          placeholder="editor@example.com"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </Field>
    </Dialog>
  )
}
