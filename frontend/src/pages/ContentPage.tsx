/**
 * Content library: every article, search campaign, social post and email the
 * content agents wrote — preview, edit, approve & publish, reject, download.
 *
 * Article and email HTML is rendered in a sandboxed iframe with no scripts,
 * so generated markup can never run in the console.
 */
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, safeFilename } from '@/api/client'
import { isWebLink, socialApi, type SocialPost } from '@/api/social'
import type { ActionResult, ContentAsset, ContentAssetDetail } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { useConfirm } from '@/components/Confirm'
import { Menu } from '@/components/Menu'
import { ShareDialog, SocialStatusChips } from '@/components/social/ShareDialog'
import { useToasts } from '@/components/Toasts'
import { MoreIcon } from '@/components/icons'
import { ListPager } from '@/components/ListPager'
import { Drawer, EmptyState, ErrorState, Field, Loading, PageHeader, StatusDot } from '@/components/ui'
import { usePagedList } from '@/hooks/usePagedList'
import { invalidateResourceCache, useResource } from '@/hooks/useResource'
import { when } from '@/lib/time'

const KINDS: { key: string; label: string }[] = [
  { key: '', label: 'All' },
  { key: 'article', label: 'Articles' },
  { key: 'search_campaign', label: 'Search campaigns' },
  { key: 'social_post', label: 'Social posts' },
  { key: 'email', label: 'Emails' },
  { key: 'creative', label: 'Designs' },
]

const KIND_LABEL: Record<string, string> = {
  article: 'Article',
  search_campaign: 'Search campaign',
  social_post: 'Social post',
  email: 'Email',
  creative: 'Design',
}

/** A design can be shared once it is approved (or already downloaded). */
const SHAREABLE = new Set(['approved', 'exported'])

const STATUS_FILTERS = [
  { key: '', label: 'Any status' },
  { key: 'draft', label: 'Waiting for approval' },
  { key: 'scheduled', label: 'Scheduled' },
  { key: 'published', label: 'Published' },
  { key: 'approved', label: 'Ready to download' },
  { key: 'failed', label: 'Failed' },
  { key: 'rejected', label: 'Rejected' },
]

type Tone = 'success' | 'warning' | 'danger' | 'neutral' | 'info'
const TONE: Record<string, Tone> = {
  draft: 'warning',
  approved: 'info',
  scheduled: 'info',
  published: 'success',
  exported: 'success',
  rejected: 'neutral',
  failed: 'danger',
}

