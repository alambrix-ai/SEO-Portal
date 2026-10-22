/**
 * Creative Studio - on-brand images, banners and flyers in five plain steps.
 *
 * 1. What are you making?  Visual tiles; several sizes per job, up to the server limit.
 * 2. Brand and product.    Kit and product pickers with thumbnails (Brand Kit API).
 * 3. Message.              Goal, audience, key message and an optional tone.
 * 4. AI choices.           Friendly labels from the picker, recommended one preselected.
 * 5. Generate.             Progress, previews, QA notes and the review actions.
 *
 * Every design waits for a person: Approve, Ask for changes (a new AI round)
 * or Edit text (no AI call). Downloads unlock only after approval, and the
 * server enforces that as well as every permission shown here.
 *
 * Images are fetched as authenticated blobs (`GET /files/{id}`) and shown
 * through object URLs, never through a URL that carries a token. Text from
 * the server is rendered as plain React text, never as HTML.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { brandKitApi, isId, type BrandKit, type Product } from '@/api/brandKit'
import { api, ApiError } from '@/api/client'
import {
  creativeApi,
  FEEDBACK_MAX_CHARS,
  safeFilename,
  TERMINAL_STATUSES,
  type CreativeFormat,
  type CreativeJob,
  type CreativeJobInput,
  type CreativeOptions,
  type CreativeOutput,
  type CreativeQuota,
  type DownloadFormat,
  type ModelOption,
  type ModelPicker,
} from '@/api/creative'
import { useAuth } from '@/auth/AuthContext'
import { ShareDialog } from '@/components/social/ShareDialog'
import { useToasts } from '@/components/Toasts'
import { Dialog, EmptyState, ErrorState, Field, Loading, PageHeader, Tag } from '@/components/ui'
import { friendlyLoadError, invalidateResourceCache, useResource } from '@/hooks/useResource'
import { REVIEW_SETTING_NAME, sentence } from '@/lib/plain'
import { softenErrorMessage } from '@/lib/softenError'
import '@/styles/creative.css'

// ── Copy ───────────────────────────────────────────────────────────────────
const STEPS = ['What are you making?', 'Brand and product', 'Message', 'AI choices', 'Generate'] as const
type StepIndex = 0 | 1 | 2 | 3 | 4

const ASK_ADMIN = 'Ask a workspace admin. They can approve AI models for Creative Studio under Settings, AI models.'
const NO_WRITE_NOTE =
  'You can look around, but making designs needs edit access to Content. Ask a workspace admin if you need it.'
const RATE_LIMITED = 'You are going a little fast. Wait a minute, then try again.'
const NO_PERMISSION = 'You do not have permission to do that. Ask a workspace admin if you need it.'
const GONE = 'That design is no longer available. Refresh the page to see the latest.'
const NOT_AVAILABLE = 'Creative Studio is not switched on for this workspace yet. Ask a workspace admin.'
const CREATE_FALLBACK = 'We could not start your designs. Please try again.'
const SLOW_NOTE =
  'This is taking longer than usual. You can leave this page: finished designs will also appear in Approvals.'
const LOST_TOUCH = 'We lost touch with the server while checking on your designs.'

const QUOTA_MESSAGES: Record<string, string> = {
  jobs_per_month:
    'Your workspace has used all of this month’s designs. The allowance resets on the 1st, or ask your administrator about a higher limit.',
  images_per_month:
    'Your workspace has used all of this month’s AI images. Choose “No AI image” to keep going, or ask your administrator about a higher limit.',
  tokens_per_month:
    'Your workspace has used all of this month’s AI writing allowance. It resets on the 1st, or ask your administrator about a higher limit.',
}

const JOB_FAILURES: Record<string, string> = {
  model_unavailable: 'The AI model you chose is not available any more. Pick another one in step 4 and try again.',
  models_not_approved: `An administrator needs to approve AI models for Creative Studio. ${ASK_ADMIN}`,
  quota_exceeded: QUOTA_MESSAGES.jobs_per_month ?? '',
  render_failed: 'We could not lay out these designs. Please try again, or choose fewer sizes.',
  halted: 'Creative work is paused for this workspace right now. Ask a workspace admin.',
  timeout: 'Making these designs took too long. Please try again with fewer sizes.',
}
const JOB_FAILED_FALLBACK = 'We could not make these designs. Please try again.'

const GOALS = [
  'Promote an offer',
  'Launch something new',
  'Build awareness',
  'Get sign-ups or enquiries',
  'Announce an event',
] as const

const TONES = ['Friendly', 'Professional', 'Bold', 'Playful', 'Calm', 'Luxurious'] as const

const FEEDBACK_EXAMPLES = [
  'Make the headline shorter and punchier.',
  'Use more of our main brand colour.',
  'Make the button text more direct, like “Book now”.',
  'Put the product photo first and the text below it.',
  'Use a calmer, more professional tone.',
]

// ── Format groups (the tiles) ──────────────────────────────────────────────
type GroupId = 'social' | 'banner' | 'email' | 'flyer' | 'brochure'

const GROUPS: { id: GroupId; title: string; description: string }[] = [
  { id: 'social', title: 'Social post', description: 'Instagram, Facebook, LinkedIn, X and Pinterest.' },
  { id: 'banner', title: 'Display banner', description: 'Standard web ad sizes, kept under 150 KB.' },
  { id: 'email', title: 'Email header', description: 'A wide image for the top of an email.' },
  { id: 'flyer', title: 'Flyer', description: 'A printable page, A4 or US Letter.' },
  { id: 'brochure', title: 'Brochure', description: 'A folded print brochure with bleed.' },
]

function groupOf(format: CreativeFormat): GroupId {
  if (format.kind === 'print') return format.id.startsWith('brochure') ? 'brochure' : 'flyer'
  return format.kind
}

function sizeText(format: CreativeFormat): string {
  if (format.kind === 'print') return format.panels > 1 ? `${format.panels} panels, print-ready PDF` : 'Print-ready PDF'
  return format.width_px && format.height_px ? `${format.width_px} × ${format.height_px} px` : ''
}

/** A small outline of each tile's shape: decorative only. */
function GroupIcon({ group }: { group: GroupId }) {
  const shapes: Record<GroupId, { w: number; h: number; x: number; y: number }> = {
    social: { w: 26, h: 26, x: 7, y: 7 },
    banner: { w: 34, h: 12, x: 3, y: 14 },
    email: { w: 34, h: 14, x: 3, y: 6 },
    flyer: { w: 22, h: 32, x: 9, y: 4 },
    brochure: { w: 32, h: 24, x: 4, y: 8 },
  }
  const s = shapes[group]
  return (
    <svg className="cs-tile-icon" viewBox="0 0 40 40" width="40" height="40" aria-hidden="true" focusable="false">
      <rect x={s.x} y={s.y} width={s.w} height={s.h} rx="3" fill="none" stroke="currentColor" strokeWidth="2" />
      {group === 'brochure' ? <line x1="20" y1={s.y} x2="20" y2={s.y + s.h} stroke="currentColor" strokeWidth="1.5" /> : null}
      {group === 'email' ? <line x1="3" y1="26" x2="37" y2="26" stroke="currentColor" strokeWidth="1.5" /> : null}
    </svg>
  )
}

// ── Helpers ────────────────────────────────────────────────────────────────
function remaining(limit: number, used: number): number | null {
  return limit > 0 ? Math.max(0, limit - used) : null
}

function quotaLine(quota: CreativeQuota | null): string {
  if (!quota) return ''
  return quotaItems(quota)
    .map((item) => `${item.label}: ${item.value}`)
    .join(' · ')
}

function quotaItems(quota: CreativeQuota): { label: string; value: string }[] {
  const jobs = remaining(quota.limits.jobs_per_month, quota.used.jobs)
  const images = remaining(quota.limits.images_per_month, quota.used.images)
  const items = [
    {
      label: 'Designs left this month',
      value: jobs === null ? 'No limit' : `${jobs} of ${quota.limits.jobs_per_month}`,
    },
  ]
  if (images !== null) {
    items.push({
      label: 'AI images left',
      value: `${images} of ${quota.limits.images_per_month}`,
    })
  }
  return items
}

