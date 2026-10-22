/**
 * Social sharing API (`/social/*`): post an approved design now or later.
 *
 * - `POST /social/schedule`: one design on several networks, now or at a local time.
 * - `GET  /social/posts?creative=`: their status (scheduled, posted, failed, cancelled).
 * - `POST /social/posts/{id}/cancel`: cancel a post that is still scheduled.
 *
 * The per-network limits below mirror the server's rules
 * (`app/agents/social_media_manager/platforms.py`). They only guide the person
 * while typing; the server checks every post again and has the final word.
 *
 * Replies are read through small parsers: unknown fields are dropped, ids are
 * checked before they become part of a request path, and a post's link is
 * kept only when it is a plain https address.
 */
import { isId } from './brandKit'
import { request } from './client'
import type { Toast } from './types'

// ── Networks ───────────────────────────────────────────────────────────────
export type NetworkKey = 'facebook' | 'instagram' | 'linkedin' | 'x' | 'pinterest' | 'reddit'

export interface NetworkRules {
  key: NetworkKey
  label: string
  /** The integration (connector slug) that publishes it. */
  connector: string
  /** Characters allowed in the post body (Pinterest description, Reddit post text). */
  limit: number
  /** What the body is called on this network. */
  bodyLabel: string
  /** Most hashtags allowed; 0 when the server sets no separate count. */
  maxHashtags: number
  /** Characters allowed in the title; 0 when there is no title. */
  titleLimit: number
  titleLabel: string
  needsTitle: boolean
  /** A link field is offered; `needsLink` makes it required. */
  takesLink: boolean
  needsLink: boolean
}

export const NETWORKS: readonly NetworkRules[] = [
  { key: 'instagram', label: 'Instagram', connector: 'instagram_business', limit: 2200, bodyLabel: 'Caption', maxHashtags: 30, titleLimit: 0, titleLabel: '', needsTitle: false, takesLink: false, needsLink: false },
  { key: 'linkedin', label: 'LinkedIn', connector: 'linkedin_pages', limit: 3000, bodyLabel: 'Post text', maxHashtags: 0, titleLimit: 0, titleLabel: '', needsTitle: false, takesLink: true, needsLink: false },
  { key: 'x', label: 'X', connector: 'x_twitter', limit: 280, bodyLabel: 'Post text', maxHashtags: 0, titleLimit: 0, titleLabel: '', needsTitle: false, takesLink: true, needsLink: false },
  { key: 'facebook', label: 'Facebook', connector: 'facebook_pages', limit: 63206, bodyLabel: 'Post text', maxHashtags: 0, titleLimit: 0, titleLabel: '', needsTitle: false, takesLink: true, needsLink: false },
  { key: 'pinterest', label: 'Pinterest', connector: 'pinterest', limit: 800, bodyLabel: 'Description', maxHashtags: 0, titleLimit: 100, titleLabel: 'Pin title', needsTitle: false, takesLink: true, needsLink: true },
  { key: 'reddit', label: 'Reddit', connector: 'reddit', limit: 40000, bodyLabel: 'Post text', maxHashtags: 0, titleLimit: 300, titleLabel: 'Post title', needsTitle: true, takesLink: true, needsLink: false },
]

export const NETWORK_BY_KEY: ReadonlyMap<NetworkKey, NetworkRules> = new Map(NETWORKS.map((n) => [n.key, n]))

/** Server caps (`social_schedule.py`). */
export const MAX_CAPTION_CHARS = 40000
export const MAX_LINK_CHARS = 2000
export const MAX_TITLE_CHARS = 300
export const MAX_ITEMS = 12

/** Reddit community names: 3 to 21 letters, numbers or underscores. */
export const SUBREDDIT_PATTERN = /^[A-Za-z0-9_]{3,21}$/