function channelLabel(channel: string): string {
  if (!channel || channel === 'export') return 'Download'
  return channel.replace(/^export:/, '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

async function download(id: string, format: string) {
  const { blob, filename } = await api.exportAsset(id, format)
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = safeFilename(filename, `content.${safeFilename(format, 'file')}`)
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 2000)
}

/** Rejecting cannot be undone from here, so it is always confirmed first. */
const REJECT_CONFIRM = {
  title: 'Reject this content?',
  message: 'It will not be published. You can still find it under Rejected.',
  confirmLabel: 'Reject',
  cancelLabel: 'Keep it',
  tone: 'danger' as const,
}

function useImage(id: string | undefined): string {
  const [src, setSrc] = useState('')
  useEffect(() => {
    if (!id) return
    let url = ''
    let cancelled = false
    void api
      .imageBlob(id)
      .then(({ blob }) => {
        if (cancelled) return
        url = URL.createObjectURL(blob)
        setSrc(url)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
      if (url) URL.revokeObjectURL(url)
    }
  }, [id])
  return src
}

export function ContentPage() {
  const { canWrite } = useAuth()
  const { fromResult, fromError } = useToasts()
  const writable = canWrite('content')
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState('')
  const { data, loading, error, reload } = useResource(
    (signal) => api.assets({ kind, status }, signal),
    [kind, status],
    `assets:${kind}:${status}`,
  )
  const [openId, setOpenId] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [share, setShare] = useState<{ id: string; title: string } | null>(null)
  const confirm = useConfirm()
  // Shared designs' posts, for the status chips and Cancel. Optional: the list still works without them.
  const social = useResource((signal) => socialApi.posts(undefined, signal), [], 'social:posts')
  const postsByDesign = useMemo(() => {
    const out = new Map<string, SocialPost[]>()
    for (const post of social.data ?? []) {
      if (!post.creative_asset_id) continue
      out.set(post.creative_asset_id, [...(out.get(post.creative_asset_id) ?? []), post])
    }
    return out
  }, [social.data])
  const postById = useMemo(() => new Map((social.data ?? []).map((post) => [post.id, post])), [social.data])

  const cancelPost = async (post: SocialPost) => {
    const ok = await confirm({
      title: 'Cancel this post?',
      message: `The scheduled post to ${post.network_label} will not go out.`,
      confirmLabel: 'Cancel the post',
      cancelLabel: 'Keep it',
      tone: 'danger',
    })
    if (!ok) return
    setBusyId(post.id)
    try {
      fromResult(await socialApi.cancel(post.id))
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
      await Promise.all([reload(), social.reload()])
    }
  }

  const reject = async (id: string) => {
    if (!(await confirm(REJECT_CONFIRM))) return
    await act(id, () => api.rejectAsset(id))
  }

  const act = async (id: string, action: () => Promise<ActionResult>) => {
    setBusyId(id)
    try {
      fromResult(await action())
      invalidateResourceCache('assets')
      await reload()
    } catch (caught) {
      fromError(caught)
      invalidateResourceCache('assets')
      await reload()
    } finally {
      setBusyId(null)
    }
  }

  const counts = useMemo(() => {
    const out: Record<string, number> = {}
    for (const asset of data ?? []) out[asset.status] = (out[asset.status] ?? 0) + 1
    return out
  }, [data])
  const pager = usePagedList(data ?? [], { storageKey: 'content', resetKey: `${kind}|${status}` })

  return (
    <>
      <PageHeader
        title="Content"
        description="Everything your content agents wrote. Approve to publish (or schedule) through your connected platforms, or download it to use anywhere."
        actions={
          <Link to="/brand" className="btn btn-secondary">
            Brand profile
          </Link>
        }
      />

      <div className="tabs tabs-underline" role="tablist">
        {KINDS.map((option) => (
          <button
            key={option.key || 'all'}
            type="button"
            role="tab"
            aria-selected={kind === option.key}
            className={`tab${kind === option.key ? ' is-active' : ''}`}
            onClick={() => setKind(option.key)}
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="content-toolbar">
        <select className="input input-narrow content-filter" value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Filter by status">
          {STATUS_FILTERS.map((option) => (
            <option key={option.key} value={option.key}>
              {option.label}
            </option>
          ))}
        </select>
        {counts.draft ? <span className="small muted">{counts.draft} waiting for your approval</span> : null}
      </div>

      {loading && !data ? (
        <Loading label="Loading content…" />
      ) : error && !data ? (
        <ErrorState message={error} onRetry={reload} />
      ) : !data || data.length === 0 ? (
        <EmptyState
          title="Nothing here yet"
          description="Turn on the content agents — SEO Article Writer, Search Campaign Builder, Social Media Manager and Email Marketer — and what they write appears here."
          action={
            <Link to="/agents" className="btn btn-primary">
              Open agents
            </Link>
          }
        />
      ) : (
        <div className="panel">
          <ul className="content-list">
            {pager.slice.map((asset) => (
              <li key={asset.id} className="content-row">
                <button type="button" className="content-row-main" onClick={() => setOpenId(asset.id)}>
                  <Thumb asset={asset} />
                  <span className="content-row-text">
                    <span className="content-row-title">{asset.title || KIND_LABEL[asset.kind] || asset.kind}</span>
                    <span className="content-row-meta">
                      {KIND_LABEL[asset.kind] ?? asset.kind} · {channelLabel(asset.channel)} · {asset.agent_name}
                      {asset.scheduled_for ? ` · for ${when(asset.scheduled_for)}` : ''}
                    </span>
                    {asset.error ? <span className="content-row-error">{asset.error}</span> : null}
                  </span>
                </button>
                <div className="content-row-state">
                  <StatusDot tone={TONE[asset.status] ?? 'neutral'}>
                    {postById.get(asset.id.toLowerCase())?.status_label || asset.status_label}
                  </StatusDot>
                  {asset.kind === 'creative' ? (
                    <SocialStatusChips posts={postsByDesign.get(asset.id.toLowerCase()) ?? []} />
                  ) : null}
                </div>
                <div className="content-row-actions">
                  {writable && ['draft', 'failed'].includes(asset.status) ? (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busyId === asset.id}
                      onClick={() => void act(asset.id, () => api.publishAsset(asset.id))}
                    >
                      {asset.can_publish ? (asset.scheduled_for ? 'Approve & schedule' : 'Approve & publish') : 'Approve'}
                    </button>
                  ) : null}
                  {asset.kind === 'creative' && SHAREABLE.has(asset.status) ? (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => setShare({ id: asset.id, title: asset.title || KIND_LABEL.creative || 'Design' })}
                    >
                      {writable ? 'Share or schedule' : 'Sharing status'}
                      <span className="sr-only"> for {asset.title || 'this design'}</span>
                    </button>
                  ) : null}
                  <Menu
                    label={`More for ${asset.title}`}
                    trigger={<MoreIcon />}
                    items={[
                      { label: 'Preview', onSelect: () => setOpenId(asset.id) },
                      ...Object.entries(asset.formats).map(([format, label]) => ({
                        label: `Download ${label}`,
                        onSelect: () => void download(asset.id, format).catch(fromError),
                      })),
                      ...(writable && !['published', 'rejected'].includes(asset.status)
                        ? [{ label: 'Reject', tone: 'danger' as const, onSelect: () => void reject(asset.id) }]
                        : []),
                      ...(writable && postById.get(asset.id.toLowerCase())?.can_cancel && busyId !== asset.id
                        ? [
                            {
                              label: 'Cancel scheduled post',
                              tone: 'danger' as const,
                              onSelect: () => void cancelPost(postById.get(asset.id.toLowerCase()) as SocialPost),
                            },
                          ]
                        : []),
                      // Only a plain https address is opened; anything else is not offered.
                      ...(asset.remote_url && isWebLink(asset.remote_url, true)
                        ? [
                            {
                              label: 'Open on the platform',
                              onSelect: () => window.open(asset.remote_url, '_blank', 'noopener,noreferrer'),
                            },
                          ]
                        : []),
                    ]}
                  />
                </div>
              </li>
            ))}
          </ul>
          <ListPager
            total={pager.total}
            from={pager.from}
            to={pager.to}
            page={pager.page}
            pageCount={pager.pageCount}
            pageSize={pager.pageSize}
            onPage={pager.setPage}
            onPageSize={pager.setPageSize}
            label="Content pages"
          />
        </div>
      )}

      {openId ? (
        <AssetDrawer
          id={openId}
          writable={writable}
          onClose={() => setOpenId(null)}
          onChanged={async () => {
            await reload()
          }}
        />
      ) : null}

      {share ? (
        <ShareDialog
          creativeId={share.id}
          title={share.title}
          canWrite={writable}
          onClose={() => setShare(null)}
          onChanged={() => {
            void reload()
            void social.reload()
          }}
        />
      ) : null}
    </>
  )
}

function Thumb({ asset }: { asset: ContentAsset }) {
  const src = useImage(asset.image_ids[0])
  return src ? (
    <img className="content-thumb" src={src} alt="" />
  ) : (
    <span className="content-thumb is-empty" aria-hidden="true">
      {(KIND_LABEL[asset.kind] ?? '?').charAt(0)}
    </span>
  )
}

function AssetDrawer({
  id,
  writable,
  onClose,
  onChanged,
}: {
  id: string
  writable: boolean
  onClose: () => void
  onChanged: () => Promise<void>
}) {
  const { fromResult, fromError, push } = useToasts()
  const [asset, setAsset] = useState<ContentAssetDetail | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const confirm = useConfirm()

  const load = async () => {
    try {
      setAsset(await api.asset(id))
    } catch (caught) {
      fromError(caught)
    }
  }
  useEffect(() => {
    void load()
  }, [id])

  const editable = editableFields(asset)

  const startEdit = () => {
    if (!asset) return
    setDraft(
      Object.fromEntries(
        editable.map((field) => [
          field.key,
          field.paragraphs
            ? ((asset.content[field.key] as string[] | undefined) ?? []).join('\n\n')
            : String(asset.content[field.key] ?? ''),
        ]),
      ),
    )
    setEditing(true)
  }

  const saveEdit = async () => {
    if (!asset) return
    setBusy(true)
    try {
      const changes: Record<string, unknown> = {}
      for (const field of editable) {
        const value = draft[field.key] ?? ''
        changes[field.key] = field.paragraphs
          ? value.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean)
          : value
      }
      setAsset(await api.updateAsset(asset.id, { content: { ...asset.content, ...changes } }))
      setEditing(false)
      push('Saved', 'success')
      await onChanged()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const run = async (action: () => Promise<ActionResult>) => {
    setBusy(true)
    try {
      fromResult(await action())
      invalidateResourceCache('assets')
      await load()
      await onChanged()
    } catch (caught) {
      fromError(caught)
      invalidateResourceCache('assets')
      await load()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Drawer
      title={asset?.title || 'Content'}
      subtitle={asset ? `${KIND_LABEL[asset.kind] ?? asset.kind} · ${channelLabel(asset.channel)} · ${asset.status_label}` : 'Loading…'}
      width={720}
      onClose={onClose}
      actions={
        asset ? (
          <>
            {Object.entries(asset.formats).slice(0, 3).map(([format, label]) => (
              <button key={format} type="button" className="btn btn-ghost btn-sm" onClick={() => void download(asset.id, format).catch(fromError)}>
                {label}
              </button>
            ))}
            <div className="dialog-actions-spacer" />
            {writable && editable.length && !['published'].includes(asset.status) ? (
              editing ? (
                <>
                  <button type="button" className="btn btn-secondary" onClick={() => setEditing(false)}>
                    Cancel
                  </button>
                  <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void saveEdit()}>
                    Save
                  </button>
                </>
              ) : (
                <button type="button" className="btn btn-secondary" onClick={startEdit}>
                  Edit
                </button>
              )
            ) : null}
            {writable && !editing && ['draft', 'failed'].includes(asset.status) ? (
              <>
                <button type="button" className="btn btn-ghost danger" disabled={busy} onClick={() =>
                    void (async () => {
                      if (await confirm(REJECT_CONFIRM)) await run(() => api.rejectAsset(asset.id))
                    })()
                  }
                >
                  Reject
                </button>
                <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void run(() => api.publishAsset(asset.id))}>
                  {asset.can_publish ? (asset.scheduled_for ? 'Approve & schedule' : 'Approve & publish') : 'Approve'}
                </button>
              </>
            ) : null}
          </>
        ) : null
      }
    >
      {!asset ? (
        <p className="guide-text muted">Loading…</p>
      ) : editing ? (
        editable.map((field) => (
          <Field key={field.key} label={field.label} htmlFor={`edit-${field.key}`}>
            {field.long ? (
              <textarea
                id={`edit-${field.key}`}
                className="input"
                rows={field.key.includes('html') || field.paragraphs ? 16 : 5}
                value={draft[field.key] ?? ''}
                onChange={(event) => setDraft((d) => ({ ...d, [field.key]: event.target.value }))}
              />
            ) : (
              <input
                id={`edit-${field.key}`}
                className="input"
                value={draft[field.key] ?? ''}
                onChange={(event) => setDraft((d) => ({ ...d, [field.key]: event.target.value }))}
              />
            )}
          </Field>
        ))
      ) : (
        <>
          {asset.error ? <div className="banner is-danger">{asset.error}</div> : null}
          {asset.status === 'approved' ? (
            <div className="banner is-info">This channel has no publishing connection, so it is ready to download.</div>
          ) : null}
          {asset.status === 'published' && asset.kind === 'article' ? (
            <div className="banner is-info">
              Sent to your site as a draft (or an open pull request) — review and publish it there to make it live.
            </div>
          ) : null}
          {asset.scheduled_for && asset.status !== 'published' ? (
            <p className="guide-text muted small">Planned for {when(asset.scheduled_for)}.</p>
          ) : null}
          <Preview asset={asset} />
        </>
      )}
    </Drawer>
  )
}

type EditField = { key: string; label: string; long?: boolean; paragraphs?: boolean }

/** The fields a person can correct, per kind. Emails are edited as copy: their
 *  HTML is rebuilt from these fields when published or downloaded. */
function editableFields(asset: ContentAssetDetail | null): EditField[] {
  if (!asset) return []
  const candidates: EditField[] =
    asset.kind === 'email'
      ? [
          { key: 'subject', label: 'Subject line' },
          { key: 'preview_text', label: 'Preview text' },
          { key: 'headline', label: 'Headline' },
          { key: 'paragraphs', label: 'Body — blank line between paragraphs', long: true, paragraphs: true },
          { key: 'cta_label', label: 'Button text' },
        ]
      : [
          { key: 'title', label: 'Title' },
          { key: 'meta_title', label: 'Meta title' },
          { key: 'meta_description', label: 'Meta description', long: true },
          { key: 'text', label: 'Text', long: true },
          { key: 'body_html', label: 'Body (HTML)', long: true },
        ]
  return candidates.filter((field) =>
    field.paragraphs ? Array.isArray(asset.content[field.key]) : typeof asset.content[field.key] === 'string',
  )
}

function Preview({ asset }: { asset: ContentAssetDetail }) {
  const c = asset.content
  const image = useImage(asset.image_ids[0])
  const str = (key: string) => (typeof c[key] === 'string' ? (c[key] as string) : '')

  if (asset.kind === 'article') {
    // The FAQ is stored apart from the body and appended when published, so
    // show it here the same way.
    const faq = (Array.isArray(c.faq) ? (c.faq as { q?: string; a?: string }[]) : []).filter((i) => i.q && i.a)
    const faqHtml = faq.length
      ? `<h2>${escapeHtml(str('faq_heading') || 'Frequently asked questions')}</h2>` +
        faq.map((i) => `<h3>${escapeHtml(i.q ?? '')}</h3><p>${escapeHtml(i.a ?? '')}</p>`).join('')
      : ''
    const doc = `<!doctype html><html><head><meta charset="utf-8"><style>body{font:15px/1.6 system-ui,sans-serif;color:#111;margin:16px;max-width:680px}h1{font-size:24px}img{max-width:100%}</style></head><body><h1>${escapeHtml(str('title') || asset.title)}</h1>${str('body_html')}${faqHtml}</body></html>`
    return (
      <>
        <dl className="summary-list">
          {str('target_keyword') ? (
            <div>
              <dt>Target keyword</dt>
              <dd>{str('target_keyword')}</dd>
            </div>
          ) : null}
          {str('meta_title') ? (
            <div>
              <dt>Meta title</dt>
              <dd>{str('meta_title')}</dd>
            </div>
          ) : null}
          {str('meta_description') ? (
            <div>
              <dt>Meta description</dt>
              <dd>{str('meta_description')}</dd>
            </div>
          ) : null}
        </dl>
        {image ? <img className="preview-hero" src={image} alt="" /> : null}
        <iframe className="preview-frame" title="Article preview" sandbox="" srcDoc={doc} />
      </>
    )
  }

  if (asset.kind === 'email') {
    return (
      <>
        <dl className="summary-list">
          <div>
            <dt>Subject</dt>
            <dd>{str('subject')}</dd>
          </div>
          <div>
            <dt>Preview text</dt>
            <dd>{str('preview_text')}</dd>
          </div>
        </dl>
        <iframe className="preview-frame is-email" title="Email preview" sandbox="" srcDoc={str('html')} />
      </>
    )
  }

  if (asset.kind === 'social_post') {
    const hashtags = Array.isArray(c.hashtags) ? (c.hashtags as string[]) : []
    return (
      <div className="social-preview">
        <div className="social-preview-head">{channelLabel(String(c.network ?? asset.channel))}</div>
        {image ? <img className="social-preview-image" src={image} alt="" /> : null}
        {str('title') ? <div className="social-preview-title">{str('title')}</div> : null}
        <div className="social-preview-text">{str('text')}</div>
        {hashtags.length ? <div className="social-preview-tags">{hashtags.map((tag) => (tag.startsWith('#') ? tag : `#${tag}`)).join(' ')}</div> : null}
        {str('link') ? <div className="social-preview-link">{str('link')}</div> : null}
      </div>
    )
  }

  if (asset.kind === 'search_campaign') {
    const groups = Array.isArray(c.ad_groups) ? (c.ad_groups as Record<string, unknown>[]) : []
    const negatives = Array.isArray(c.negative_keywords) ? (c.negative_keywords as Record<string, unknown>[]) : []
    const text = (item: unknown) =>
      typeof item === 'string'
        ? item
        : `${String((item as Record<string, unknown>).text ?? '')} · ${String((item as Record<string, unknown>).match_type ?? '').toLowerCase()}`
    const budget =
      typeof c.daily_budget === 'number'
        ? `${String(c.currency ?? '')}${c.daily_budget.toFixed(2)} a day${c.budget_is_recommendation ? ' (recommended — no monthly budget set)' : ''}`
        : ''
    return (
      <>
        <dl className="summary-list">
          {Object.entries({ Campaign: c.name, Goal: c.goal, Budget: budget, Bidding: c.bidding, 'Starts as': c.status })
            .filter(([, v]) => v !== undefined && v !== '')
            .map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{String(value)}</dd>
              </div>
            ))}
        </dl>
        {groups.map((group, index) => {
          const ad = (group.ad ?? {}) as Record<string, unknown>
          return (
            <section key={index} className="campaign-group">
              <div className="guide-title">{String(group.name ?? group.theme ?? `Ad group ${index + 1}`)}</div>
              {group.landing_url ? <div className="small muted">{String(group.landing_url)}</div> : null}
              <div className="campaign-cols">
                <div>
                  <div className="small muted">Keywords</div>
                  <ul className="campaign-list">
                    {(Array.isArray(group.keywords) ? (group.keywords as unknown[]) : []).map((k, i) => (
                      <li key={i}>{text(k)}</li>
                    ))}
                  </ul>
                </div>
                <div>
                  <div className="small muted">Headlines</div>
                  <ul className="campaign-list">
                    {(Array.isArray(ad.headlines) ? (ad.headlines as string[]) : []).map((h, i) => (
                      <li key={i}>{h}</li>
                    ))}
                  </ul>
                  <div className="small muted">Descriptions</div>
                  <ul className="campaign-list">
                    {(Array.isArray(ad.descriptions) ? (ad.descriptions as string[]) : []).map((d, i) => (
                      <li key={i}>{d}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </section>
          )
        })}
        {negatives.length ? (
          <section className="campaign-group">
            <div className="guide-title">Negative keywords</div>
            <ul className="campaign-list">
              {negatives.map((k, i) => (
                <li key={i}>{text(k)}</li>
              ))}
            </ul>
          </section>
        ) : null}
      </>
    )
  }

  return <pre className="preview-json">{JSON.stringify(c, null, 2)}</pre>
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch] as string)
}
