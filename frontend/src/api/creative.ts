/**
 * Creative Studio API (`/creative/*`).
 *
 * Routes and shapes follow the creative API contract (plan step 6.1):
 *
 * - `GET  /creative/options`: formats, model picker, quota and limits.
 * - `POST /creative/jobs`: start a design job (202).
 * - `GET  /creative/jobs` and `GET /creative/jobs/{id}`: job status and outputs.
 * - `GET  /creative/assets/{asset_id}/download?format=png|pdf|zip`: approved work only.
 * - `GET  /creative/jobs/{id}/download?format=zip`: every approved design of a job, in one ZIP.
 *
 * Kits, products and photos come from `brandKitApi` (`./brandKit`), and the
 * review actions (approve, ask for changes, edit text) from `api` in `./client`.
 *
 * Every reply is read through a small parser: unknown fields are dropped,
 * wrong types fall back to safe defaults, and ids are checked with `isId`
 * before they can become part of a request path.
 */
import { isId } from './brandKit'
import { request, requestBlob } from './client'

// The download-name guard lives with `requestBlob`, which applies it to every
// download; re-exported so existing imports from this module keep working.
export { safeFilename } from './client'

// ── Types ──────────────────────────────────────────────────────────────────
export type FormatKind = 'banner' | 'social' | 'email' | 'print'

export interface CopyLimits {
  headline_chars: number
  /** 0 when the layout has no room for body text. */
  body_chars: number
  cta_chars: number
  headline_words: number | null
}

/** One size the studio can make (`app/services/creative/formats.py`). */
export interface CreativeFormat {
  id: string
  /** Plain-English name, e.g. "Instagram post (square)". */
  label: string
  kind: FormatKind
  width_px: number
  height_px: number
  network: string
  /** Fold panels for brochures; 1 for a flat sheet. */
  panels: number
  limits: CopyLimits | null
}

/** One AI choice in the picker (`model_choice.ModelOption`). */
export interface ModelOption {
  /** What is sent back: a connector slug, or "none" for no AI image. */
  key: string
  label: string
  available: boolean
  /** Why it can't be chosen right now, in plain English ("" when it can). */
  reason: string
  reason_code: string
  /** Raw identifiers, for the "Advanced" disclosure only. */
  advanced: { connector: string; model: string }
}

export interface ModelPicker {
  text: ModelOption[]
  image: ModelOption[]
  text_ready: boolean
  reason: string
  reason_code: string
  /** Optional preselection from the server; the console falls back to the first usable option. */
  recommended_text: string
  recommended_image: string
}

/** This month's allowance. A limit of 0 means no cap. */
export interface CreativeQuota {
  limits: { jobs_per_month: number; images_per_month: number; tokens_per_month: number }
  used: { jobs: number; images: number; tokens: number }
  /** ISO date the allowance resets, when the server sends it. */
  resets_on: string
}

/** Input limits the server enforces (the server still decides). */
export interface CreativeLimits {
  max_formats: number
  message_chars: number
  audience_chars: number
  notes_chars: number
  goal_chars: number
  tone_chars: number
  max_revisions: number
}

export interface CreativeOptions {
  formats: CreativeFormat[]
  models: ModelPicker
  quota: CreativeQuota | null
  limits: CreativeLimits
}

/** Body of `POST /creative/jobs`. */
export interface CreativeJobInput {
  formats: string[]
  brand_kit_id: string
  product_id?: string | null
  message: string
  audience: string
  notes: string
  /** What the design is for ("Promote an offer"); "" for none. Never null: the server expects text. */
  goal?: string
  /** The feel, when it should differ from the brand voice; "" for the brand voice. Never null. */
  tone?: string
  text_model: { connector: string; model?: string | null }
  image_model: { provider: string; model?: string | null }
}

export type CreativeJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface QAIssue {
  code: string
  severity: 'error' | 'warning'
  /** Fixed wording from the server; never quotes the copy. */
  message: string
}

export interface CreativeCopy {
  headline: string
  body: string
  cta: string
}

/** One design a job produced. */
export interface CreativeOutput {
  format_id: string
  asset_id: string | null
  /** The review item for this design; null while it is being made. */
  approval_id: string | null
  preview_file_id: string | null
  qa: { passed: boolean; issues: QAIssue[] }
  /** pending | approved | rejected | superseded | exported, when the server sends it. */
  approval_status: string
  /** The current wording, when the server includes it (to prefill "Edit text"). */
  copy: CreativeCopy | null
  /** A friendly reason this format could not be made, if it failed. */
  error: string
}

export interface CreativeJob {
  id: string
  status: CreativeJobStatus
  /** Optional finer progress: queued | writing | designing | checking. */
  stage: string
  formats: string[]
  brand_kit_id: string | null
  product_id: string | null
  parent_job_id: string | null
  revision_count: number
  outputs: CreativeOutput[]
  /** Machine key for a failure ("model_unavailable", "quota_exceeded"…). */
  error_code: string
  /** Plain-English failure text, when the server sends it. */
  error: string
  created_at: string
  finished_at: string | null
}