/** "r/Marketing" or "/r/Marketing/" typed by habit, reduced to the name. */
export function cleanSubreddit(raw: string): string {
  return raw.trim().replace(/^\/?r\//i, '').replace(/\/+$/, '')
}

// ── Length, as each network counts it ──────────────────────────────────────
/** X counts every link as this many characters, whatever its length. */
const X_URL_LENGTH = 23
const URL_IN_TEXT = /https?:\/\/\S+/gi

function weighted(value: string): number {
  let count = 0
  for (const ch of value) {
    const point = ch.codePointAt(0) ?? 0
    const light =
      point <= 4351 || (point >= 8192 && point <= 8205) || (point >= 8208 && point <= 8223) || (point >= 8242 && point <= 8247)
    count += light ? 1 : 2
  }
  return count
}

/** Length as X counts it: links are 23; most non-Latin characters and emoji count twice. */
export function xLength(value: string): number {
  let total = 0
  let position = 0
  for (const match of value.matchAll(URL_IN_TEXT)) {
    const start = match.index ?? 0
    total += weighted(value.slice(position, start)) + X_URL_LENGTH
    position = start + match[0].length
  }
  return total + weighted(value.slice(position))
}

/** Python's len(): code points, not UTF-16 units. */
function codePoints(value: string): number {
  let count = 0
  for (const _ of value) count++
  return count
}

/** The body as the server sends it (the link is added where the network shows it in the text). */
export function composeBody(network: NetworkKey, caption: string, link: string): string {
  const text = caption.trim()
  const url = link.trim()
  if (network === 'x') return [text, url && !text.includes(url) ? url : ''].filter(Boolean).join(' ')
  if (network === 'linkedin' && url && !text.includes(url)) return [text, url].filter(Boolean).join('\n\n')
  return text
}

export function bodyLength(network: NetworkKey, caption: string, link: string): number {
  const body = composeBody(network, caption, link)
  return network === 'x' ? xLength(body) : codePoints(body)
}

const INLINE_HASHTAG = /(^|[^\p{L}\p{N}_#])#[\p{L}\p{N}_]+/gu

export function hashtagCount(caption: string): number {
  return Array.from(caption.matchAll(INLINE_HASHTAG)).length
}

/** A plain http(s) address with a host and no spaces or sign-in details. */
export function isWebLink(value: string, httpsOnly = false): boolean {
  const text = value.trim()
  if (!text || /\s/.test(text) || text.length > MAX_LINK_CHARS) return false
  try {
    const url = new URL(text)
    const schemes = httpsOnly ? ['https:'] : ['https:', 'http:']
    return schemes.includes(url.protocol) && url.hostname !== '' && url.username === '' && url.password === ''
  } catch {
    return false
  }
}

// ── Drafts ─────────────────────────────────────────────────────────────────
export interface PostDraft {
  caption: string
  title: string
  link: string
  subreddit: string
}

export const EMPTY_DRAFT: PostDraft = { caption: '', title: '', link: '', subreddit: '' }

/** What is wrong with one network's draft, per field, in plain words. Empty when it can be sent. */
export function draftProblems(network: NetworkKey, draft: PostDraft): Partial<Record<keyof PostDraft, string>> {
  const rules = NETWORK_BY_KEY.get(network)
  if (!rules) return { caption: 'Choose a network this design can be posted to.' }
  const out: Partial<Record<keyof PostDraft, string>> = {}
  const caption = draft.caption.trim()
  const link = rules.takesLink ? draft.link.trim() : ''
  if (!caption) out.caption = `Write the ${rules.bodyLabel.toLowerCase()} for ${rules.label}.`
  else if (codePoints(caption) > MAX_CAPTION_CHARS) out.caption = `This is too long. Keep it under ${MAX_CAPTION_CHARS.toLocaleString()} characters.`
  else {
    const size = bodyLength(network, draft.caption, link)
    if (size > rules.limit) {
      out.caption =
        `${rules.label} allows ${rules.limit.toLocaleString()} characters; this is ${size.toLocaleString()}` +
        (network === 'x' ? ' as X counts them (each link counts as 23)' : '') +
        '. Shorten it.'
    } else if (rules.maxHashtags && hashtagCount(caption) > rules.maxHashtags) {
      out.caption = `${rules.label} allows at most ${rules.maxHashtags} hashtags; this has ${hashtagCount(caption)}.`
    }
  }
  if (rules.titleLimit) {
    const title = draft.title.split(/\s+/).filter(Boolean).join(' ')
    if (rules.needsTitle && !title) out.title = `A ${rules.label} post needs a title.`
    else if (codePoints(title) > rules.titleLimit) out.title = `${rules.label} allows ${rules.titleLimit} characters in the title; this is ${codePoints(title)}.`
  }
  if (rules.takesLink) {
    if (!link && rules.needsLink) out.link = `A ${rules.label} post needs a link to your website.`
    else if (link && !isWebLink(link)) out.link = 'Enter a web address starting with https://.'
  }
  if (network === 'reddit') {
    const name = cleanSubreddit(draft.subreddit)
    if (!name) out.subreddit = 'Enter the subreddit to post to.'
    else if (!SUBREDDIT_PATTERN.test(name)) out.subreddit = 'A subreddit name is 3 to 21 letters, numbers or underscores.'
  }
  return out
}

// ── Workspace time ─────────────────────────────────────────────────────────
/** `YYYY-MM-DDTHH:MM` wall-clock time in `zone` at `at`, or '' if the browser cannot work it out. */
export function localInZone(zone: string, at: Date = new Date()): string {
  try {
    const parts = new Intl.DateTimeFormat('en-GB', {
      timeZone: zone || 'UTC',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(at)
    const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? ''
    const value = `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`
    return WHEN_LOCAL.test(value) ? value : ''
  } catch {
    return ''
  }
}

export const WHEN_LOCAL = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/

/** How soon and how far ahead a post may be scheduled (the server's rule). */
const MIN_LEAD_MS = 60_000
const MAX_LEAD_MS = 365 * 24 * 60 * 60 * 1000

/** Why this local time can't be used, or ''. Wall-clock strings compare correctly as text. */
export function scheduleProblem(whenLocal: string, zone: string, now: Date = new Date()): string {
  if (!WHEN_LOCAL.test(whenLocal)) return 'Choose a date and a time.'
  const earliest = localInZone(zone, new Date(now.getTime() + MIN_LEAD_MS))
  const latest = localInZone(zone, new Date(now.getTime() + MAX_LEAD_MS))
  if (!earliest || !latest) return 'We could not work out the time in your workspace time zone. Choose Post now instead.'
  if (whenLocal < earliest) return 'That time has already passed. Choose a time at least a minute from now, or post now.'
  if (whenLocal > latest) return 'Posts can be scheduled up to a year ahead. Choose an earlier date.'
  return ''
}

/** "Tue 3 Nov 2026, 14:30" for a `YYYY-MM-DDTHH:MM` wall-clock time, without shifting it. */
export function formatLocal(whenLocal: string): string {
  if (!WHEN_LOCAL.test(whenLocal)) return ''
  const [datePart = '', timePart = ''] = whenLocal.split('T')
  const [y, m, d] = datePart.split('-').map(Number)
  const [hh, mm] = timePart.split(':').map(Number)
  const date = new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1, hh ?? 0, mm ?? 0))
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: 'UTC',
      weekday: 'short',
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(date)
  } catch {
    return whenLocal.replace('T', ' ')
  }
}

// ── Posts ──────────────────────────────────────────────────────────────────
export type PostStatus = 'scheduled' | 'publishing' | 'published' | 'failed' | 'rejected'

export interface SocialPost {
  id: string
  creative_asset_id: string
  network: string
  network_label: string
  subreddit: string
  status: PostStatus | string
  status_label: string
  scheduled_for: string | null
  /** `YYYY-MM-DDTHH:MM` in the workspace time zone. */
  scheduled_local: string
  timezone: string
  published_at: string | null
  /** The post on the platform: https only, '' otherwise. */
  link: string
  reason: string
  note: string
  can_cancel: boolean
}

export interface ScheduleItemInput {
  network: NetworkKey
  caption: string
  hashtags: string[]
  link: string
  title: string
  subreddit: string | null
  /** `YYYY-MM-DDTHH:MM` in the workspace time zone; null posts now. */
  when_local: string | null
}

export interface ScheduleInput {
  creative_asset_id: string
  items: ScheduleItemInput[]
}

export interface ScheduleResult {
  posts: SocialPost[]
  toast: Toast | null
}

type Obj = Record<string, unknown>

function obj(value: unknown): Obj {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Obj) : {}
}

function str(value: unknown, max = 500): string {
  return typeof value === 'string' ? value.slice(0, max) : ''
}

function parseToast(value: unknown): Toast | null {
  const raw = obj(value)
  const message = str(raw.message, 400)
  if (!message) return null
  const kinds: readonly Toast['kind'][] = ['info', 'success', 'warning', 'error']
  const kind = kinds.find((k) => k === raw.kind) ?? 'info'
  return { message, kind }
}

/** A post from the server, or null when it is not a usable record. */
export function parsePost(value: unknown): SocialPost | null {
  const raw = obj(value)
  if (typeof raw.id !== 'string' || !isId(raw.id)) return null
  const link = str(raw.link, 2000)
  const creative = str(raw.creative_asset_id, 64)
  const scheduledLocal = str(raw.scheduled_local, 16)
  return {
    id: raw.id,
    creative_asset_id: isId(creative) ? creative.toLowerCase() : '',
    network: str(raw.network, 24),
    network_label: str(raw.network_label, 40) || str(raw.network, 24),
    subreddit: str(raw.subreddit, 24),
    status: str(raw.status, 24),
    status_label: str(raw.status_label, 40),
    scheduled_for: typeof raw.scheduled_for === 'string' ? raw.scheduled_for.slice(0, 40) : null,
    scheduled_local: WHEN_LOCAL.test(scheduledLocal) ? scheduledLocal : '',
    timezone: str(raw.timezone, 64) || 'UTC',
    published_at: typeof raw.published_at === 'string' ? raw.published_at.slice(0, 40) : null,
    // Only a plain https address ever becomes a clickable link.
    link: isWebLink(link, true) ? link : '',
    reason: str(raw.reason, 500),
    note: str(raw.note, 500),
    can_cancel: raw.can_cancel === true,
  }
}

function posts(value: unknown): SocialPost[] {
  return (Array.isArray(value) ? value : []).map(parsePost).filter((p): p is SocialPost => p !== null)
}

function idPath(value: string): string {
  if (!isId(value)) throw new Error('Invalid id')
  return encodeURIComponent(value)
}

// ── Endpoints ──────────────────────────────────────────────────────────────
export const socialApi = {
  schedule: async (body: ScheduleInput): Promise<ScheduleResult> => {
    if (!isId(body.creative_asset_id)) throw new Error('Invalid id')
    const reply = obj(await request<unknown>('/social/schedule', { method: 'POST', body }))
    return { posts: posts(reply.posts), toast: parseToast(reply.toast) }
  },

  /** One design's posts, or every shared design's posts (newest first, at most 100). */
  posts: async (creativeId?: string, signal?: AbortSignal): Promise<SocialPost[]> => {
    const query = creativeId ? `?creative=${idPath(creativeId)}` : ''
    return posts(await request<unknown>(`/social/posts${query}`, { signal }))
  },

  cancel: async (postId: string): Promise<{ post: SocialPost | null; toast: Toast | null }> => {
    const reply = obj(await request<unknown>(`/social/posts/${idPath(postId)}/cancel`, { method: 'POST' }))
    return { post: parsePost(reply.post), toast: parseToast(reply.toast) }
  },
}
