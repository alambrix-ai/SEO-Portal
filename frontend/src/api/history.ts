/**
 * Version history API (`/history/*`).
 *
 * Types mirror `backend/app/api/v1/history.py` and `app/services/history`
 * (step 4.7): numbered versions of analyses and settings, any two compared in
 * plain words, restore for settings, and how long versions are kept.
 *
 * Everything goes through the shared client in `./client`, so auth, token
 * refresh and the `{code, detail, fields}` error shape behave as on every
 * other screen. Kinds, ids, subjects and cursors are checked against the
 * server's own patterns before they reach a URL, so a bad value can never
 * reshape the request, and every call gives up after a fixed time.
 */
import { ApiError, request } from './client'
import type { ModuleKey } from './types'

// ── Kinds ───────────────────────────────────────────────────────────────────
/** Every kind the history API serves (`history.KINDS`). */
export const VERSION_KINDS = [
  'technical_seo',
  'seo_scores',
  'local_ranks',
  'site_analysis',
  'backlink_scan',
  'competitor_scan',
  'brand_profile',
  'brand_kit',
  'product',
  'agent_config',
  'workspace_policy',
  'creative_text',
] as const

export type VersionKind = (typeof VERSION_KINDS)[number]

/** Kinds whose versions are kept in the history table, so retention applies (`RETAINED_KINDS`). */
export const RETAINED_KINDS: readonly VersionKind[] = VERSION_KINDS.filter((k) => k !== 'local_ranks')

/** Who may read each kind; restoring needs write access to the same module (`KIND_MODULE`). */
export const KIND_MODULE: Record<VersionKind, ModuleKey> = {
  technical_seo: 'seo',
  seo_scores: 'seo',
  local_ranks: 'seo',
  site_analysis: 'seo',
  backlink_scan: 'offpage',
  competitor_scan: 'offpage',
  brand_profile: 'content',
  brand_kit: 'brand_kit',
  product: 'brand_kit',
  agent_config: 'agents',
  workspace_policy: 'admin',
  creative_text: 'content',
}

/** The plain name of a kind, for headings and confirmations ("the brand kit"). */
export const KIND_NAMES: Record<VersionKind, string> = {
  technical_seo: 'technical SEO audit',
  seo_scores: 'SEO and AEO page scores',
  local_ranks: 'local rankings',
  site_analysis: 'site analysis',
  backlink_scan: 'backlink scan',
  competitor_scan: 'competitor scan',
  brand_profile: 'brand profile',
  brand_kit: 'brand kit',
  product: 'product',
  agent_config: 'agent settings',
  workspace_policy: 'workspace policy',
  creative_text: 'creative text',
}

export function isVersionKind(value: string): value is VersionKind {
  return (VERSION_KINDS as readonly string[]).includes(value)
}

// ── Limits (mirror the server; the server still decides) ───────────────────
/** `GET /history/{kind}` `limit` bounds. */
export const HISTORY_PAGE_MAX = 100
export const HISTORY_PAGE_DEFAULT = 30
/** `store.CURSOR_MAX_LENGTH`. */
const CURSOR_MAX_LENGTH = 200
/** `_SUBJECT_PATTERN` and `_ENTRY_PATTERN` in the router. */
const SUBJECT_PATTERN = /^[A-Za-z0-9._:-]{1,64}$/
const ENTRY_PATTERN = /^[A-Za-z0-9-]{1,64}$/
/** Most restore notes shown, and the longest one. */
const NOTES_MAX = 20
const NOTE_MAX_CHARS = 300
/** How long one history call may take before we give up. */
export const HISTORY_TIMEOUT_MS = 20_000

const TIMEOUT_MESSAGE = 'The server took too long to answer. Please try again.'
const BAD_INPUT_MESSAGE = 'That version could not be found. Please reload the list and try again.'

// ── Shapes ──────────────────────────────────────────────────────────────────
/** Who made a version: a person, an agent, or the system itself. */
export type VersionActor = 'user' | 'agent' | 'system'