function recommendedKey(options: ModelOption[], preferred: string, avoid?: (o: ModelOption) => boolean): string {
  const usable = options.filter((o) => o.available && !(avoid?.(o) ?? false))
  if (preferred && usable.some((o) => o.key === preferred)) return preferred
  return usable[0]?.key ?? ''
}

function isAbort(caught: unknown): boolean {
  return caught instanceof Error && caught.name === 'AbortError'
}

/**
 * One plain sentence for any error from this screen's actions.
 *
 * `on409` replaces a 409 only when `on409Reason` matches the server's
 * `fields.reason` (downloads pass `'not_approved'`). Any other conflict,
 * including an emergency stop, keeps the server's own, softened, sentence.
 */
function actionErrorMessage(caught: unknown, fallback: string, on409 = '', on409Reason = ''): string {
  if (caught instanceof ApiError) {
    if (caught.status === 429) {
      const quota = caught.code === 'quota_exceeded' ? QUOTA_MESSAGES[String(caught.fields?.quota ?? '')] : undefined
      return quota ?? (caught.code === 'quota_exceeded' ? QUOTA_MESSAGES.jobs_per_month ?? RATE_LIMITED : RATE_LIMITED)
    }
    if (caught.status === 403) return NO_PERMISSION
    if (caught.status === 404) return GONE
    if (
      caught.status === 409 &&
      on409 &&
      on409Reason &&
      String(caught.fields?.reason ?? '') === on409Reason
    ) {
      return on409
    }
    if (caught.status === 413) return 'That is more text than we can take. Please shorten it.'
    if (caught.status === 422) {
      if (String(caught.fields?.reason ?? '') === 'models_not_approved') {
        return `An administrator needs to approve AI models for Creative Studio. ${ASK_ADMIN}`
      }
      return softenErrorMessage(caught.message, 'Some details need a fix. Please check them and try again.')
    }
    return softenErrorMessage(caught.message, fallback)
  }
  if (caught instanceof Error) return friendlyLoadError(caught)
  return fallback
}

/** Which wizard step a rejected field belongs to. */
function stepForField(field: string): StepIndex | null {
  const name = field.split('.')[0] ?? field
  if (name === 'formats') return 0
  if (name === 'brand_kit_id' || name === 'product_id') return 1
  if (name === 'message' || name === 'audience' || name === 'notes' || name === 'goal' || name === 'tone') return 2
  if (name === 'text_model' || name === 'image_model') return 3
  return null
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.rel = 'noopener'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 2000)
}

