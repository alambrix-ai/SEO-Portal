/**
 * Share or schedule an approved design on social networks.
 *
 * 1. Pick networks. Only connected ones can be chosen; the rest link to
 *    Integrations.
 * 2. Write a caption per network, with a live character count against that
 *    network's limit (and a title, link or subreddit where the network needs one).
 * 3. Post now, or pick a date and time in the WORKSPACE time zone, shown by name.
 * 4. Review a summary, then confirm.
 *
 * Below the form, the design's posts and their status: Scheduled, Posted (with
 * a link), Failed (with the reason) or Cancelled, and a Cancel button while a
 * post is still scheduled.
 *
 * Everything typed is checked here first so mistakes show up while typing, but
 * the server checks it all again and decides.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ApiError } from '@/api/client'
import {
  bodyLength,
  cleanSubreddit,
  draftProblems,
  EMPTY_DRAFT,
  formatLocal,
  hashtagCount,
  localInZone,
  MAX_CAPTION_CHARS,
  MAX_LINK_CHARS,
  MAX_TITLE_CHARS,
  NETWORK_BY_KEY,
  NETWORKS,
  scheduleProblem,
  socialApi,
  type NetworkKey,
  type NetworkRules,
  type PostDraft,
  type ScheduleItemInput,
  type SocialPost,
} from '@/api/social'
import type { ConnectorOut } from '@/api/types'
import { useConfirm } from '@/components/Confirm'
import { useToasts } from '@/components/Toasts'
import { Dialog, Field, Tag, type TagTone } from '@/components/ui'
import { friendlyLoadError } from '@/hooks/useResource'
import { sentence } from '@/lib/plain'
import { softenErrorMessage } from '@/lib/softenError'
import { isKnownTimeZone, timeZoneLabel } from '@/lib/timezones'
import '@/styles/creative.css'

// ── Status chips ───────────────────────────────────────────────────────────
/** Each status has a text label and a mark, so it never depends on colour alone. */
const STATUS_LOOK: Record<string, { tone: TagTone; mark: string; label: string }> = {
  scheduled: { tone: 'accent', mark: '◷', label: 'Scheduled' },
  publishing: { tone: 'accent', mark: '…', label: 'Posting now' },
  published: { tone: 'accent-2', mark: '✓', label: 'Posted' },
  failed: { tone: 'danger', mark: '!', label: 'Failed' },
  rejected: { tone: 'outline', mark: '✕', label: 'Cancelled' },
}

function statusLook(post: SocialPost): { tone: TagTone; mark: string; label: string } {
  const look = STATUS_LOOK[post.status] ?? { tone: 'neutral' as TagTone, mark: '•', label: post.status }
  return { ...look, label: post.status_label || look.label }
}

function postWhere(post: SocialPost): string {
  return post.network === 'reddit' && post.subreddit ? `${post.network_label} (r/${post.subreddit})` : post.network_label
}

function postWhen(post: SocialPost): string {
  if (post.status === 'published' && post.published_at) {
    const date = new Date(post.published_at)
    return Number.isNaN(date.getTime()) ? '' : `Posted ${date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`
  }
  if (!post.scheduled_local) return ''
  return `${post.status === 'scheduled' ? 'Goes out' : 'Planned for'} ${formatLocal(post.scheduled_local)} (${timeZoneLabel(post.timezone)})`
}

/** Compact chips for a list row: "X: Posted", "Reddit: Scheduled". */
export function SocialStatusChips({ posts }: { posts: SocialPost[] }) {
  if (posts.length === 0) return null
  return (
    <span className="cs-chip-row" role="list" aria-label="Sharing status">
      {posts.map((post) => {
        const look = statusLook(post)
        return (
          <span role="listitem" key={post.id}>
            <Tag tone={look.tone} title={post.reason || post.note || undefined}>
              <span aria-hidden="true">{look.mark} </span>
              {postWhere(post)}: {look.label}
            </Tag>
          </span>
        )
      })}
    </span>
  )
}