/** One version in a list (`store.base_entry` plus the kind's extras). */
export interface VersionEntry {
  id: string
  /** 1, 2, 3… per item; null when the server could not number it. */
  version: number | null
  subject_id?: string
  created_at: string | null
  source: string
  /** The short summary ("Brand kit saved", "Restored version 3", "Scheduled run"). */
  label: string
  created_by: string | null
  actor_type?: VersionActor | string | null
  agent_slug?: string | null
  model?: string | null
  run_id?: string | null
  truncated?: boolean
  headline?: number | null
  change?: number | null
  /** Settings kinds: the fields that differ from the version before; null for the first. */
  changed_fields?: string[] | null
  is_current?: boolean
}

export interface VersionPage {
  kind: VersionKind
  subject_id?: string | null
  restorable: boolean
  entries: VersionEntry[]
  /** Pass back as `cursor` for the next, older page; null on the last page. */
  next_cursor: string | null
  started_at: string | null
  baseline_recorded?: boolean
  limit?: number
}

/** One side of a comparison. */
export interface VersionRef {
  id: string
  version: number | null
  created_at: string | null
  label: string
}

/** One thing that differs: short plain strings, any of which may be missing. */
export interface ChangeRow {
  label: string
  where: string | null
  before: string | null
  after: string | null
}

/** A headline number before and after, and whether the move is good news. */
export interface Measure {
  label: string
  before: number | null
  after: number | null
  change: number | null
  better: 'higher' | 'lower' | null
  verdict: 'better' | 'worse' | 'same' | null
}

export interface ChangeCounts {
  new: number
  fixed: number
  worse: number
  changed: number
  unchanged: number
}

/** The shared comparison shape every kind compares into (`store.comparison`). */
export interface Comparison {
  counts: ChangeCounts
  new: ChangeRow[]
  fixed: ChangeRow[]
  worse: ChangeRow[]
  changed: ChangeRow[]
  measures: Measure[]
  /** One plain sentence, e.g. "2 new issues, 5 fixed and 1 got worse; 40 unchanged.". */
  summary: string
  truncated: boolean
}

/** `GET /history/{kind}/compare`: always from the earlier version to the later. */
export interface CompareResult extends Comparison {
  kind: VersionKind
  subject_id: string
  before: VersionRef
  after: VersionRef
}

/** One field of a settings version, already in words. */
export interface FieldView {
  field: string
  label: string
  value: string
}

/** `GET /history/{kind}/{id}`. */
export interface VersionDetail extends VersionEntry {
  kind: VersionKind
  restorable: boolean
  /** Compared with the version before it; null for the first version kept. */
  changes: (Comparison & { previous: VersionRef }) | null
  /** Settings kinds: every field of this version, in words. */
  fields?: FieldView[]
  /** Site analysis: the website the version is about. */
  website?: string | null
}

/** `POST /history/{kind}/{id}/restore`. */
export interface RestoreResult {
  kind: VersionKind
  subject_id: string
  restored_version: number
  entry: VersionEntry | null
  /** Plain notes about the restore, e.g. "These files no longer exist and were left out". */
  notes?: string[]
}

export interface RetentionKind {
  kind: VersionKind
  name: string
  keep_versions: number
  /** 0 keeps versions for ever. */
  keep_days: number
  /** True when the workspace chose this rather than using the default. */
  custom: boolean
}

export interface RetentionView {
  min_versions_kept: number
  defaults: { keep_versions: number; keep_days: number }
  limits: { keep_versions: number; keep_days: number }
  kinds: RetentionKind[]
}

/** `null` for a value goes back to the workspace default. */
export interface RetentionChange {
  keep_versions: number | null
  keep_days: number | null
}

// ── Checks ──────────────────────────────────────────────────────────────────
function kindPath(kind: string): string {
  if (!isVersionKind(kind)) throw new ApiError(400, 'invalid_kind', BAD_INPUT_MESSAGE)
  return `/history/${kind}`
}