export type DownloadFormat = 'png' | 'pdf' | 'zip'

export const TERMINAL_STATUSES: ReadonlySet<CreativeJobStatus> = new Set(['succeeded', 'failed', 'cancelled'])

/** Fallbacks when the server omits a limit; they mirror the plan's contract. */
export const DEFAULT_LIMITS: CreativeLimits = {
  max_formats: 8,
  message_chars: 600,
  audience_chars: 300,
  notes_chars: 600,
  goal_chars: 100,
  tone_chars: 100,
  max_revisions: 5,
}

/** The image choice that uses no AI model (`model_choice.NO_IMAGE`). Always allowed. */
export const NO_IMAGE_KEY = 'none'
export const NO_IMAGE_LABEL = 'No AI image: use my photos or brand colours'

/** Server cap on "Ask for changes" feedback (`RequestChangesRequest`). */
export const FEEDBACK_MAX_CHARS = 2000

// ── Parsers ────────────────────────────────────────────────────────────────
type Obj = Record<string, unknown>

function obj(value: unknown): Obj {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Obj) : {}
}

function str(value: unknown, max = 2000): string {
  return typeof value === 'string' ? value.slice(0, max) : ''
}

function num(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function bool(value: unknown): boolean {
  return value === true
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function idOrNull(value: unknown): string | null {
  return typeof value === 'string' && isId(value) ? value : null
}

/** Format ids and connector keys: short, lower-case, no path characters. */
const KEY = /^[a-z0-9][a-z0-9_:.-]{0,63}$/

function key(value: unknown): string {
  return typeof value === 'string' && KEY.test(value) ? value : ''
}

const KINDS: readonly FormatKind[] = ['banner', 'social', 'email', 'print']
const STATUSES: readonly CreativeJobStatus[] = ['queued', 'running', 'succeeded', 'failed', 'cancelled']

function parseLimits(value: unknown): CopyLimits | null {
  const raw = obj(value)
  if (!('headline_chars' in raw)) return null
  return {
    headline_chars: num(raw.headline_chars, 70),
    body_chars: num(raw.body_chars, 0),
    cta_chars: num(raw.cta_chars, 30),
    headline_words: typeof raw.headline_words === 'number' ? raw.headline_words : null,
  }
}

function parseFormat(value: unknown): CreativeFormat | null {
  const raw = obj(value)
  const id = key(raw.id)
  const kind = KINDS.find((k) => k === raw.kind)
  if (!id || !kind) return null
  return {
    id,
    label: str(raw.label, 120) || id,
    kind,
    width_px: num(raw.width_px),
    height_px: num(raw.height_px),
    network: str(raw.network, 40),
    panels: Math.max(1, num(raw.panels, 1)),
    limits: parseLimits(raw.limits),
  }
}

function parseOption(value: unknown): ModelOption | null {
  const raw = obj(value)
  const optionKey = key(raw.key)
  if (!optionKey) return null
  const advanced = obj(raw.advanced)
  return {
    key: optionKey,
    label: str(raw.label, 120) || optionKey,
    available: bool(raw.available),
    reason: str(raw.reason, 400),
    reason_code: str(raw.reason_code, 64),
    advanced: { connector: str(advanced.connector, 64), model: str(advanced.model, 128) },
  }
}

function parsePicker(value: unknown): ModelPicker {
  const raw = obj(value)
  const options = (items: unknown) =>
    list(items)
      .map(parseOption)
      .filter((o): o is ModelOption => o !== null)
  const image = options(raw.image)
  // "No AI image" is always allowed by the server, so it is always offered.
  if (!image.some((o) => o.key === NO_IMAGE_KEY)) {
    image.push({
      key: NO_IMAGE_KEY,
      label: NO_IMAGE_LABEL,
      available: true,
      reason: '',
      reason_code: '',
      advanced: { connector: '', model: '' },
    })
  }
  return {
    text: options(raw.text),
    image,
    text_ready: bool(raw.text_ready),
    reason: str(raw.reason, 400),
    reason_code: str(raw.reason_code, 64),
    recommended_text: key(raw.recommended_text),
    recommended_image: key(raw.recommended_image),
  }
}

function parseQuota(value: unknown): CreativeQuota | null {
  const raw = obj(value)
  if (!('limits' in raw) || !('used' in raw)) return null
  const limits = obj(raw.limits)
  const used = obj(raw.used)
  return {
    limits: {
      jobs_per_month: num(limits.jobs_per_month),
      images_per_month: num(limits.images_per_month),
      tokens_per_month: num(limits.tokens_per_month),
    },
    used: { jobs: num(used.jobs), images: num(used.images), tokens: num(used.tokens) },
    resets_on: str(raw.resets_on, 40),
  }
}

function parseOptions(value: unknown): CreativeOptions {
  const raw = obj(value)
  const limits = obj(raw.limits)
  const positive = (v: unknown, fallback: number) => {
    const n = num(v, fallback)
    return n > 0 ? Math.floor(n) : fallback
  }
  return {
    formats: list(raw.formats)
      .map(parseFormat)
      .filter((f): f is CreativeFormat => f !== null),
    models: parsePicker(raw.models ?? raw.picker),
    quota: parseQuota(raw.quota),
    limits: {
      max_formats: positive(limits.max_formats, DEFAULT_LIMITS.max_formats),
      message_chars: positive(limits.message_chars, DEFAULT_LIMITS.message_chars),
      audience_chars: positive(limits.audience_chars, DEFAULT_LIMITS.audience_chars),
      notes_chars: positive(limits.notes_chars, DEFAULT_LIMITS.notes_chars),
      goal_chars: positive(limits.goal_chars, DEFAULT_LIMITS.goal_chars),
      tone_chars: positive(limits.tone_chars, DEFAULT_LIMITS.tone_chars),
      max_revisions: num(limits.max_revisions, DEFAULT_LIMITS.max_revisions),
    },
  }
}

function parseCopy(value: unknown): CreativeCopy | null {
  const raw = obj(value)
  if (typeof raw.headline !== 'string') return null
  return { headline: str(raw.headline), body: str(raw.body), cta: str(raw.cta) }
}

function parseOutput(value: unknown): CreativeOutput | null {
  const raw = obj(value)
  const formatId = key(raw.format_id ?? raw.format)
  if (!formatId) return null
  const qa = obj(raw.qa)
  const issues = list(qa.issues)
    .map((item): QAIssue | null => {
      const issue = obj(item)
      const message = str(issue.message, 400)
      if (!message) return null
      return { code: str(issue.code, 64), severity: issue.severity === 'warning' ? 'warning' : 'error', message }
    })
    .filter((i): i is QAIssue => i !== null)
  return {
    format_id: formatId,
    asset_id: idOrNull(raw.asset_id),
    approval_id: idOrNull(raw.approval_id),
    preview_file_id: idOrNull(raw.preview_file_id),
    qa: { passed: typeof qa.passed === 'boolean' ? qa.passed : !issues.some((i) => i.severity === 'error'), issues },
    approval_status: str(raw.approval_status, 32),
    copy: parseCopy(raw.copy),
    error: str(raw.error, 400),
  }
}

/** A job from the server, or null when it is not a usable job record. */
function parseJob(value: unknown): CreativeJob | null {
  const raw = obj(value)
  const id = idOrNull(raw.id)
  const status = STATUSES.find((s) => s === raw.status)
  if (!id || !status) return null
  return {
    id,
    status,
    stage: str(raw.stage, 32),
    formats: list(raw.formats).map(key).filter(Boolean),
    brand_kit_id: idOrNull(raw.brand_kit_id),
    product_id: idOrNull(raw.product_id),
    parent_job_id: idOrNull(raw.parent_job_id),
    revision_count: num(raw.revision_count),
    outputs: list(raw.outputs)
      .map(parseOutput)
      .filter((o): o is CreativeOutput => o !== null),
    error_code: str(raw.error_code, 64),
    error: str(raw.error, 400),
    created_at: str(raw.created_at, 40),
    finished_at: typeof raw.finished_at === 'string' ? raw.finished_at.slice(0, 40) : null,
  }
}

function jobOrThrow(value: unknown): CreativeJob {
  const job = parseJob(value)
  if (!job) throw new SyntaxError('Unexpected creative job reply')
  return job
}

function idPath(value: string): string {
  if (!isId(value)) throw new Error('Invalid id')
  return encodeURIComponent(value)
}

// ── Endpoints ──────────────────────────────────────────────────────────────
export const creativeApi = {
  options: async (signal?: AbortSignal): Promise<CreativeOptions> =>
    parseOptions(await request<unknown>('/creative/options', { signal })),

  createJob: async (body: CreativeJobInput): Promise<CreativeJob> =>
    jobOrThrow(await request<unknown>('/creative/jobs', { method: 'POST', body })),

  jobs: async (signal?: AbortSignal): Promise<CreativeJob[]> => {
    const reply = await request<unknown>('/creative/jobs', { signal })
    // A bare list, or a page object with `items`.
    const items = Array.isArray(reply) ? reply : list(obj(reply).items)
    return items.map(parseJob).filter((j): j is CreativeJob => j !== null)
  },

  job: async (id: string, signal?: AbortSignal): Promise<CreativeJob> =>
    jobOrThrow(await request<unknown>(`/creative/jobs/${idPath(id)}`, { signal })),

  /** An approved design's file. The server refuses work that is not approved. */
  download: (assetId: string, format: DownloadFormat) =>
    requestBlob(`/creative/assets/${idPath(assetId)}/download?format=${encodeURIComponent(format)}`),

  /** Every approved design of a job, as one ZIP. The server leaves out work that is not approved. */
  downloadJob: (jobId: string) => requestBlob(`/creative/jobs/${idPath(jobId)}/download?format=zip`),
}