/** One post's full status, with its link, reason, note and Cancel. */
function PostRow({
  post,
  canCancel,
  busy,
  onCancel,
}: {
  post: SocialPost
  canCancel: boolean
  busy: boolean
  onCancel: () => void
}) {
  const look = statusLook(post)
  const where = postWhere(post)
  const time = postWhen(post)
  return (
    <li className="cs-callout">
      <div>
        <strong>{where}</strong>{' '}
        <Tag tone={look.tone}>
          <span aria-hidden="true">{look.mark} </span>
          {look.label}
        </Tag>
      </div>
      {time ? <div className="cs-note">{time}</div> : null}
      {post.status === 'published' && post.link ? (
        <div>
          <a href={post.link} target="_blank" rel="noopener noreferrer">
            View the post on {post.network_label}
            <span className="sr-only"> (opens in a new tab)</span>
          </a>
        </div>
      ) : null}
      {post.status === 'failed' ? (
        <p className="cs-note">
          <strong>Why it failed:</strong> {sentence(post.reason) || 'The network did not accept it. Try again later.'}
        </p>
      ) : null}
      {post.note ? (
        <p className="cs-note">
          <strong>Note:</strong> {sentence(post.note)}
        </p>
      ) : null}
      {post.can_cancel && canCancel ? (
        <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onCancel}>
          Cancel this post<span className="sr-only"> to {where}</span>
        </button>
      ) : null}
    </li>
  )
}

// ── Caption counter ────────────────────────────────────────────────────────
type CountState = 'ok' | 'near' | 'over'

function countState(used: number, limit: number): CountState {
  if (used > limit) return 'over'
  return used >= limit * 0.9 ? 'near' : 'ok'
}

/**
 * The visible count is read with the field (aria-describedby). A separate
 * polite live region speaks only when the count crosses into "nearly full" or
 * "over the limit", so a screen reader is not interrupted on every key.
 */
function Counter({ id, used, limit, label }: { id: string; used: number; limit: number; label: string }) {
  const state = countState(used, limit)
  const [announcement, setAnnouncement] = useState('')
  const previous = useRef<CountState>(state)
  useEffect(() => {
    if (state === previous.current) return
    previous.current = state
    if (state === 'over') setAnnouncement(`${label} is over the limit: ${used} of ${limit} characters.`)
    else if (state === 'near') setAnnouncement(`${label} is nearly full: ${limit - used} characters left.`)
    else setAnnouncement(`${label} is within the limit.`)
  }, [state, used, limit, label])
  return (
    <>
      <span id={id} className="cs-note">
        {used.toLocaleString()} / {limit.toLocaleString()} characters
        {state === 'over' ? ' (over the limit)' : state === 'near' ? ' (nearly full)' : ''}
      </span>
      <span className="sr-only" aria-live="polite" aria-atomic="true">
        {announcement}
      </span>
    </>
  )
}

// ── Connections ────────────────────────────────────────────────────────────
type Connection = 'connected' | 'not_connected' | 'unavailable' | 'unknown'

function connectionOf(rules: NetworkRules, connectors: ConnectorOut[] | null): Connection {
  if (connectors === null) return 'unknown'
  const found = connectors.find((c) => c.slug === rules.connector)
  if (!found) return 'unavailable'
  return found.connected ? 'connected' : 'not_connected'
}

function errorText(caught: unknown, fallback: string): string {
  if (caught instanceof ApiError) {
    if (caught.status === 429) return 'You are going a little fast. Wait a minute, then try again.'
    if (caught.status === 403) return 'You do not have permission to do that. Ask a workspace admin if you need it.'
    if (caught.status === 404) return 'That design or post is no longer in the library. Refresh the page.'
    return softenErrorMessage(caught.message, fallback)
  }
  if (caught instanceof Error) return friendlyLoadError(caught)
  return fallback
}

function isAbort(caught: unknown): boolean {
  return caught instanceof Error && caught.name === 'AbortError'
}

/** Polls while something is about to change; stops after this many checks. */
const POLL_MS = 8000
const MAX_POLLS = 20