function entryId(id: string): string {
  if (!ENTRY_PATTERN.test(id)) throw new ApiError(400, 'invalid_id', BAD_INPUT_MESSAGE)
  return encodeURIComponent(id)
}

/**
 * Run one request with a time limit, still honouring the caller's own abort
 * (a screen that unmounts). A timeout surfaces as a plain sentence, not as an
 * abort, so the screen shows it instead of staying blank.
 */
async function timed<T>(call: (signal: AbortSignal) => Promise<T>, outer?: AbortSignal): Promise<T> {
  const controller = new AbortController()
  let timedOut = false
  const timer = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, HISTORY_TIMEOUT_MS)
  const forward = () => controller.abort()
  if (outer?.aborted) controller.abort()
  else outer?.addEventListener('abort', forward, { once: true })
  try {
    return await call(controller.signal)
  } catch (caught) {
    if (timedOut) throw new ApiError(408, 'timeout', TIMEOUT_MESSAGE)
    throw caught
  } finally {
    window.clearTimeout(timer)
    outer?.removeEventListener('abort', forward)
  }
}

/** Notes are server words shown as text; keep a sane number of short strings. */
function cleanNotes(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .filter((n): n is string => typeof n === 'string' && n.trim() !== '')
    .slice(0, NOTES_MAX)
    .map((n) => (n.length > NOTE_MAX_CHARS ? `${n.slice(0, NOTE_MAX_CHARS - 1)}…` : n))
}

/** True when the server refused a restore until it is confirmed (workspace policy). */
export function needsConfirmation(caught: unknown): boolean {
  if (!(caught instanceof ApiError) || caught.status !== 422) return false
  return (caught.fields as Record<string, unknown>).needs_confirmation === true
}

/** Check one kind's retention change against the server's limits; a plain message, or null. */
export function retentionProblem(change: RetentionChange, view: Pick<RetentionView, 'min_versions_kept' | 'limits'>): string | null {
  const { keep_versions: versions, keep_days: days } = change
  if (versions !== null) {
    if (!Number.isInteger(versions)) return 'Enter a whole number of versions.'
    if (versions < view.min_versions_kept) return `Keep at least ${view.min_versions_kept} versions.`
    if (versions > view.limits.keep_versions) return `Keep at most ${view.limits.keep_versions.toLocaleString()} versions.`
  }
  if (days !== null) {
    if (!Number.isInteger(days)) return 'Enter a whole number of days.'
    if (days < 0) return 'Enter 0 or more days.'
    if (days > view.limits.keep_days) return `Keep versions for at most ${view.limits.keep_days.toLocaleString()} days.`
  }
  return null
}