// ── Images ─────────────────────────────────────────────────────────────────
/** A stored file as an object URL, fetched with the session's auth (same pattern as BrandKitPage). */
function useFileImage(id: string | null | undefined): { src: string; failed: boolean } {
  const [state, setState] = useState<{ src: string; failed: boolean }>({ src: '', failed: false })
  useEffect(() => {
    setState({ src: '', failed: false })
    if (!isId(id)) return
    const controller = new AbortController()
    let url = ''
    void brandKitApi
      .fileBlob(id, controller.signal)
      .then(({ blob }) => {
        if (controller.signal.aborted) return
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

function FileImage({ id, alt, className }: { id: string | null; alt: string; className: string }) {
  const { src, failed } = useFileImage(id)
  if (src) return <img className={className} src={src} alt={alt} />
  return (
    <span className={`${className} cs-image-empty`} role={alt ? 'img' : undefined} aria-label={alt || undefined}>
      {failed ? 'Image unavailable' : id ? 'Loading…' : 'No image'}
    </span>
  )
}

// ── Step 1: formats ────────────────────────────────────────────────────────
function FormatStep({
  formats,
  selected,
  maxFormats,
  onToggle,
}: {
  formats: CreativeFormat[]
  selected: string[]
  maxFormats: number
  onToggle: (id: string) => void
}) {
  const groups = GROUPS.map((group) => ({ ...group, formats: formats.filter((f) => groupOf(f) === group.id) })).filter(
    (group) => group.formats.length > 0,
  )
  const [open, setOpen] = useState<GroupId | null>(groups[0]?.id ?? null)
  const full = selected.length >= maxFormats
  const active = groups.find((g) => g.id === open) ?? null

  if (groups.length === 0) {
    return <p className="cs-note">No design sizes are available right now. Please try again later.</p>
  }

  return (
    <div>
      <p className="cs-note">
        Pick a type, then tick the sizes you need. You can mix types and choose up to {maxFormats} sizes in one go.
      </p>
      <div className="cs-tiles">
        {groups.map((group) => {
          const count = group.formats.filter((f) => selected.includes(f.id)).length
          return (
            <button
              key={group.id}
              type="button"
              className={`cs-tile${open === group.id ? ' is-open' : ''}${count > 0 ? ' is-chosen' : ''}`}
              aria-pressed={open === group.id}
              aria-controls="cs-sizes"
              onClick={() => setOpen(group.id)}
            >
              <GroupIcon group={group.id} />
              <span className="cs-tile-title">{group.title}</span>
              <span className="cs-tile-text">{group.description}</span>
              <span className="cs-tile-count">{count > 0 ? `${count} chosen` : 'None chosen'}</span>
            </button>
          )
        })}
      </div>

      {active ? (
        <fieldset id="cs-sizes" className="cs-sizes">
          <legend className="cs-legend">{active.title} sizes</legend>
          <ul className="cs-size-list">
            {active.formats.map((format) => {
              const checked = selected.includes(format.id)
              const disabled = !checked && full
              return (
                <li key={format.id}>
                  <label className={`cs-size${checked ? ' is-checked' : ''}${disabled ? ' is-disabled' : ''}`}>
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={disabled}
                      onChange={() => onToggle(format.id)}
                    />
                    <span className="cs-size-name">{format.label}</span>
                    <span className="cs-size-meta">{sizeText(format)}</span>
                  </label>
                </li>
              )
            })}
          </ul>
        </fieldset>
      ) : null}

      <p className="cs-count" role="status" aria-live="polite">
        {selected.length === 0
          ? 'No sizes chosen yet.'
          : `${selected.length} of ${maxFormats} sizes chosen.${full ? ' That is the most for one job: untick one to swap it.' : ''}`}
      </p>
    </div>
  )
}

// ── Step 2: brand and product ──────────────────────────────────────────────
function BrandStep({
  kits,
  products,
  kitsError,
  productsError,
  kitId,
  productId,
  onKit,
  onProduct,
  onRetry,
}: {
  kits: BrandKit[] | null
  products: Product[] | null
  kitsError: string | null
  productsError: string | null
  kitId: string
  productId: string
  onKit: (id: string) => void
  onProduct: (id: string) => void
  onRetry: () => void
}) {
  if (kitsError) return <ErrorState message={kitsError} onRetry={onRetry} />
  if (!kits) return <Loading label="Loading your brand kits…" />
  if (kits.length === 0) {
    return (
      <EmptyState
        title="Set up your brand first"
        description="Designs use your logo, colours and fonts from a brand kit. Add one in Brand Kit, then come back here. If you can't edit the brand kit, ask a workspace admin."
        action={
          <Link className="btn btn-primary" to="/brand-kit">
            Go to Brand Kit
          </Link>
        }
      />
    )
  }
  const product = products?.find((p) => p.id === productId) ?? null

  return (
    <div className="cs-brand">
      <fieldset className="cs-fieldset">
        <legend className="cs-legend">Brand kit</legend>
        <div className="cs-choice-grid">
          {kits.map((kit) => (
            <label key={kit.id} className={`cs-choice${kitId === kit.id ? ' is-checked' : ''}`}>
              <input type="radio" name="cs-kit" value={kit.id} checked={kitId === kit.id} onChange={() => onKit(kit.id)} />
              <FileImage id={kit.logo_file_id} alt="" className="cs-choice-thumb" />
              <span className="cs-choice-text">
                <span className="cs-choice-name">
                  {kit.name}
                  {kit.is_default ? <Tag tone="outline">Default</Tag> : null}
                </span>
                <span className="cs-swatches" aria-hidden="true">
                  {[kit.primary_color, kit.secondary_color, kit.accent_color].map((colour, index) => (
                    <span key={index} className="cs-swatch" style={{ background: colour }} />
                  ))}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="cs-fieldset">
        <legend className="cs-legend">Product (optional)</legend>
        {productsError ? (
          <p className="cs-note">{productsError}</p>
        ) : !products ? (
          <Loading label="Loading your products…" />
        ) : (
          <>
            {products.length === 0 ? (
              <p className="cs-note">
                You have no products yet. Designs can still promote your brand, or{' '}
                <Link to="/brand-kit">add a product in Brand Kit</Link> so the copy can mention it.
              </p>
            ) : null}
            <div className="cs-choice-grid">
              <label className={`cs-choice${productId === '' ? ' is-checked' : ''}`}>
                <input type="radio" name="cs-product" value="" checked={productId === ''} onChange={() => onProduct('')} />
                <span className="cs-choice-thumb cs-image-empty" aria-hidden="true">
                  Brand
                </span>
                <span className="cs-choice-text">
                  <span className="cs-choice-name">No specific product</span>
                  <span className="cs-choice-meta">About the brand in general</span>
                </span>
              </label>
              {products.map((item) => (
                <label key={item.id} className={`cs-choice${productId === item.id ? ' is-checked' : ''}`}>
                  <input
                    type="radio"
                    name="cs-product"
                    value={item.id}
                    checked={productId === item.id}
                    onChange={() => onProduct(item.id)}
                  />
                  <FileImage id={item.image_file_ids[0] ?? null} alt="" className="cs-choice-thumb" />
                  <span className="cs-choice-text">
                    <span className="cs-choice-name">{item.name}</span>
                    <span className="cs-choice-meta">
                      {item.approved_claims.length === 1 ? '1 approved claim' : `${item.approved_claims.length} approved claims`}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </>
        )}
      </fieldset>

      {product ? (
        <div className="cs-product-detail">
          {product.approved_claims.length === 0 ? (
            <p className="cs-callout is-warning" role="note">
              <strong>Heads up:</strong> this product has no approved claims, so the copy will not mention specific
              benefits, prices or numbers. <Link to="/brand-kit">Add claims in Brand Kit</Link>.
            </p>
          ) : null}
          {product.image_file_ids.length > 0 ? (
            <>
              <div className="cs-subhead">Photos we can use</div>
              <ul className="cs-photos">
                {product.image_file_ids.slice(0, 6).map((id, index) => (
                  <li key={id}>
                    <FileImage id={id} alt={`${product.name} photo ${index + 1}`} className="cs-photo" />
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="cs-note">This product has no photos. Designs will use an AI image or your brand colours.</p>
          )}
        </div>
      ) : null}
    </div>
  )
}

// ── Step 3: message ────────────────────────────────────────────────────────
interface MessageState {
  goal: string
  audience: string
  message: string
  tone: string
  extra: string
}

function CharCount({ id, current, max }: { id: string; current: number; max: number }) {
  return (
    <span id={id} className="cs-char-count">
      {current} / {max}
    </span>
  )
}

function MessageStep({
  value,
  limits,
  errors,
  onChange,
}: {
  value: MessageState
  limits: CreativeOptions['limits']
  errors: Record<string, string>
  onChange: (next: MessageState) => void
}) {
  const extraLimit = limits.notes_chars
  return (
    <div className="cs-message">
      <fieldset className="cs-fieldset">
        <legend className="cs-legend">Goal (optional)</legend>
        <p className="field-description">What this design should do. Pick one, or leave it as Not sure.</p>
        <div className="cs-chip-row" role="radiogroup" aria-label="Goal">
          {GOALS.map((goal) => (
            <label key={goal} className={`cs-chip${value.goal === goal ? ' is-checked' : ''}`}>
              <input
                type="radio"
                name="cs-goal"
                value={goal}
                checked={value.goal === goal}
                onChange={() => onChange({ ...value, goal })}
              />
              {goal}
            </label>
          ))}
          <label className={`cs-chip${value.goal === '' ? ' is-checked' : ''}`}>
            <input type="radio" name="cs-goal" value="" checked={value.goal === ''} onChange={() => onChange({ ...value, goal: '' })} />
            Not sure
          </label>
        </div>
      </fieldset>

      <Field
        label="Key message"
        htmlFor="cs-message"
        required
        description="The one thing people should take away."
        error={errors.message}
      >
        <div className="cs-counted">
          <textarea
            id="cs-message"
            className="input"
            rows={5}
            required
            maxLength={limits.message_chars}
            value={value.message}
            placeholder="e.g. Our new summer menu is here, with 20% off lunch for members this week."
            aria-invalid={errors.message ? true : undefined}
            aria-describedby="cs-message-desc cs-message-count"
            onChange={(event) => onChange({ ...value, message: event.target.value })}
          />
          <CharCount id="cs-message-count" current={value.message.length} max={limits.message_chars} />
        </div>
      </Field>

      <div className="cs-message-split">
        <Field
          label="Who is it for? (optional)"
          htmlFor="cs-audience"
          description="The people you want this to speak to."
          error={errors.audience}
        >
          <div className="cs-counted">
            <input
              id="cs-audience"
              className="input"
              type="text"
              maxLength={limits.audience_chars}
              value={value.audience}
              placeholder="e.g. Office workers near our city-centre shop"
              aria-invalid={errors.audience ? true : undefined}
              aria-describedby="cs-audience-desc cs-audience-count"
              onChange={(event) => onChange({ ...value, audience: event.target.value })}
            />
            <CharCount id="cs-audience-count" current={value.audience.length} max={limits.audience_chars} />
          </div>
        </Field>

        <Field
          label="Tone (optional)"
          htmlFor="cs-tone"
          description="Leave it on your brand voice unless this design needs a different feel."
        >
          <select
            id="cs-tone"
            className="input"
            value={value.tone}
            onChange={(event) => onChange({ ...value, tone: event.target.value })}
          >
            <option value="">Use my brand voice</option>
            {TONES.map((tone) => (
              <option key={tone} value={tone}>
                {tone}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field
        label="Anything else? (optional)"
        htmlFor="cs-extra"
        description="Extra details the design should include."
        error={errors.notes}
      >
        <div className="cs-counted">
          <textarea
            id="cs-extra"
            className="input"
            rows={3}
            maxLength={extraLimit}
            value={value.extra}
            placeholder="e.g. Mention that we are open late on Fridays."
            aria-invalid={errors.notes ? true : undefined}
            aria-describedby="cs-extra-desc cs-extra-count"
            onChange={(event) => onChange({ ...value, extra: event.target.value.slice(0, extraLimit) })}
          />
          <CharCount id="cs-extra-count" current={value.extra.length} max={extraLimit} />
        </div>
      </Field>
    </div>
  )
}

// ── Step 4: AI choices ─────────────────────────────────────────────────────
function ModelGroup({
  name,
  legend,
  options,
  value,
  recommended,
  onChange,
}: {
  name: string
  legend: string
  options: ModelOption[]
  value: string
  recommended: string
  onChange: (key: string) => void
}) {
  return (
    <fieldset className="cs-fieldset">
      <legend className="cs-legend">{legend}</legend>
      <ul className="cs-model-list">
        {options.map((option) => {
          const reasonId = `${name}-${option.key}-reason`
          return (
            <li key={option.key}>
              <label className={`cs-model${value === option.key ? ' is-checked' : ''}${option.available ? '' : ' is-disabled'}`}>
                <input
                  type="radio"
                  name={name}
                  value={option.key}
                  checked={value === option.key}
                  disabled={!option.available}
                  aria-describedby={option.available ? undefined : reasonId}
                  onChange={() => onChange(option.key)}
                />
                <span className="cs-model-label">
                  {option.label}
                  {option.key === recommended ? <Tag tone="accent">Recommended</Tag> : null}
                  {!option.available ? <Tag tone="outline">Not available</Tag> : null}
                </span>
              </label>
              {!option.available && option.reason ? (
                <p id={reasonId} className="cs-model-reason">
                  {sentence(option.reason)}
                  {option.reason_code === 'models_not_approved' ? ` ${ASK_ADMIN}` : ''}
                </p>
              ) : null}
            </li>
          )
        })}
      </ul>
    </fieldset>
  )
}

function AiStep({
  picker,
  quota,
  textKey,
  imageKey,
  recommendedText,
  recommendedImage,
  onText,
  onImage,
}: {
  picker: ModelPicker
  quota: CreativeQuota | null
  textKey: string
  imageKey: string
  recommendedText: string
  recommendedImage: string
  onText: (key: string) => void
  onImage: (key: string) => void
}) {
  const advanced = [...picker.text, ...picker.image].filter((o) => o.advanced.connector || o.advanced.model)
  const imagesLeft = quota ? remaining(quota.limits.images_per_month, quota.used.images) : null
  return (
    <div className="cs-ai">
      {!picker.text_ready ? (
        <div className="cs-callout is-warning" role="note">
          <strong>Writing is not ready yet.</strong>{' '}
          {picker.reason_code === 'models_not_approved' ? (
            <>
              {sentence(picker.reason) || 'An administrator needs to approve AI models for Creative Studio.'} {ASK_ADMIN}
            </>
          ) : picker.reason_code === 'not_connected' ? (
            <>
              {sentence(picker.reason)} A workspace admin can connect one on the <Link to="/connectors">Integrations</Link>{' '}
              page.
            </>
          ) : (
            <>
              {sentence(picker.reason) || 'None of the AI models can be used right now.'} Ask a workspace admin for help.
            </>
          )}
        </div>
      ) : null}

      {picker.text.length > 0 ? (
        <ModelGroup
          name="cs-text-model"
          legend="Who writes the words?"
          options={picker.text}
          value={textKey}
          recommended={recommendedText}
          onChange={onText}
        />
      ) : null}

      <ModelGroup
        name="cs-image-model"
        legend="Where does the picture come from?"
        options={picker.image}
        value={imageKey}
        recommended={recommendedImage}
        onChange={onImage}
      />
      {imagesLeft !== null ? (
        <p className="cs-note">
          AI images left this month: {imagesLeft}.{imagesLeft === 0 ? ' Choose “No AI image” to keep going.' : ''}
        </p>
      ) : null}

      {advanced.length > 0 ? (
        <details className="cs-advanced">
          <summary>Advanced: technical details</summary>
          <p className="cs-note">For admins checking which connection and model each choice uses.</p>
          <table className="cs-advanced-table">
            <thead>
              <tr>
                <th scope="col">Choice</th>
                <th scope="col">Connection</th>
                <th scope="col">Model</th>
              </tr>
            </thead>
            <tbody>
              {advanced.map((option, index) => (
                <tr key={`${option.key}-${index}`}>
                  <td>{option.label}</td>
                  <td>
                    <code>{option.advanced.connector || '—'}</code>
                  </td>
                  <td>
                    <code>{option.advanced.model || 'Not chosen'}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}
    </div>
  )
}

// ── Step 5: progress and results ───────────────────────────────────────────
type OutputState = 'making' | 'failed' | 'pending' | 'approved' | 'replaced' | 'rejected'

const OUTPUT_STATE: Record<OutputState, { label: string; tone: 'neutral' | 'accent' | 'accent-2' | 'danger' | 'warning' | 'outline'; mark: string }> = {
  making: { label: 'Being made', tone: 'neutral', mark: '…' },
  failed: { label: 'Could not be made', tone: 'danger', mark: '!' },
  pending: { label: 'Waiting for your review', tone: 'warning', mark: '?' },
  approved: { label: 'Approved', tone: 'accent-2', mark: '✓' },
  replaced: { label: 'Replaced by a new version', tone: 'outline', mark: '↻' },
  rejected: { label: 'Not approved', tone: 'danger', mark: '✕' },
}

function outputState(output: CreativeOutput, local: Record<string, OutputState>): OutputState {
  const localState = output.approval_id ? local[output.approval_id] : undefined
  if (localState) return localState
  switch (output.approval_status) {
    case 'approved':
    case 'exported':
    case 'published':
      return 'approved'
    case 'superseded':
      return 'replaced'
    case 'rejected':
    case 'expired':
      return 'rejected'
    default:
      break
  }
  if (output.error) return 'failed'
  return output.approval_id ? 'pending' : 'making'
}

function stageText(job: CreativeJob): string {
  switch (job.stage) {
    case 'queued':
      return 'Getting ready…'
    case 'writing':
      return 'Writing your copy…'
    case 'designing':
    case 'rendering':
    case 'images':
      return 'Designing…'
    case 'checking':
    case 'qa':
      return 'Checking your designs…'
    default:
      return job.status === 'queued' ? 'Getting ready…' : 'Writing your copy…'
  }
}

const PROGRESS_STEPS = ['Writing your copy', 'Designing', 'Checking your designs'] as const

function progressIndex(job: CreativeJob): number {
  const text = stageText(job)
  if (text.startsWith('Designing')) return 1
  if (text.startsWith('Checking')) return 2
  if (text.startsWith('Writing')) return 0
  return -1
}

function JobProgress({ job }: { job: CreativeJob }) {
  const current = progressIndex(job)
  return (
    <div className="cs-progress" role="status" aria-live="polite">
      <div className="cs-progress-now">
        <span className="cs-spinner" aria-hidden="true" />
        {stageText(job)}
      </div>
      <ol className="cs-progress-steps">
        {PROGRESS_STEPS.map((label, index) => {
          const state = index < current ? 'done' : index === current ? 'now' : 'next'
          return (
            <li key={label} className={`cs-progress-step is-${state}`}>
              <span className="cs-progress-mark" aria-hidden="true">
                {state === 'done' ? '✓' : index + 1}
              </span>
              {label}
              <span className="sr-only">{state === 'done' ? ' (done)' : state === 'now' ? ' (in progress)' : ' (next)'}</span>
            </li>
          )
        })}
      </ol>
      <p className="cs-note">This usually takes a minute or two. You can stay on this page.</p>
    </div>
  )
}

function QaNotes({ output }: { output: CreativeOutput }) {
  const { issues, passed } = output.qa
  if (issues.length === 0) {
    return <p className="cs-qa is-ok">{passed ? 'Passed all checks.' : 'Checks finished.'}</p>
  }
  return (
    <div className="cs-qa">
      <div className="cs-subhead">Quality checks</div>
      <ul className="cs-qa-list">
        {issues.map((issue, index) => (
          <li key={`${issue.code}-${index}`} className={`cs-qa-item is-${issue.severity}`}>
            <span className="cs-qa-kind">{issue.severity === 'error' ? 'Needs a fix:' : 'Worth a look:'}</span>{' '}
            {sentence(issue.message)}
          </li>
        ))}
      </ul>
    </div>
  )
}

function OutputCard({
  output,
  format,
  state,
  busy,
  canReview,
  canEdit,
  onApprove,
  onAskChanges,
  onEditText,
  onDownload,
  onShare,
}: {
  output: CreativeOutput
  format: CreativeFormat | null
  state: OutputState
  busy: boolean
  canReview: boolean
  canEdit: boolean
  onApprove: () => void
  onAskChanges: () => void
  onEditText: () => void
  onDownload: (format: DownloadFormat) => void
  onShare: () => void
}) {
  const label = format?.label ?? 'Design'
  const meta = OUTPUT_STATE[state]
  const reviewable = state === 'pending' && output.approval_id !== null
  const downloadable = state === 'approved' && output.asset_id !== null
  const isPrint = format?.kind === 'print'
  const headingId = `cs-out-${output.approval_id ?? output.asset_id ?? output.format_id}`
  return (
    <li className={`card cs-output is-${state}`} aria-labelledby={headingId}>
      <div className="cs-output-preview">
        {output.preview_file_id ? (
          <FileImage id={output.preview_file_id} alt={`Preview of the ${label} design`} className="cs-output-image" />
        ) : (
          <span className="cs-output-image cs-image-empty">{state === 'failed' ? 'No preview' : 'Preview on its way…'}</span>
        )}
      </div>
      <div className="cs-output-body">
        <div className="cs-output-head">
          <h3 id={headingId} className="cs-output-title">
            {label}
          </h3>
          <Tag tone={meta.tone}>
            <span aria-hidden="true">{meta.mark} </span>
            {meta.label}
          </Tag>
        </div>
        {format ? <div className="cs-output-meta">{sizeText(format)}</div> : null}
        {state === 'failed' ? (
          <p className="cs-callout is-danger">{sentence(output.error) || JOB_FAILED_FALLBACK}</p>
        ) : (
          <QaNotes output={output} />
        )}

        {reviewable ? (
          <div className="cs-output-actions">
            {canReview ? (
              <>
                <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={onApprove}>
                  Approve
                </button>
                <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onAskChanges}>
                  Ask for changes
                </button>
              </>
            ) : null}
            {canEdit ? (
              <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onEditText}>
                Edit text
              </button>
            ) : null}
            {!canReview && !canEdit ? (
              <p className="cs-note">Someone with approval rights needs to review this design.</p>
            ) : null}
          </div>
        ) : null}

        {state === 'approved' || state === 'pending' ? (
          <div className="cs-downloads" role="group" aria-label={`Download the ${label} design`}>
            <span className="cs-downloads-label">Download:</span>
            {!isPrint ? (
              <button type="button" className="btn btn-ghost btn-sm" disabled={!downloadable || busy} onClick={() => onDownload('png')}>
                PNG
              </button>
            ) : null}
            {isPrint ? (
              <button type="button" className="btn btn-ghost btn-sm" disabled={!downloadable || busy} onClick={() => onDownload('pdf')}>
                PDF
              </button>
            ) : null}
            <button type="button" className="btn btn-ghost btn-sm" disabled={!downloadable || busy} onClick={() => onDownload('zip')}>
              ZIP (all files)
            </button>
            {!downloadable ? <span className="cs-note">Downloads unlock after approval.</span> : null}
          </div>
        ) : null}

        {downloadable ? (
          <div className="cs-output-actions">
            <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onShare}>
              Share or schedule<span className="sr-only"> the {label} design</span>
            </button>
          </div>
        ) : null}
      </div>
    </li>
  )
}

// ── Dialogs ────────────────────────────────────────────────────────────────
function FeedbackDialog({
  label,
  busy,
  error,
  onSend,
  onCancel,
}: {
  label: string
  busy: boolean
  error: string
  onSend: (feedback: string) => void
  onCancel: () => void
}) {
  const [feedback, setFeedback] = useState('')
  const trimmed = feedback.trim()
  const add = (example: string) =>
    setFeedback((current) => (current.trim() ? `${current.trim()} ${example}` : example).slice(0, FEEDBACK_MAX_CHARS))
  return (
    <Dialog
      title={`Ask for changes: ${label}`}
      onClose={busy ? () => undefined : onCancel}
      width={560}
      actions={
        <>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" disabled={busy || !trimmed} onClick={() => onSend(trimmed)}>
            {busy ? 'Sending…' : 'Send for a new version'}
          </button>
        </>
      }
    >
      <p className="cs-note">
        Say what to change in plain words. The AI makes a new version and it comes back here for your review.
      </p>
      <Field
        label="What should change?"
        htmlFor="cs-feedback"
        required
        error={error || undefined}
        hint={`${feedback.length} / ${FEEDBACK_MAX_CHARS}`}
      >
        <textarea
          id="cs-feedback"
          className="input"
          rows={4}
          required
          maxLength={FEEDBACK_MAX_CHARS}
          value={feedback}
          placeholder="e.g. Make the headline shorter and use more of our main brand colour."
          aria-invalid={error ? true : undefined}
          onChange={(event) => setFeedback(event.target.value)}
        />
      </Field>
      <div className="cs-subhead" id="cs-feedback-examples">
        Examples (click to add)
      </div>
      <div className="cs-chip-row" role="group" aria-labelledby="cs-feedback-examples">
        {FEEDBACK_EXAMPLES.map((example) => (
          <button key={example} type="button" className="cs-chip" disabled={busy} onClick={() => add(example)}>
            {example}
          </button>
        ))}
      </div>
    </Dialog>
  )
}

type CopyField = 'headline' | 'body' | 'cta'

function EditTextDialog({
  label,
  output,
  format,
  busy,
  errors,
  onSave,
  onCancel,
}: {
  label: string
  output: CreativeOutput
  format: CreativeFormat | null
  busy: boolean
  errors: Record<string, string>
  onSave: (fields: Record<string, string>) => void
  onCancel: () => void
}) {
  const limits = format?.limits
  const caps: Record<CopyField, number> = {
    headline: limits?.headline_chars ?? 70,
    body: limits?.body_chars ?? 1500,
    cta: limits?.cta_chars ?? 30,
  }
  const known = output.copy
  const [values, setValues] = useState<Record<CopyField, string>>({
    headline: known?.headline ?? '',
    body: known?.body ?? '',
    cta: known?.cta ?? '',
  })
  const fields: { key: CopyField; label: string; hint: string; multiline: boolean }[] = [
    { key: 'headline', label: 'Headline', hint: 'The big line people read first.', multiline: false },
    ...(caps.body > 0 ? [{ key: 'body' as const, label: 'Body text', hint: 'The supporting sentence or two.', multiline: true }] : []),
    { key: 'cta', label: 'Button text', hint: 'A short action, like “Shop now”.', multiline: false },
  ]
  const changed: Record<string, string> = {}
  for (const field of fields) {
    const text = values[field.key].trim()
    if (text && text !== (known?.[field.key] ?? '').trim()) changed[field.key] = text
  }
  const nothing = Object.keys(changed).length === 0
  return (
    <Dialog
      title={`Edit text: ${label}`}
      onClose={busy ? () => undefined : onCancel}
      width={560}
      actions={
        <>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" disabled={busy || nothing} onClick={() => onSave(changed)}>
            {busy ? 'Saving…' : 'Save and redraw'}
          </button>
        </>
      }
    >
      <p className="cs-note">
        {known
          ? 'Fix the wording yourself. No AI is used: we redraw the design with your text and check it again.'
          : 'Type only the parts you want to change; leave a box empty to keep the current wording. No AI is used.'}
      </p>
      {errors._ ? <p className="cs-callout is-danger">{errors._}</p> : null}
      {fields.map((field) => {
        const id = `cs-edit-${field.key}`
        const value = values[field.key]
        const props = {
          id,
          maxLength: caps[field.key],
          value,
          'aria-invalid': errors[field.key] ? true : undefined,
        }
        return (
          <Field
            key={field.key}
            label={field.label}
            htmlFor={id}
            error={errors[field.key]}
            hint={`${field.hint} ${value.length} / ${caps[field.key]}`}
          >
            {field.multiline ? (
              <textarea
                {...props}
                className="input"
                rows={3}
                onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
              />
            ) : (
              <input
                {...props}
                className="input"
                type="text"
                onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
              />
            )}
          </Field>
        )
      })}
    </Dialog>
  )
}

// ── Polling ────────────────────────────────────────────────────────────────
const FIRST_POLL_MS = 1500
const POLL_MS = 3000
const MAX_POLL_DELAY_MS = 15000
const MAX_POLL_MS = 20 * 60 * 1000
const MAX_POLL_FAILURES = 5

function mergeJobs(current: CreativeJob[], fresh: CreativeJob[]): CreativeJob[] {
  const byId = new Map(fresh.map((job) => [job.id, job]))
  return current.map((job) => byId.get(job.id) ?? job)
}

// ── Page ───────────────────────────────────────────────────────────────────
const EMPTY_MESSAGE: MessageState = { goal: '', audience: '', message: '', tone: '', extra: '' }

type DialogState = { kind: 'feedback' | 'edit'; jobId: string; output: CreativeOutput } | null

export function CreativeStudioPage() {
  const { canWrite } = useAuth()
  const toasts = useToasts()
  const canCreate = canWrite('content')
  const canReview = canWrite('approvals')
  const canEdit = canWrite('approvals') && canWrite('content')

  const options = useResource((signal) => creativeApi.options(signal), [], 'creative:options')
  const kits = useResource((signal) => brandKitApi.kits(signal), [], 'creative:kits')
  const products = useResource((signal) => brandKitApi.products(false, signal), [], 'creative:products')

  // Wizard
  const [step, setStep] = useState<StepIndex>(0)
  const [formats, setFormats] = useState<string[]>([])
  const [kitId, setKitId] = useState('')
  const [productId, setProductId] = useState('')
  const [message, setMessage] = useState<MessageState>(EMPTY_MESSAGE)
  const [textKey, setTextKey] = useState('')
  const [imageKey, setImageKey] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [stepError, setStepError] = useState('')
  const headingRef = useRef<HTMLHeadingElement>(null)
  const firstRender = useRef(true)

  // Generation
  const [jobs, setJobs] = useState<CreativeJob[]>([])
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const [slow, setSlow] = useState(false)
  const [pollProblem, setPollProblem] = useState('')
  const [pollAttempt, setPollAttempt] = useState(0)
  const [localStates, setLocalStates] = useState<Record<string, OutputState>>({})
  const [busyKey, setBusyKey] = useState('')
  const [dialog, setDialog] = useState<DialogState>(null)
  const [dialogErrors, setDialogErrors] = useState<Record<string, string>>({})
  const [waitingRevision, setWaitingRevision] = useState<{ parentId: string; jobId: string | null } | null>(null)
  const [share, setShare] = useState<{ assetId: string; label: string } | null>(null)

  const data = options.data
  const limits = data?.limits
  const picker = data?.models
  const formatById = useMemo(() => new Map((data?.formats ?? []).map((f) => [f.id, f])), [data])

  const quota = data?.quota ?? null
  const jobsLeft = quota ? remaining(quota.limits.jobs_per_month, quota.used.jobs) : null
  const imagesLeft = quota ? remaining(quota.limits.images_per_month, quota.used.images) : null

  const recommendedText = picker ? recommendedKey(picker.text, picker.recommended_text) : ''
  const recommendedImage = picker
    ? recommendedKey(picker.image, picker.recommended_image, (o) => o.key !== 'none' && imagesLeft === 0) || 'none'
    : ''

  // Preselect the recommended choices once they are known.
  useEffect(() => {
    if (!textKey && recommendedText) setTextKey(recommendedText)
  }, [recommendedText, textKey])
  useEffect(() => {
    if (!imageKey && recommendedImage) setImageKey(recommendedImage)
  }, [recommendedImage, imageKey])

  // The default kit is chosen for you.
  useEffect(() => {
    const list = kits.data
    if (!list || list.length === 0 || list.some((k) => k.id === kitId)) return
    setKitId((list.find((k) => k.is_default) ?? list[0])?.id ?? '')
  }, [kits.data, kitId])

  // Move focus to the step heading when the step changes, for keyboard and screen-reader users.
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    headingRef.current?.focus()
  }, [step])

  // ── Step rules ──
  const selectedText = picker?.text.find((o) => o.key === textKey && o.available) ?? null
  const selectedImage = picker?.image.find((o) => o.key === imageKey && o.available) ?? null

  const stepProblem = (index: StepIndex): string => {
    if (index === 0 && formats.length === 0) return 'Choose at least one size to continue.'
    if (index === 1 && !kits.data?.some((k) => k.id === kitId)) return 'Choose a brand kit to continue.'
    if (index === 2 && !message.message.trim()) return 'Write your key message to continue.'
    if (index === 3) {
      if (!selectedText) return picker?.text_ready === false ? 'Writing is not ready yet. See the note above.' : 'Choose who writes the words.'
      if (!selectedImage) return 'Choose where the picture comes from.'
    }
    return ''
  }

  const firstBlockedStep = (): StepIndex | null => {
    for (const index of [0, 1, 2, 3] as const) if (stepProblem(index)) return index
    return null
  }

  const canReach = (target: StepIndex): boolean => {
    for (let index = 0; index < target; index++) if (stepProblem(index as StepIndex)) return false
    return true
  }

  const goNext = () => {
    const problem = stepProblem(step)
    if (problem) {
      setStepError(problem)
      return
    }
    setStepError('')
    if (step < 4) setStep((step + 1) as StepIndex)
  }

  const goBack = () => {
    setStepError('')
    if (step > 0) setStep((step - 1) as StepIndex)
  }

  const toggleFormat = (id: string) => {
    setStepError('')
    setFormats((current) => {
      if (current.includes(id)) return current.filter((f) => f !== id)
      if (!limits || current.length >= limits.max_formats) return current
      return [...current, id]
    })
  }

  // ── Polling ──
  const pendingKey = jobs
    .filter((job) => !TERMINAL_STATUSES.has(job.status))
    .map((job) => job.id)
    .join(',')

  useEffect(() => {
    if (!pendingKey) return
    const ids = pendingKey.split(',').filter(isId)
    const controller = new AbortController()
    const started = Date.now()
    let timer = 0
    let delay = POLL_MS
    let failures = 0

    const tick = async () => {
      try {
        const fresh = await Promise.all(ids.map((id) => creativeApi.job(id, controller.signal)))
        if (controller.signal.aborted) return
        failures = 0
        setJobs((current) => mergeJobs(current, fresh))
        delay = Math.min(Math.round(delay * 1.3), MAX_POLL_DELAY_MS)
      } catch (caught) {
        if (controller.signal.aborted || isAbort(caught)) return
        failures += 1
        if (failures >= MAX_POLL_FAILURES) {
          setPollProblem(LOST_TOUCH)
          return
        }
        delay = Math.min(delay * 2, MAX_POLL_DELAY_MS)
      }
      if (Date.now() - started > MAX_POLL_MS) {
        setSlow(true)
        return
      }
      timer = window.setTimeout(() => void tick(), delay)
    }
    timer = window.setTimeout(() => void tick(), FIRST_POLL_MS)
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [pendingKey, pollAttempt])

  // After "Ask for changes", find the new job the server started for it.
  useEffect(() => {
    if (!waitingRevision) return
    const controller = new AbortController()
    let timer = 0
    let tries = 0
    const look = async () => {
      tries += 1
      try {
        const list = await creativeApi.jobs(controller.signal)
        if (controller.signal.aborted) return
        const known = new Set(jobs.map((j) => j.id))
        const child =
          (waitingRevision.jobId ? list.find((j) => j.id === waitingRevision.jobId) : undefined) ??
          list.find((j) => j.parent_job_id === waitingRevision.parentId && !known.has(j.id))
        if (child) {
          setJobs((current) => [...current, child])
          setWaitingRevision(null)
          return
        }
      } catch (caught) {
        if (controller.signal.aborted || isAbort(caught)) return
      }
      if (tries >= 10) {
        setWaitingRevision(null)
        toasts.push('Your new version is being made. It will appear in Approvals when it is ready.', 'info')
        return
      }
      timer = window.setTimeout(() => void look(), POLL_MS)
    }
    timer = window.setTimeout(() => void look(), FIRST_POLL_MS)
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
    // `jobs` is read for the ids known when the search starts; re-running on every poll would restart it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waitingRevision])

  /** Re-read one job after a review action. The action itself already succeeded, so a failure here is only a note. */
  const refreshJob = useCallback(
    async (id: string) => {
      try {
        const fresh = await creativeApi.job(id)
        setJobs((current) => mergeJobs(current, [fresh]))
      } catch (caught) {
        if (!isAbort(caught)) toasts.push('Saved. Refresh the page to see the latest preview.', 'info')
      }
    },
    [toasts],
  )

  // ── Actions ──
  const create = async () => {
    const blocked = firstBlockedStep()
    if (blocked !== null) {
      setStep(blocked)
      setStepError(stepProblem(blocked))
      return
    }
    if (!limits || !selectedText || !selectedImage) return
    const body: CreativeJobInput = {
      formats,
      brand_kit_id: kitId,
      product_id: isId(productId) ? productId : null,
      message: message.message.trim().slice(0, limits.message_chars),
      audience: message.audience.trim().slice(0, limits.audience_chars),
      notes: message.extra.trim().slice(0, limits.notes_chars),
      // "" means none (the brand voice for tone); the server expects text, never null.
      goal: message.goal.slice(0, limits.goal_chars),
      tone: message.tone.slice(0, limits.tone_chars),
      text_model: { connector: selectedText.key, model: null },
      image_model: { provider: selectedImage.key, model: null },
    }
    setCreating(true)
    setCreateError('')
    setFieldErrors({})
    setSlow(false)
    setPollProblem('')
    try {
      const job = await creativeApi.createJob(body)
      setJobs([job])
      setLocalStates({})
      void options.reload()
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 422 && caught.code === 'validation_error') {
        const errors: Record<string, string> = {}
        let target: StepIndex | null = null
        for (const [field, text] of Object.entries(caught.fields ?? {})) {
          // `reason` and `request_id` are machine keys, never shown.
          if (field === 'reason' || field === 'request_id' || typeof text !== 'string') continue
          const name = field.split('.')[0] ?? field
          if (!errors[name]) errors[name] = softenErrorMessage(text, 'Please check this')
          target = target ?? stepForField(field)
        }
        setFieldErrors(errors)
        if (target !== null) {
          setStep(target)
          setStepError('Some details need a fix. Check the messages below.')
          return
        }
      }
      if (caught instanceof ApiError && caught.status === 429) void options.reload()
      setCreateError(actionErrorMessage(caught, CREATE_FALLBACK))
    } finally {
      setCreating(false)
    }
  }

  const startOver = () => {
    setJobs([])
    setLocalStates({})
    setCreateError('')
    setSlow(false)
    setPollProblem('')
    setWaitingRevision(null)
    setFormats([])
    setMessage(EMPTY_MESSAGE)
    setFieldErrors({})
    setStep(0)
  }

  const approve = async (output: CreativeOutput, jobId: string) => {
    if (!output.approval_id) return
    setBusyKey(output.approval_id)
    try {
      const result = await api.approve(output.approval_id)
      toasts.fromResult(result)
      if (result.ok) setLocalStates((current) => ({ ...current, [output.approval_id as string]: 'approved' }))
      invalidateResourceCache('social:posts')
      await refreshJob(jobId)
    } catch (caught) {
      toasts.push(actionErrorMessage(caught, 'We could not approve this design. Please try again.'), 'error')
    } finally {
      setBusyKey('')
    }
  }

  const askForChanges = async (feedback: string) => {
    if (!dialog || !dialog.output.approval_id) return
    const { output, jobId } = dialog
    const approvalId = output.approval_id as string
    setBusyKey(approvalId)
    setDialogErrors({})
    try {
      const result = await api.requestChanges(approvalId, feedback)
      toasts.fromResult(result)
      if (!result.toast) toasts.push('Thanks. A new version is on its way.', 'success')
      setLocalStates((current) => ({ ...current, [approvalId]: 'replaced' }))
      setDialog(null)
      // The server names the job making the new version; the parent match is
      // only for a reply without one. Never guess by time: that could pick up
      // a teammate's job.
      setWaitingRevision({ parentId: jobId, jobId: result.job_id && isId(result.job_id) ? result.job_id : null })
      invalidateResourceCache('social:posts')
    } catch (caught) {
      setDialogErrors({ feedback: actionErrorMessage(caught, 'We could not send your feedback. Please try again.') })
    } finally {
      setBusyKey('')
    }
  }

  const saveText = async (fields: Record<string, string>) => {
    if (!dialog || !dialog.output.approval_id) return
    const { output, jobId } = dialog
    const approvalId = output.approval_id as string
    setBusyKey(approvalId)
    setDialogErrors({})
    try {
      const result = await api.editApprovalText(approvalId, fields)
      toasts.fromResult(result)
      if (!result.toast) toasts.push('Saved. The design has been redrawn with your text.', 'success')
      setDialog(null)
      invalidateResourceCache('social:posts')
      await refreshJob(jobId)
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 422 && Object.keys(caught.fields ?? {}).length > 0) {
        const errors: Record<string, string> = {}
        for (const [field, text] of Object.entries(caught.fields ?? {})) {
          if (field === 'reason' || field === 'request_id' || typeof text !== 'string') continue
          const name = field.split('.').pop() ?? field
          const keyName = name === 'headline' || name === 'body' || name === 'cta' ? name : '_'
          if (!errors[keyName]) errors[keyName] = softenErrorMessage(text, 'Please check this')
        }
        if (Object.keys(errors).length === 0) errors._ = actionErrorMessage(caught, 'Please check the text and try again.')
        setDialogErrors(errors)
      } else {
        setDialogErrors({
          _: actionErrorMessage(caught, 'We could not save your text. Please try again.'),
        })
      }
    } finally {
      setBusyKey('')
    }
  }

  const download = async (output: CreativeOutput, format: DownloadFormat) => {
    if (!output.asset_id) return
    setBusyKey(output.asset_id)
    try {
      const { blob, filename } = await creativeApi.download(output.asset_id, format)
      triggerDownload(blob, safeFilename(filename, `design-${output.format_id}.${format}`))
    } catch (caught) {
      toasts.push(actionErrorMessage(caught, 'We could not download that file. Please try again.', 'Approve this design first, then download it.', 'not_approved'), 'error')
    } finally {
      setBusyKey('')
    }
  }

  const downloadAll = async (job: CreativeJob) => {
    setBusyKey(job.id)
    try {
      const { blob, filename } = await creativeApi.downloadJob(job.id)
      triggerDownload(blob, safeFilename(filename, `designs-${job.id.slice(0, 8)}.zip`))
    } catch (caught) {
      toasts.push(
        actionErrorMessage(caught, 'We could not download your designs. Please try again.', 'Approve at least one design first, then download.', 'not_approved'),
        'error',
      )
    } finally {
      setBusyKey('')
    }
  }

  // ── Render ──
  const header = (
    <PageHeader
      title="Creative Studio"
      description="Make on-brand social posts, banners, email headers, flyers and brochures. Every design is checked and waits for your approval."
    />
  )

  if (options.loading && !data) {
    return (
      <>
        {header}
        <Loading label="Loading Creative Studio…" />
      </>
    )
  }
  if (!data) {
    return (
      <>
        {header}
        {options.forbidden ? (
          <EmptyState title="No access to Creative Studio" description={NO_PERMISSION} />
        ) : (
          <ErrorState message={options.error ?? NOT_AVAILABLE} onRetry={() => void options.reload()} />
        )}
      </>
    )
  }

  const activeLimits = data.limits
  const kitName = kits.data?.find((k) => k.id === kitId)?.name ?? ''
  const productName = products.data?.find((p) => p.id === productId)?.name ?? 'No specific product'
  const generating = jobs.length > 0
  const quotaText = quotaLine(quota)

  const renderStep = () => {
    switch (step) {
      case 0:
        return <FormatStep formats={data.formats} selected={formats} maxFormats={activeLimits.max_formats} onToggle={toggleFormat} />
      case 1:
        return (
          <BrandStep
            kits={kits.data}
            products={products.data}
            kitsError={kits.forbidden ? 'You need access to Brand Kit to choose a kit. Ask a workspace admin.' : kits.error}
            productsError={products.forbidden ? 'You need access to Brand Kit to choose a product.' : products.error}
            kitId={kitId}
            productId={productId}
            onKit={(id) => {
              setStepError('')
              setKitId(id)
            }}
            onProduct={setProductId}
            onRetry={() => {
              void kits.reload()
              void products.reload()
            }}
          />
        )
      case 2:
        return <MessageStep value={message} limits={activeLimits} errors={fieldErrors} onChange={(next) => {
          setStepError('')
          setMessage(next)
        }} />
      case 3:
        return (
          <AiStep
            picker={data.models}
            quota={quota}
            textKey={textKey}
            imageKey={imageKey}
            recommendedText={recommendedText}
            recommendedImage={recommendedImage}
            onText={(key) => {
              setStepError('')
              setTextKey(key)
            }}
            onImage={(key) => {
              setStepError('')
              setImageKey(key)
            }}
          />
        )
      case 4:
        return generating ? null : (
          <div className="cs-summary">
            <dl className="cs-summary-list">
              <dt>Sizes</dt>
              <dd>{formats.map((id) => formatById.get(id)?.label ?? id).join(', ')}</dd>
              <dt>Brand kit</dt>
              <dd>{kitName}</dd>
              <dt>Product</dt>
              <dd>{productName}</dd>
              <dt>Key message</dt>
              <dd className="cs-summary-text">{message.message.trim()}</dd>
              <dt>Words by</dt>
              <dd>{selectedText?.label ?? 'Not chosen'}</dd>
              <dt>Picture</dt>
              <dd>{selectedImage?.label ?? 'Not chosen'}</dd>
            </dl>
            <p className="cs-note">
              Designs always wait for your review here and in Approvals, whatever your “{REVIEW_SETTING_NAME}” setting.
            </p>
            {createError ? (
              <p className="cs-callout is-danger" role="alert">
                {createError}
              </p>
            ) : null}
            {jobsLeft === 0 ? (
              <p className="cs-callout is-warning" role="note">
                {QUOTA_MESSAGES.jobs_per_month}
              </p>
            ) : null}
            <button
              type="button"
              className="btn btn-primary"
              disabled={creating || !canCreate || jobsLeft === 0}
              onClick={() => void create()}
            >
              {creating ? 'Starting…' : formats.length === 1 ? 'Create my design' : `Create my ${formats.length} designs`}
            </button>
          </div>
        )
    }
  }

  const terminalJobs = jobs.filter((job) => TERMINAL_STATUSES.has(job.status))
  const runningJobs = jobs.filter((job) => !TERMINAL_STATUSES.has(job.status))

  return (
    <>
      {header}
      <div className="cs-topline">
        {quota ? (
          <ul className="cs-quota" aria-label="Monthly allowance">
            {quotaItems(quota).map((item) => (
              <li key={item.label} className="cs-quota-pill">
                <span className="cs-quota-label">{item.label}</span>
                <strong className="cs-quota-value">{item.value}</strong>
              </li>
            ))}
          </ul>
        ) : quotaText ? (
          <p className="cs-quota" aria-label="Monthly allowance">
            {quotaText}
          </p>
        ) : null}
        {!canCreate ? <p className="cs-callout is-warning">{NO_WRITE_NOTE}</p> : null}
      </div>

      {!generating ? (
        <nav aria-label="Creative Studio steps">
          <ol className="cs-stepper">
            {STEPS.map((title, index) => {
              const reachable = canReach(index as StepIndex)
              const done = index < step && !stepProblem(index as StepIndex)
              return (
                <li key={title} className={`cs-stepper-item${index === step ? ' is-current' : ''}${done ? ' is-done' : ''}`}>
                  <button
                    type="button"
                    className="cs-stepper-button"
                    aria-current={index === step ? 'step' : undefined}
                    disabled={!reachable}
                    onClick={() => {
                      setStepError('')
                      setStep(index as StepIndex)
                    }}
                  >
                    <span className="cs-stepper-mark" aria-hidden="true">
                      {done ? '✓' : index + 1}
                    </span>
                    <span className="cs-stepper-title">{title}</span>
                    {done ? <span className="sr-only"> (done)</span> : null}
                  </button>
                </li>
              )
            })}
          </ol>
        </nav>
      ) : null}

      <section className="card cs-panel" aria-labelledby="cs-step-heading">
        <h2 id="cs-step-heading" className="cs-step-heading" tabIndex={-1} ref={headingRef}>
          {generating ? 'Your designs' : `Step ${step + 1} of ${STEPS.length}: ${STEPS[step]}`}
        </h2>

        {generating ? (
          <div className="cs-results">
            {runningJobs.map((job) => (
              <JobProgress key={job.id} job={job} />
            ))}
            {waitingRevision ? (
              <p className="cs-note" role="status">
                Starting your new version…
              </p>
            ) : null}
            {slow ? <p className="cs-callout is-warning">{SLOW_NOTE}</p> : null}
            {pollProblem ? (
              <div className="cs-callout is-danger" role="alert">
                {pollProblem}{' '}
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => {
                    setPollProblem('')
                    setPollAttempt((n) => n + 1)
                  }}
                >
                  Check again
                </button>
              </div>
            ) : null}

            {terminalJobs.map((job, index) => (
              <div key={job.id} className="cs-job">
                {terminalJobs.length > 1 ? (
                  <h3 className="cs-job-title">{index === 0 ? 'First version' : `New version ${index + 1}`}</h3>
                ) : null}
                {job.status === 'failed' || job.status === 'cancelled' ? (
                  <p className="cs-callout is-danger" role="alert">
                    {job.status === 'cancelled'
                      ? 'This job was cancelled.'
                      : JOB_FAILURES[job.error_code] ?? (sentence(softenErrorMessage(job.error, '')) || JOB_FAILED_FALLBACK)}
                  </p>
                ) : null}
                {job.outputs.length > 0 ? (
                  <ul className="cs-output-grid">
                    {job.outputs.map((output, outputIndex) => {
                      const format = formatById.get(output.format_id) ?? null
                      const busy = busyKey !== '' && (busyKey === output.approval_id || busyKey === output.asset_id)
                      return (
                        <OutputCard
                          key={output.approval_id ?? output.asset_id ?? `${output.format_id}-${outputIndex}`}
                          output={output}
                          format={format}
                          state={outputState(output, localStates)}
                          busy={busy}
                          canReview={canReview}
                          canEdit={canEdit}
                          onApprove={() => void approve(output, job.id)}
                          onAskChanges={() => {
                            setDialogErrors({})
                            setDialog({ kind: 'feedback', jobId: job.id, output })
                          }}
                          onEditText={() => {
                            setDialogErrors({})
                            setDialog({ kind: 'edit', jobId: job.id, output })
                          }}
                          onDownload={(fmt) => void download(output, fmt)}
                          onShare={() => {
                            if (output.asset_id) setShare({ assetId: output.asset_id, label: format?.label ?? 'Design' })
                          }}
                        />
                      )
                    })}
                  </ul>
                ) : job.status === 'succeeded' ? (
                  <p className="cs-note">No designs came back from this job. Please try again.</p>
                ) : null}
                {job.outputs.some((output) => outputState(output, localStates) === 'approved' && output.asset_id !== null) ? (
                  <div className="cs-downloads">
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busyKey === job.id}
                      aria-busy={busyKey === job.id || undefined}
                      onClick={() => void downloadAll(job)}
                    >
                      Download all approved designs (ZIP)
                    </button>
                  </div>
                ) : null}
              </div>
            ))}

            <div className="cs-nav">
              <button type="button" className="btn btn-secondary" onClick={startOver}>
                Make another design
              </button>
              <Link className="btn btn-ghost" to="/approvals">
                Open Approvals
              </Link>
            </div>
          </div>
        ) : (
          <>
            {renderStep()}
            {stepError ? (
              <p className="cs-callout is-danger" role="alert">
                {stepError}
              </p>
            ) : null}
            <div className="cs-nav">
              <button type="button" className="btn btn-secondary" disabled={step === 0} onClick={goBack}>
                Back
              </button>
              {step < 4 ? (
                <button type="button" className="btn btn-primary" onClick={goNext}>
                  Next
                </button>
              ) : null}
            </div>
          </>
        )}
      </section>

      {dialog?.kind === 'feedback' ? (
        <FeedbackDialog
          label={formatById.get(dialog.output.format_id)?.label ?? 'Design'}
          busy={busyKey !== ''}
          error={dialogErrors.feedback ?? ''}
          onSend={(feedback) => void askForChanges(feedback)}
          onCancel={() => setDialog(null)}
        />
      ) : null}
      {dialog?.kind === 'edit' ? (
        <EditTextDialog
          label={formatById.get(dialog.output.format_id)?.label ?? 'Design'}
          output={dialog.output}
          format={formatById.get(dialog.output.format_id) ?? null}
          busy={busyKey !== ''}
          errors={dialogErrors}
          onSave={(fields) => void saveText(fields)}
          onCancel={() => setDialog(null)}
        />
      ) : null}
      {share ? (
        <ShareDialog creativeId={share.assetId} title={share.label} canWrite={canCreate} onClose={() => setShare(null)} />
      ) : null}
    </>
  )
}