// ── Dialog ─────────────────────────────────────────────────────────────────
export function ShareDialog({
  creativeId,
  title,
  canWrite,
  onClose,
  onChanged,
}: {
  creativeId: string
  /** The design's name, for the dialog title. */
  title: string
  /** Edit access to Content: needed to post, schedule or cancel. */
  canWrite: boolean
  onClose: () => void
  /** Called after posts were scheduled or cancelled. */
  onChanged?: () => void
}) {
  const toasts = useToasts()
  const confirm = useConfirm()

  const [connectors, setConnectors] = useState<ConnectorOut[] | null>(null)
  const [connectorsChecked, setConnectorsChecked] = useState(false)
  const [zone, setZone] = useState<string | null>(null)
  const [zoneProblem, setZoneProblem] = useState('')

  const [posts, setPosts] = useState<SocialPost[]>([])
  const [postsLoading, setPostsLoading] = useState(true)
  const [postsError, setPostsError] = useState('')
  const [polls, setPolls] = useState(0)

  const [step, setStep] = useState<'compose' | 'review'>('compose')
  const [selected, setSelected] = useState<NetworkKey[]>([])
  const [drafts, setDrafts] = useState<Record<string, PostDraft>>({})
  const [mode, setMode] = useState<'now' | 'schedule'>('now')
  const [date, setDate] = useState('')
  const [time, setTime] = useState('')
  const [showErrors, setShowErrors] = useState(false)
  const [formError, setFormError] = useState('')
  const [busy, setBusy] = useState(false)
  const [cancelling, setCancelling] = useState('')

  // Connections and the workspace time zone, once.
  useEffect(() => {
    const controller = new AbortController()
    api
      .connectors('', controller.signal)
      .then((list) => setConnectors(Array.isArray(list) ? list : []))
      .catch((caught: unknown) => {
        // No access to Integrations: every network is offered and the server says which can post.
        if (!isAbort(caught)) setConnectors(null)
      })
      .finally(() => {
        if (!controller.signal.aborted) setConnectorsChecked(true)
      })
    api
      .brandProfile(controller.signal)
      .then((state) => {
        const name = (state?.timezone ?? '').trim() || 'UTC'
        if (isKnownTimeZone(name)) setZone(name)
        else setZoneProblem('This browser does not know your workspace time zone, so only Post now is available.')
      })
      .catch((caught: unknown) => {
        if (!isAbort(caught)) setZoneProblem('We could not load your workspace time zone, so only Post now is available.')
      })
    return () => controller.abort()
  }, [])

  const loadPosts = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const list = await socialApi.posts(creativeId, signal)
        setPosts(list)
        setPostsError('')
      } catch (caught) {
        if (!isAbort(caught)) setPostsError(errorText(caught, 'We could not load this design’s posts.'))
      } finally {
        if (!signal?.aborted) setPostsLoading(false)
      }
    },
    [creativeId],
  )

  useEffect(() => {
    const controller = new AbortController()
    void loadPosts(controller.signal)
    return () => controller.abort()
  }, [loadPosts])

  // Keep checking while a post is going out right now.
  const settling = posts.some(
    (p) => p.status === 'publishing' || (p.status === 'scheduled' && p.scheduled_for !== null && Date.parse(p.scheduled_for) <= Date.now()),
  )
  useEffect(() => {
    if (!settling || polls >= MAX_POLLS) return
    const timer = window.setTimeout(() => {
      setPolls((n) => n + 1)
      void loadPosts()
    }, POLL_MS)
    return () => window.clearTimeout(timer)
  }, [settling, polls, loadPosts])

  const draftFor = (key: NetworkKey): PostDraft => drafts[key] ?? EMPTY_DRAFT
  const setDraft = (key: NetworkKey, patch: Partial<PostDraft>) =>
    setDrafts((current) => ({ ...current, [key]: { ...(current[key] ?? EMPTY_DRAFT), ...patch } }))

  const toggle = (key: NetworkKey) => {
    setFormError('')
    setSelected((current) => (current.includes(key) ? current.filter((k) => k !== key) : [...current, key]))
  }

  const problems = useMemo(
    () => Object.fromEntries(selected.map((key) => [key, draftProblems(key, drafts[key] ?? EMPTY_DRAFT)])),
    [selected, drafts],
  ) as Record<string, Partial<Record<keyof PostDraft, string>>>

  const whenLocal = mode === 'schedule' && date && time ? `${date}T${time.slice(0, 5)}` : ''
  const timeProblem = mode === 'schedule' ? (zone ? scheduleProblem(whenLocal, zone) : zoneProblem || 'Loading your workspace time zone…') : ''
  const today = zone ? localInZone(zone).slice(0, 10) : ''

  const review = () => {
    setShowErrors(true)
    setFormError('')
    if (selected.length === 0) {
      setFormError('Choose at least one network.')
      return
    }
    const bad = selected.find((key) => Object.keys(problems[key] ?? {}).length > 0)
    if (bad) {
      setFormError(`Check the ${NETWORK_BY_KEY.get(bad)?.label ?? bad} post: some details need a fix.`)
      return
    }
    if (timeProblem) {
      setFormError(timeProblem)
      return
    }
    setStep('review')
  }

  const send = async () => {
    if (busy) return
    // Checked again at the moment of sending: a minute may have passed on the summary.
    if (mode === 'schedule') {
      const late = zone ? scheduleProblem(whenLocal, zone) : zoneProblem
      if (late) {
        setStep('compose')
        setFormError(late)
        return
      }
    }
    const items: ScheduleItemInput[] = selected.map((key) => {
      const rules = NETWORK_BY_KEY.get(key) as NetworkRules
      const draft = draftFor(key)
      return {
        network: key,
        caption: draft.caption.trim().slice(0, MAX_CAPTION_CHARS),
        hashtags: [],
        link: rules.takesLink ? draft.link.trim().slice(0, MAX_LINK_CHARS) : '',
        title: rules.titleLimit ? draft.title.split(/\s+/).filter(Boolean).join(' ').slice(0, MAX_TITLE_CHARS) : '',
        subreddit: key === 'reddit' ? cleanSubreddit(draft.subreddit) : null,
        when_local: mode === 'schedule' ? whenLocal : null,
      }
    })
    setBusy(true)
    setFormError('')
    try {
      const result = await socialApi.schedule({ creative_asset_id: creativeId, items })
      if (result.toast) toasts.push(result.toast.message, result.toast.kind)
      else toasts.push(mode === 'now' ? 'Posting now.' : 'Scheduled.', 'success')
      setSelected([])
      setDrafts({})
      setDate('')
      setTime('')
      setMode('now')
      setShowErrors(false)
      setStep('compose')
      setPolls(0)
      await loadPosts()
      onChanged?.()
    } catch (caught) {
      setStep('compose')
      setFormError(errorText(caught, 'We could not schedule these posts. Please try again.'))
    } finally {
      setBusy(false)
    }
  }

  const cancelPost = async (post: SocialPost) => {
    const ok = await confirm({
      title: 'Cancel this post?',
      message: `The post to ${postWhere(post)} will not go out.`,
      confirmLabel: 'Cancel the post',
      cancelLabel: 'Keep it',
      tone: 'danger',
    })
    if (!ok) return
    setCancelling(post.id)
    try {
      const result = await socialApi.cancel(post.id)
      if (result.toast) toasts.push(result.toast.message, result.toast.kind)
      if (result.post) {
        const fresh = result.post
        setPosts((current) => current.map((p) => (p.id === fresh.id ? fresh : p)))
      } else {
        await loadPosts()
      }
      onChanged?.()
    } catch (caught) {
      toasts.push(errorText(caught, 'We could not cancel that post. Please try again.'), 'error')
      await loadPosts()
    } finally {
      setCancelling('')
    }
  }

  const zoneText = zone ? timeZoneLabel(zone) : ''

  // ── Compose ──
  const networkPicker = (
    <fieldset className="cs-fieldset">
      <legend className="cs-legend">Where should it go?</legend>
      {!connectorsChecked ? <p className="cs-note">Checking your connected networks…</p> : null}
      {connectorsChecked && connectors === null ? (
        <p className="cs-note">We could not check which networks are connected. If one isn’t, we will tell you when you send.</p>
      ) : null}
      <ul className="cs-chip-row" style={{ listStyle: 'none', padding: 0 }}>
        {NETWORKS.map((rules) => {
          const state = connectionOf(rules, connectors)
          const usable = state === 'connected' || state === 'unknown'
          const checked = selected.includes(rules.key)
          return (
            <li key={rules.key}>
              {usable ? (
                <label className={`cs-chip${checked ? ' is-checked' : ''}`}>
                  <input type="checkbox" checked={checked} disabled={!canWrite || busy} onChange={() => toggle(rules.key)} />
                  {rules.label}
                </label>
              ) : state === 'not_connected' ? (
                <span className="cs-note">
                  {rules.label}: <Link to="/connectors">Connect {rules.label} in Integrations</Link>
                </span>
              ) : (
                <span className="cs-note">{rules.label}: not available on this platform</span>
              )}
            </li>
          )
        })}
      </ul>
    </fieldset>
  )

  const editors = selected.map((key) => {
    const rules = NETWORK_BY_KEY.get(key) as NetworkRules
    const draft = draftFor(key)
    const errors = showErrors ? problems[key] ?? {} : {}
    const base = `share-${key}`
    const used = bodyLength(key, draft.caption, rules.takesLink ? draft.link : '')
    const titleUsed = Array.from(draft.title.split(/\s+/).filter(Boolean).join(' ')).length
    return (
      <fieldset key={key} className="cs-fieldset">
        <legend className="cs-legend">{rules.label}</legend>
        {rules.titleLimit ? (
          <Field label={`${rules.titleLabel}${rules.needsTitle ? '' : ' (optional)'}`} htmlFor={`${base}-title`} required={rules.needsTitle} error={errors.title}>
            <input
              id={`${base}-title`}
              className="input"
              type="text"
              value={draft.title}
              maxLength={MAX_TITLE_CHARS}
              required={rules.needsTitle}
              aria-invalid={errors.title ? true : undefined}
              aria-describedby={`${base}-title-count`}
              onChange={(event) => setDraft(key, { title: event.target.value })}
            />
            <Counter id={`${base}-title-count`} used={titleUsed} limit={rules.titleLimit} label={`${rules.label} title`} />
          </Field>
        ) : null}
        {key === 'reddit' ? (
          <Field
            label="Subreddit"
            htmlFor={`${base}-subreddit`}
            required
            error={errors.subreddit}
            hint="The community name, for example Marketing (3 to 21 letters, numbers or underscores)."
          >
            <input
              id={`${base}-subreddit`}
              className="input"
              type="text"
              value={draft.subreddit}
              maxLength={24}
              required
              autoComplete="off"
              spellCheck={false}
              placeholder="r/"
              aria-invalid={errors.subreddit ? true : undefined}
              onChange={(event) => setDraft(key, { subreddit: event.target.value })}
            />
          </Field>
        ) : null}
        <Field label={rules.bodyLabel} htmlFor={`${base}-caption`} required error={errors.caption}>
          <textarea
            id={`${base}-caption`}
            className="input"
            rows={key === 'x' ? 3 : 5}
            value={draft.caption}
            maxLength={MAX_CAPTION_CHARS}
            required
            aria-invalid={errors.caption ? true : undefined}
            aria-describedby={`${base}-caption-count`}
            onChange={(event) => setDraft(key, { caption: event.target.value })}
          />
          <Counter id={`${base}-caption-count`} used={used} limit={rules.limit} label={`${rules.label} ${rules.bodyLabel.toLowerCase()}`} />
          {rules.maxHashtags ? (
            <span className="cs-note">
              {' '}
              · {hashtagCount(draft.caption)} / {rules.maxHashtags} hashtags
            </span>
          ) : null}
          {key === 'x' ? <span className="cs-note"> · Each link counts as 23 characters.</span> : null}
        </Field>
        {rules.takesLink ? (
          <Field
            label={`Link${rules.needsLink ? '' : ' (optional)'}`}
            htmlFor={`${base}-link`}
            required={rules.needsLink}
            error={errors.link}
            hint={rules.needsLink ? 'Where people go when they tap the Pin.' : undefined}
          >
            <input
              id={`${base}-link`}
              className="input"
              type="url"
              inputMode="url"
              value={draft.link}
              maxLength={MAX_LINK_CHARS}
              required={rules.needsLink}
              placeholder="https://"
              aria-invalid={errors.link ? true : undefined}
              onChange={(event) => setDraft(key, { link: event.target.value })}
            />
          </Field>
        ) : null}
        {key === 'reddit' ? (
          <p className="cs-callout is-warning">
            <strong>Heads up:</strong> each subreddit has its own rules. Reddit or the community’s moderators may remove
            posts that break them, so check the rules before you post.
          </p>
        ) : null}
        {key === 'instagram' ? <p className="cs-note">Links in Instagram captions can’t be tapped, so there is no link field.</p> : null}
      </fieldset>
    )
  })

  const whenPicker = (
    <fieldset className="cs-fieldset">
      <legend className="cs-legend">When?</legend>
      <div className="cs-chip-row">
        <label className={`cs-chip${mode === 'now' ? ' is-checked' : ''}`}>
          <input type="radio" name="share-when" checked={mode === 'now'} disabled={!canWrite || busy} onChange={() => setMode('now')} />
          Post now
        </label>
        <label className={`cs-chip${mode === 'schedule' ? ' is-checked' : ''}`}>
          <input
            type="radio"
            name="share-when"
            checked={mode === 'schedule'}
            disabled={!canWrite || busy || zone === null}
            onChange={() => setMode('schedule')}
          />
          Schedule
        </label>
      </div>
      {zoneProblem ? <p className="cs-note">{zoneProblem}</p> : null}
      {mode === 'schedule' && zone ? (
        <>
          <p className="cs-note" id="share-zone">
            Times are in your workspace time zone: <strong>{zoneText}</strong>.
          </p>
          <div className="cs-chip-row">
            <Field label={`Date (${zoneText})`} htmlFor="share-date" required error={showErrors && !date ? 'Choose a date.' : undefined}>
              <input
                id="share-date"
                className="input"
                type="date"
                value={date}
                min={today || undefined}
                required
                aria-describedby="share-zone"
                onChange={(event) => setDate(event.target.value)}
              />
            </Field>
            <Field label={`Time (${zoneText})`} htmlFor="share-time" required error={showErrors && !time ? 'Choose a time.' : undefined}>
              <input
                id="share-time"
                className="input"
                type="time"
                value={time}
                step={60}
                required
                aria-describedby="share-zone"
                onChange={(event) => setTime(event.target.value)}
              />
            </Field>
          </div>
          {showErrors && date && time && timeProblem ? (
            <p className="cs-callout is-danger" role="alert">
              {timeProblem}
            </p>
          ) : null}
        </>
      ) : null}
    </fieldset>
  )

  // ── Review ──
  const summary = (
    <div>
      <p>
        Please check before you {mode === 'now' ? 'post' : 'schedule'}.{' '}
        {mode === 'now' ? (
          <strong>This posts straight away.</strong>
        ) : (
          <>
            Goes out on <strong>{formatLocal(whenLocal)}</strong> ({zoneText}).
          </>
        )}
      </p>
      <ul style={{ paddingLeft: 18 }}>
        {selected.map((key) => {
          const rules = NETWORK_BY_KEY.get(key) as NetworkRules
          const draft = draftFor(key)
          const used = bodyLength(key, draft.caption, rules.takesLink ? draft.link : '')
          return (
            <li key={key}>
              <strong>{rules.label}</strong>
              {key === 'reddit' ? ` in r/${cleanSubreddit(draft.subreddit)}` : ''}
              {rules.titleLimit && draft.title.trim() ? ` · ${rules.titleLabel}: “${draft.title.trim()}”` : ''}
              {` · ${used.toLocaleString()} of ${rules.limit.toLocaleString()} characters`}
              {rules.takesLink && draft.link.trim() ? ` · links to ${draft.link.trim()}` : ''}
            </li>
          )
        })}
      </ul>
    </div>
  )

  const actions =
    step === 'review' ? (
      <>
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => setStep('compose')}>
          Back to edit
        </button>
        <button type="button" className="btn btn-primary" disabled={busy} aria-busy={busy || undefined} onClick={() => void send()}>
          {busy ? 'Sending…' : mode === 'now' ? `Post now to ${selected.length} network${selected.length === 1 ? '' : 's'}` : 'Confirm schedule'}
        </button>
      </>
    ) : (
      <>
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Close
        </button>
        {canWrite ? (
          <button type="button" className="btn btn-primary" disabled={busy} onClick={review}>
            Review and {mode === 'now' ? 'post' : 'schedule'}
          </button>
        ) : null}
      </>
    )

  return (
    <Dialog title={`Share or schedule: ${title}`} width={760} onClose={onClose} actions={actions}>
      {step === 'review' ? (
        summary
      ) : (
        <>
          {canWrite ? (
            <>
              {networkPicker}
              {editors}
              {whenPicker}
            </>
          ) : (
            <p className="cs-note">You can see where this design was shared. Posting needs edit access to Content.</p>
          )}
        </>
      )}
      {formError ? (
        <p className="cs-callout is-danger" role="alert">
          {formError}
        </p>
      ) : null}

      <section aria-labelledby="share-status-heading">
        <h3 id="share-status-heading" className="cs-subhead">
          Posts of this design
        </h3>
        {postsLoading ? (
          <p className="cs-note">Loading…</p>
        ) : postsError ? (
          <p className="cs-callout is-danger">
            {postsError}{' '}
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadPosts()}>
              Try again
            </button>
          </p>
        ) : posts.length === 0 ? (
          <p className="cs-note">Not shared yet.</p>
        ) : (
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }} aria-live="polite">
            {posts.map((post) => (
              <PostRow
                key={post.id}
                post={post}
                canCancel={canWrite}
                busy={cancelling === post.id}
                onCancel={() => void cancelPost(post)}
              />
            ))}
          </ul>
        )}
      </section>
    </Dialog>
  )
}