// ── Calls ───────────────────────────────────────────────────────────────────
export const historyApi = {
  /** One page of versions, newest first. `subject` picks one item (a kit, product, agent or site). */
  list: (
    kind: VersionKind,
    options: { subject?: string | null; cursor?: string | null; limit?: number } = {},
    signal?: AbortSignal,
  ): Promise<VersionPage> => {
    const query = new URLSearchParams()
    const limit = Math.trunc(options.limit ?? HISTORY_PAGE_DEFAULT)
    query.set('limit', String(Math.min(Math.max(Number.isFinite(limit) ? limit : HISTORY_PAGE_DEFAULT, 1), HISTORY_PAGE_MAX)))
    if (options.subject) {
      if (!SUBJECT_PATTERN.test(options.subject)) return Promise.reject(new ApiError(400, 'invalid_subject', BAD_INPUT_MESSAGE))
      query.set('subject', options.subject)
    }
    if (options.cursor) {
      if (options.cursor.length > CURSOR_MAX_LENGTH)
        return Promise.reject(new ApiError(400, 'invalid_cursor', 'That page of history is no longer available. Please reload the list.'))
      query.set('cursor', options.cursor)
    }
    return timed((s) => request<VersionPage>(`${kindPath(kind)}?${query.toString()}`, { signal: s }), signal)
  },

  /** Any two versions of the same item; the server orders them earlier → later. */
  compare: (kind: VersionKind, a: string, b: string, signal?: AbortSignal): Promise<CompareResult> =>
    timed((s) => {
      if (!ENTRY_PATTERN.test(a) || !ENTRY_PATTERN.test(b)) throw new ApiError(400, 'invalid_id', BAD_INPUT_MESSAGE)
      const query = new URLSearchParams({ a, b })
      return request<CompareResult>(`${kindPath(kind)}/compare?${query.toString()}`, { signal: s })
    }, signal),

  /** One version with what changed since the one before it. */
  detail: (kind: VersionKind, id: string, signal?: AbortSignal): Promise<VersionDetail> =>
    timed((s) => request<VersionDetail>(`${kindPath(kind)}/${entryId(id)}`, { signal: s }), signal),

  /**
   * Make an old version current again (recorded as a new version). The
   * workspace policy needs `confirm` to go back to "Publish automatically";
   * without it the server answers 422 (see `needsConfirmation`).
   */
  restore: async (kind: VersionKind, id: string, options: { confirm?: boolean } = {}): Promise<RestoreResult> => {
    const result = await timed(
      (s) =>
        request<RestoreResult>(`${kindPath(kind)}/${entryId(id)}/restore`, {
          method: 'POST',
          body: { confirm: options.confirm === true },
          signal: s,
        }),
    )
    return { ...result, notes: cleanNotes(result?.notes) }
  },

  retention: (signal?: AbortSignal): Promise<RetentionView> =>
    timed((s) => request<RetentionView>('/history/retention', { signal: s }), signal),

  setRetention: (kinds: Partial<Record<VersionKind, RetentionChange>>): Promise<RetentionView> => {
    const body: Record<string, RetentionChange> = {}
    for (const [kind, change] of Object.entries(kinds)) {
      if (!change || !(RETAINED_KINDS as readonly string[]).includes(kind)) continue
      body[kind] = { keep_versions: change.keep_versions, keep_days: change.keep_days }
    }
    return timed((s) => request<RetentionView>('/history/retention', { method: 'PUT', body: { kinds: body }, signal: s }))
  },
}

// ── Display helpers ─────────────────────────────────────────────────────────
function humanise(slug: string): string {
  const words = slug.replace(/[_-]+/g, ' ').trim()
  if (!words) return ''
  return words
    .split(/\s+/)
    .map((w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(' ')
}

/** Words an agent name spells in capitals ("Technical SEO Auditor"). */
const ACRONYMS = new Set(['seo', 'aeo', 'ai', 'llm', 'ppc', 'url'])

/**
 * Who made a version, in words: "Priya Shah", "Technical Seo Auditor agent
 * (gpt-4o)", "Automatic". The kind of actor is always part of the text, so it
 * does not rely on an icon or colour.
 */
export function describeActor(entry: Pick<VersionEntry, 'actor_type' | 'created_by' | 'agent_slug' | 'model' | 'source'>): string {
  const actor = entry.actor_type || (entry.agent_slug ? 'agent' : entry.created_by ? 'user' : 'system')
  const model = entry.model ? ` (${entry.model})` : ''
  if (actor === 'agent') {
    const name = entry.agent_slug ? `${humanise(entry.agent_slug)} agent` : 'An agent'
    const started = entry.created_by ? `, started by ${entry.created_by}` : ''
    return `${name}${model}${started}`
  }
  if (actor === 'user') return `${entry.created_by || 'A team member'}${model}`
  return entry.source === 'baseline' ? 'Automatic (history started)' : `Automatic${model}`
}

/** "Version 4", or the summary label when the server could not number it. */
export function versionName(entry: Pick<VersionEntry, 'version' | 'label'>): string {
  return entry.version ? `Version ${entry.version}` : entry.label || 'Version'
}

/** A headline number in words; a missing one is said, never shown as 0. */
export function measureValue(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'Not recorded'
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 })
}
