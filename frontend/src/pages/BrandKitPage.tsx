/**
 * Brand kit - the logos, colours, fonts and products every design uses.
 *
 * Two tabs: brand kits (logos, colours, fonts, voice and words to avoid) and
 * products (what a design is about and the only claims its copy may make).
 * Creative Studio picks one kit and one product for each design.
 *
 * Everything here is edited by people and saved only when they press Save:
 * "Fill from my website profile" shows suggestions to accept one by one. A
 * user without Brand Kit write access sees the same screens read-only, and
 * the server refuses their changes regardless.
 *
 * Images are fetched as authenticated blobs (`GET /files/{id}`) and shown
 * through object URLs, never through a URL that carries a token.
 *
 * Each kit and product has a "History" view: its numbered versions, a
 * comparison of any two, and restore for Brand Kit writers (the server checks
 * that too). Notes from a restore, such as photos that no longer exist, are
 * shown, and the list is reloaded so the card shows the restored version.
 */
import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { Link } from 'react-router-dom'

import { ApiError } from '@/api/client'
import {
  brandKitApi,
  checkUpload,
  isId,
  UPLOAD_ACCEPT,
  type BrandKit,
  type BrandKitInput,
  type BrandKitLimits,
  type BrandKitPrefill,
  type FontOption,
  type Product,
  type ProductInput,
  type UploadKind,
} from '@/api/brandKit'
import type { VersionKind } from '@/api/history'
import { useAuth } from '@/auth/AuthContext'
import { VersionHistory } from '@/components/AuditHistory'
import { PageTabs } from '@/components/PageTabs'
import { useToasts } from '@/components/Toasts'
import { Dialog, Drawer, EmptyState, ErrorState, Field, FormSection, Loading, PageHeader, Tag } from '@/components/ui'
import { friendlyLoadError, useResource } from '@/hooks/useResource'
import { softenErrorMessage } from '@/lib/softenError'
import '@/styles/brand-kit.css'

// ── Copy ───────────────────────────────────────────────────────────────────
const UPLOAD_RULES = 'PNG, JPG, WebP or SVG, up to 5 MB (SVG up to 1 MB).'
const UPLOAD_FALLBACK = 'We could not upload that image. Please try again.'
const TOO_BIG = 'That file is too big. Please upload an image under 5 MB.'
const CHECK_FIELDS = 'Some fields need a fix. Check the messages under them.'
const READ_ONLY_NOTE =
  'You can look at the brand kit, but not change it. Ask a workspace admin if you need edit access.'

const DEFAULT_LIMITS: Required<BrandKitLimits> = {
  kits: 20,
  products: 500,
  kit_name_chars: 120,
  voice_chars: 500,
  disclaimer_chars: 500,
  banned_phrases: 200,
  banned_phrase_chars: 100,
  product_name_chars: 200,
  description_chars: 2000,
  price_text_chars: 64,
  approved_claims: 20,
  claim_chars: 200,
  product_images: 10,
}

const COLOUR_FIELDS: { key: ColourKey; label: string; hint: string }[] = [
  { key: 'primary_color', label: 'Main colour', hint: 'Your strongest brand colour, used for headlines and shapes.' },
  { key: 'secondary_color', label: 'Second colour', hint: 'Supports the main colour, for example in panels.' },
  { key: 'accent_color', label: 'Highlight colour', hint: 'For buttons and things that should stand out.' },
  { key: 'text_color', label: 'Text colour', hint: 'Most writing on a design uses this.' },
  { key: 'background_color', label: 'Background colour', hint: 'Behind the text when there is no photo.' },
]

type ColourKey = 'primary_color' | 'secondary_color' | 'accent_color' | 'text_color' | 'background_color'

const NEW_KIT: BrandKitInput = {
  name: '',
  logo_file_id: null,
  logo_dark_file_id: null,
  primary_color: '#1f2937',
  secondary_color: '#4b5563',
  accent_color: '#2563eb',
  text_color: '#111827',
  background_color: '#ffffff',
  heading_font: 'inter',
  body_font: 'inter',
  voice: '',
  banned_phrases: [],
  disclaimer: '',
}

const NEW_PRODUCT: ProductInput = {
  name: '',
  description: '',
  url: '',
  price_text: '',
  approved_claims: [],
  image_file_ids: [],
  disclaimer: '',
}

// ── Small helpers ──────────────────────────────────────────────────────────
function lines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

function limitsFrom(limits: BrandKitLimits | undefined): Required<BrandKitLimits> {
  return { ...DEFAULT_LIMITS, ...(limits ?? {}) }
}

/** Inline messages for a rejected form, keyed by field; null if it was not a field problem. */
function fieldErrorsFrom(caught: unknown): Record<string, string> | null {
  if (!(caught instanceof ApiError) || caught.status !== 422 || caught.code !== 'validation_error') return null
  const out: Record<string, string> = {}
  for (const [key, message] of Object.entries(caught.fields ?? {})) {
    // "banned_phrases.3" and "banned_phrases" both belong to the list field.
    const field = key.split('.')[0] ?? key
    if (!out[field]) out[field] = softenErrorMessage(String(message), 'Please check this field')
  }
  return Object.keys(out).length > 0 ? out : null
}

/**
 * A plain message for a failed upload. The server's own sentence is used (it
 * says what was wrong with the file); its `fields` hold a machine reason key,
 * which is never shown.
 */
function uploadErrorMessage(caught: unknown): string {
  if (caught instanceof ApiError) {
    if (caught.status === 413) return TOO_BIG
    if (caught.isForbidden) return 'You do not have permission to upload brand images.'
    return softenErrorMessage(caught.message, UPLOAD_FALLBACK)
  }
  if (caught instanceof Error && caught.name === 'AbortError') return UPLOAD_FALLBACK
  return friendlyLoadError(caught)
}

/** `#abc` or `abc` become `#aabbcc`; anything that is not a hex colour gives null. */
function normaliseHex(value: string): string | null {
  const text = value.trim().replace(/^#/, '').toLowerCase()
  if (/^[0-9a-f]{6}$/.test(text)) return `#${text}`
  if (/^[0-9a-f]{3}$/.test(text)) return `#${text.replace(/./g, (c) => c + c)}`
  return null
}

function luminance(hex: string): number {
  const channel = (start: number) => {
    const value = parseInt(hex.slice(start, start + 2), 16) / 255
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)
}

/** The WCAG contrast ratio of two `#rrggbb` colours (1 to 21). */
function contrastRatio(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number]
  return (light + 0.05) / (dark + 0.05)
}

// ── Images ─────────────────────────────────────────────────────────────────
/** An uploaded image as an object URL, fetched with the session's auth (same pattern as ContentPage). */
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
    <span className={`${className} bk-image-empty`} role={alt ? 'img' : undefined} aria-label={alt || undefined}>
      {failed ? 'Image unavailable' : id ? 'Loading…' : 'No image'}
    </span>
  )
}

// ── Confirm (uses Dialog for its focus trap) ───────────────────────────────
function ConfirmDialog({
  title,
  message,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string
  message: string
  confirmLabel: string
  busy: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Dialog
      title={title}
      onClose={busy ? () => undefined : onCancel}
      width={440}
      actions={
        <>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onCancel} autoFocus>
            Cancel
          </button>
          <button type="button" className="btn btn-primary bk-danger" disabled={busy} onClick={onConfirm}>
            {busy ? 'Working…' : confirmLabel}
          </button>
        </>
      }
    >
      <p className="bk-confirm-text">{message}</p>
    </Dialog>
  )
}

// ── History ────────────────────────────────────────────────────────────────
/** Which kit or product's history is open; one at a time per tab. */
type HistoryTarget = { id: string; name: string }

/** Opens one item's history and brings it into view. */
function useHistoryTarget(): {
  target: HistoryTarget | null
  open: (item: HistoryTarget) => void
  close: () => void
  ref: RefObject<HTMLDivElement>
} {
  const [target, setTarget] = useState<HistoryTarget | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (target) ref.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [target])
  return { target, open: setTarget, close: () => setTarget(null), ref }
}

/**
 * Version history for one kit or product. Restore notes (files left out) are
 * shown by the history kit itself; `onRestored` reloads the list.
 */
function EntityHistory({
  kind,
  target,
  panelRef,
  onClose,
  onRestored,
}: {
  kind: Extract<VersionKind, 'brand_kit' | 'product'>
  target: HistoryTarget
  panelRef: RefObject<HTMLDivElement>
  onClose: () => void
  onRestored: () => Promise<void>
}) {
  const what = kind === 'brand_kit' ? 'brand kit' : 'product'
  return (
    <div ref={panelRef} style={{ marginTop: 16 }}>
      <div className="bk-toolbar">
        <p className="bk-toolbar-text">
          History of "{target.name}". A version is kept each time this {what} is saved.
        </p>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
          Close history
        </button>
      </div>
      <VersionHistory
        key={target.id}
        kind={kind}
        subject={target.id}
        title={`${kind === 'brand_kit' ? 'Brand kit' : 'Product'} history`}
        onRestored={() => {
          void onRestored()
        }}
      />
    </div>
  )
}

// ── Page ───────────────────────────────────────────────────────────────────
export function BrandKitPage() {
  const { canWrite } = useAuth()
  const writable = canWrite('brand_kit')
  const [view, setView] = useState<'kits' | 'products'>('kits')
  const options = useResource((signal) => brandKitApi.options(signal), [], 'brand-kit:options')
  const fonts = options.data?.fonts ?? []
  const limits = limitsFrom(options.data?.limits)

  return (
    <>
      <PageHeader
        title="Brand kit"
        description="Your logos, colours, fonts, voice and products. Creative Studio uses them so every design looks and sounds like you."
      />
      {!writable ? <div className="banner is-info bk-banner">{READ_ONLY_NOTE}</div> : null}
      <PageTabs
        value={view}
        options={[
          { value: 'kits', label: 'Brand kits' },
          { value: 'products', label: 'Products' },
        ]}
        onChange={setView}
      />
      {view === 'kits' ? (
        <KitsTab writable={writable} fonts={fonts} limits={limits} />
      ) : (
        <ProductsTab writable={writable} limits={limits} />
      )}
    </>
  )
}

// ── Prefill ────────────────────────────────────────────────────────────────
/** Loads website-profile suggestions on request. Writers only (the endpoint needs write access). */
function usePrefill() {
  const { fromError } = useToasts()
  const [prefill, setPrefill] = useState<BrandKitPrefill | null>(null)
  const [loading, setLoading] = useState(false)
  const load = async () => {
    setLoading(true)
    try {
      setPrefill(await brandKitApi.prefill())
    } catch (caught) {
      fromError(caught, 'We could not load suggestions from your website profile. Please try again.')
    } finally {
      setLoading(false)
    }
  }
  return { prefill, loading, load }
}

function NoProfileNote() {
  return (
    <p className="bk-suggest-empty">
      There is no website profile yet. Create one on the <Link to="/brand">Brand profile</Link> page, then come back.
    </p>
  )
}

function Suggestion({ label, value, onUse, used }: { label: string; value: string; onUse: () => void; used: boolean }) {
  return (
    <li className="bk-suggest-item">
      <div className="bk-suggest-text">
        <span className="bk-suggest-label">{label}</span>
        <span>{value}</span>
      </div>
      <button type="button" className="btn btn-secondary btn-sm" onClick={onUse} disabled={used}>
        {used ? 'Added' : 'Use this'}
      </button>
    </li>
  )
}

// ── Kits ───────────────────────────────────────────────────────────────────
function KitsTab({ writable, fonts, limits }: { writable: boolean; fonts: FontOption[]; limits: Required<BrandKitLimits> }) {
  const { push, fromError, fromResult } = useToasts()
  const { data, loading, error, reload } = useResource((signal) => brandKitApi.kits(signal), [], 'brand-kit:kits')
  const [editing, setEditing] = useState<BrandKit | 'new' | null>(null)
  const [deleting, setDeleting] = useState<BrandKit | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const history = useHistoryTarget()

  const makeDefault = async (kit: BrandKit) => {
    setBusyId(kit.id)
    try {
      await brandKitApi.makeDefaultKit(kit.id)
      push(`"${kit.name}" is now your default brand kit`, 'success')
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  const remove = async () => {
    if (!deleting) return
    setBusyId(deleting.id)
    try {
      fromResult(await brandKitApi.deleteKit(deleting.id))
      if (history.target?.id === deleting.id) history.close()
      setDeleting(null)
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  if (loading && !data) return <Loading label="Loading brand kits…" />
  if (error && !data) return <ErrorState message={error} onRetry={() => void reload()} />
  const kits = data ?? []
  const full = kits.length >= limits.kits

  return (
    <section className="bk-section" aria-label="Brand kits">
      <div className="bk-toolbar">
        <p className="bk-toolbar-text">
          Most businesses need one kit. Add more for sub-brands or seasonal looks. The default kit is picked first in
          Creative Studio.
        </p>
        {writable ? (
          <button type="button" className="btn btn-primary" disabled={full} onClick={() => setEditing('new')}>
            New brand kit
          </button>
        ) : null}
      </div>
      {full && writable ? (
        <p className="bk-note">You have reached the limit of {limits.kits} brand kits. Delete one to add another.</p>
      ) : null}

      {kits.length === 0 ? (
        <EmptyState
          title="No brand kits yet"
          description={
            writable
              ? 'Create a brand kit with your logo, colours and fonts so every design matches your brand.'
              : 'No one has set up a brand kit for this workspace yet.'
          }
        />
      ) : (
        <ul className="bk-list">
          {kits.map((kit) => (
            <li key={kit.id} className="panel bk-card">
              <FileImage id={kit.logo_file_id} alt={`${kit.name} logo`} className="bk-card-logo" />
              <div className="bk-card-main">
                <div className="bk-card-title">
                  <span className="bk-card-name">{kit.name}</span>
                  {kit.is_default ? <Tag tone="accent-2">Default</Tag> : null}
                </div>
                <div className="bk-swatches" aria-label="Brand colours">
                  {COLOUR_FIELDS.map((field) => (
                    <span
                      key={field.key}
                      className="bk-swatch"
                      style={{ background: kit[field.key] }}
                      title={`${field.label}: ${kit[field.key]}`}
                    />
                  ))}
                </div>
                <div className="bk-card-meta">
                  Fonts: {fontLabel(fonts, kit.heading_font)} / {fontLabel(fonts, kit.body_font)}
                </div>
              </div>
              <div className="bk-card-actions">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setEditing(kit)}>
                  {writable ? 'Edit' : 'View'}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  aria-label={`History of ${kit.name}`}
                  onClick={() => history.open({ id: kit.id, name: kit.name })}
                >
                  History
                </button>
                {writable && !kit.is_default ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    disabled={busyId !== null}
                    onClick={() => void makeDefault(kit)}
                  >
                    {busyId === kit.id ? 'Saving…' : 'Make default'}
                  </button>
                ) : null}
                {writable ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm bk-danger-text"
                    disabled={busyId !== null}
                    onClick={() => setDeleting(kit)}
                  >
                    Delete
                  </button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}

      {history.target ? (
        <EntityHistory
          kind="brand_kit"
          target={history.target}
          panelRef={history.ref}
          onClose={history.close}
          onRestored={reload}
        />
      ) : null}

      {editing ? (
        <KitEditor
          kit={editing === 'new' ? null : editing}
          writable={writable}
          fonts={fonts}
          limits={limits}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await reload()
          }}
        />
      ) : null}

      {deleting ? (
        <ConfirmDialog
          title="Delete this brand kit?"
          message={`"${deleting.name}" will be removed for everyone in your workspace. Designs already made keep their look. This cannot be undone.${
            deleting.is_default ? ' It is your default kit, so choose a new default afterwards.' : ''
          }`}
          confirmLabel="Delete brand kit"
          busy={busyId === deleting.id}
          onConfirm={() => void remove()}
          onCancel={() => setDeleting(null)}
        />
      ) : null}
    </section>
  )
}

function fontLabel(fonts: FontOption[], id: string): string {
  return fonts.find((font) => font.id === id)?.label ?? id
}

function kitToInput(kit: BrandKit | null): BrandKitInput {
  if (!kit) return { ...NEW_KIT }
  return {
    name: kit.name,
    logo_file_id: kit.logo_file_id,
    logo_dark_file_id: kit.logo_dark_file_id,
    primary_color: kit.primary_color,
    secondary_color: kit.secondary_color,
    accent_color: kit.accent_color,
    text_color: kit.text_color,
    background_color: kit.background_color,
    heading_font: kit.heading_font,
    body_font: kit.body_font,
    voice: kit.voice,
    banned_phrases: kit.banned_phrases,
    disclaimer: kit.disclaimer,
  }
}

function KitEditor({
  kit,
  writable,
  fonts,
  limits,
  onClose,
  onSaved,
}: {
  kit: BrandKit | null
  writable: boolean
  fonts: FontOption[]
  limits: Required<BrandKitLimits>
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { push, fromError } = useToasts()
  const [draft, setDraft] = useState<BrandKitInput>(() => kitToInput(kit))
  const [banned, setBanned] = useState(() => (kit?.banned_phrases ?? []).join('\n'))
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [badColours, setBadColours] = useState<Set<ColourKey>>(() => new Set())
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(0)
  const { prefill, loading: prefillLoading, load: loadPrefill } = usePrefill()

  const set = <K extends keyof BrandKitInput>(key: K, value: BrandKitInput[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    setErrors((current) => {
      if (!current[key]) return current
      const next = { ...current }
      delete next[key]
      return next
    })
  }

  const setColourValid = (key: ColourKey, valid: boolean) =>
    setBadColours((current) => {
      if (valid === !current.has(key)) return current
      const next = new Set(current)
      if (valid) next.delete(key)
      else next.add(key)
      return next
    })

  const save = async () => {
    if (!draft.name.trim()) {
      setErrors((current) => ({ ...current, name: 'Please give this kit a name' }))
      push(CHECK_FIELDS, 'warning')
      return
    }
    if (badColours.size > 0) {
      push('One of the colours is not a valid colour code. Use a code like #1a2b3c.', 'warning')
      return
    }
    const body: BrandKitInput = { ...draft, name: draft.name.trim(), banned_phrases: lines(banned) }
    setSaving(true)
    try {
      if (kit) await brandKitApi.updateKit(kit.id, body)
      else await brandKitApi.createKit(body)
      push(kit ? 'Brand kit saved' : 'Brand kit created', 'success')
      await onSaved()
    } catch (caught) {
      const fields = fieldErrorsFrom(caught)
      if (fields) {
        setErrors(fields)
        push(CHECK_FIELDS, 'warning')
      } else {
        fromError(caught)
      }
    } finally {
      setSaving(false)
    }
  }

  const title = kit ? (writable ? `Edit ${kit.name}` : kit.name) : 'New brand kit'
  const prefillKit = prefill?.kit ?? null

  return (
    <Drawer
      title={title}
      subtitle={writable ? 'Nothing is saved until you press Save.' : 'View only'}
      width={680}
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {writable ? 'Cancel' : 'Close'}
          </button>
          {writable ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={saving || uploading > 0}
              onClick={() => void save()}
            >
              {saving ? 'Saving…' : kit ? 'Save changes' : 'Create brand kit'}
            </button>
          ) : null}
        </>
      }
    >
      {writable ? (
        <div className="bk-suggest">
          <div className="bk-suggest-head">
            <div>
              <div className="bk-suggest-title">Fill from my website profile</div>
              <div className="bk-suggest-hint">
                See suggestions from your brand profile and pick the ones you want. Nothing changes until you save.
              </div>
            </div>
            <button type="button" className="btn btn-secondary btn-sm" disabled={prefillLoading} onClick={() => void loadPrefill()}>
              {prefillLoading ? 'Loading…' : prefill ? 'Refresh suggestions' : 'Show suggestions'}
            </button>
          </div>
          {prefill && !prefill.available ? <NoProfileNote /> : null}
          {prefill?.available && prefillKit ? (
            <ul className="bk-suggest-list">
              {prefillKit.name ? (
                <Suggestion
                  label="Name"
                  value={prefillKit.name}
                  used={draft.name === prefillKit.name}
                  onUse={() => set('name', prefillKit.name.slice(0, limits.kit_name_chars))}
                />
              ) : null}
              {prefillKit.voice ? (
                <Suggestion
                  label="Voice and tone"
                  value={prefillKit.voice}
                  used={draft.voice === prefillKit.voice}
                  onUse={() => set('voice', prefillKit.voice.slice(0, limits.voice_chars))}
                />
              ) : null}
              {!prefillKit.name && !prefillKit.voice ? (
                <li className="bk-suggest-empty">Your website profile has nothing to suggest for a brand kit.</li>
              ) : null}
            </ul>
          ) : null}
        </div>
      ) : null}

      <FormSection title="Name">
        <Field label="Kit name" htmlFor="bk-name" required error={errors.name} hint="For example “Main brand” or “Summer sale”.">
          <input
            id="bk-name"
            className={`input${errors.name ? ' is-invalid' : ''}`}
            value={draft.name}
            maxLength={limits.kit_name_chars}
            disabled={!writable}
            required
            aria-invalid={errors.name ? true : undefined}
            onChange={(event) => set('name', event.target.value)}
          />
        </Field>
      </FormSection>

      <FormSection title="Logos" description={`Upload your logo for light and dark backgrounds. ${UPLOAD_RULES}`}>
        <div className="bk-logo-grid">
          <LogoField
            id="bk-logo"
            label="Logo for light backgrounds"
            fileId={draft.logo_file_id}
            dark={false}
            writable={writable}
            error={errors.logo_file_id}
            onUploading={(on) => setUploading((n) => n + (on ? 1 : -1))}
            onChange={(id) => set('logo_file_id', id)}
          />
          <LogoField
            id="bk-logo-dark"
            label="Logo for dark backgrounds"
            fileId={draft.logo_dark_file_id}
            dark
            writable={writable}
            error={errors.logo_dark_file_id}
            onUploading={(on) => setUploading((n) => n + (on ? 1 : -1))}
            onChange={(id) => set('logo_dark_file_id', id)}
          />
        </div>
      </FormSection>

      <FormSection title="Colours" description="Pick a colour or type its code, for example #1a2b3c.">
        <div className="bk-colour-grid">
          {COLOUR_FIELDS.map((field) => (
            <ColourField
              key={field.key}
              id={`bk-${field.key}`}
              label={field.label}
              hint={field.hint}
              value={draft[field.key]}
              error={errors[field.key]}
              writable={writable}
              onChange={(value) => set(field.key, value)}
              onValidity={(valid) => setColourValid(field.key, valid)}
            />
          ))}
        </div>
        <div className="bk-contrast" aria-live="polite">
          <ContrastHint label="Text on background" foreground={draft.text_color} background={draft.background_color} />
          <ContrastHint label="Background-coloured text on main colour" foreground={draft.background_color} background={draft.primary_color} />
        </div>
      </FormSection>

      <FormSection title="Fonts" description="Only fonts the studio can draw are listed.">
        <div className="bk-two">
          <FontField
            id="bk-heading-font"
            label="Headline font"
            value={draft.heading_font}
            fonts={fonts}
            error={errors.heading_font}
            writable={writable}
            onChange={(value) => set('heading_font', value)}
          />
          <FontField
            id="bk-body-font"
            label="Body text font"
            value={draft.body_font}
            fonts={fonts}
            error={errors.body_font}
            writable={writable}
            onChange={(value) => set('body_font', value)}
          />
        </div>
      </FormSection>

      <FormSection title="Voice and words" description="The AI follows these when it writes the words on a design.">
        <Field
          label="Voice and tone"
          htmlFor="bk-voice"
          error={errors.voice}
          hint={`How your copy should sound, for example “friendly and plain-spoken, no jargon”. Up to ${limits.voice_chars} characters.`}
        >
          <textarea
            id="bk-voice"
            className={`input${errors.voice ? ' is-invalid' : ''}`}
            rows={3}
            maxLength={limits.voice_chars}
            value={draft.voice}
            disabled={!writable}
            aria-invalid={errors.voice ? true : undefined}
            onChange={(event) => set('voice', event.target.value)}
          />
        </Field>
        <Field
          label="Words to avoid"
          htmlFor="bk-banned"
          error={errors.banned_phrases}
          hint={`One word or phrase per line, for example “cheapest” or “guaranteed results”. Designs that use them are flagged. Up to ${limits.banned_phrases} lines.`}
        >
          <textarea
            id="bk-banned"
            className={`input${errors.banned_phrases ? ' is-invalid' : ''}`}
            rows={4}
            value={banned}
            disabled={!writable}
            aria-invalid={errors.banned_phrases ? true : undefined}
            onChange={(event) => {
              setBanned(event.target.value)
              set('banned_phrases', lines(event.target.value))
            }}
          />
        </Field>
        <Field
          label="Disclaimer"
          htmlFor="bk-disclaimer"
          error={errors.disclaimer}
          hint={`Small print to include on designs, for example “Terms apply. See website for details.” Leave empty if you do not need one. Up to ${limits.disclaimer_chars} characters.`}
        >
          <textarea
            id="bk-disclaimer"
            className={`input${errors.disclaimer ? ' is-invalid' : ''}`}
            rows={2}
            maxLength={limits.disclaimer_chars}
            value={draft.disclaimer}
            disabled={!writable}
            aria-invalid={errors.disclaimer ? true : undefined}
            onChange={(event) => set('disclaimer', event.target.value)}
          />
        </Field>
      </FormSection>
    </Drawer>
  )
}

function ColourField({
  id,
  label,
  hint,
  value,
  error,
  writable,
  onChange,
  onValidity,
}: {
  id: string
  label: string
  hint: string
  value: string
  error?: string
  writable: boolean
  onChange: (value: string) => void
  onValidity: (valid: boolean) => void
}) {
  const [text, setText] = useState(value)
  const [invalid, setInvalid] = useState(false)

  // Follow outside changes (the picker, a reset) without fighting the typing.
  useEffect(() => {
    setText((current) => (normaliseHex(current) === value ? current : value))
  }, [value])

  const typed = (next: string) => {
    setText(next)
    const hex = normaliseHex(next)
    setInvalid(hex === null)
    onValidity(hex !== null)
    if (hex) onChange(hex)
  }

  const message = invalid ? 'Use a colour code like #1a2b3c' : error
  return (
    <Field label={label} htmlFor={id} hint={hint} error={message}>
      <div className="bk-colour">
        <input
          type="color"
          className="bk-colour-picker"
          value={value}
          disabled={!writable}
          aria-label={`${label} picker`}
          onChange={(event) => typed(event.target.value)}
        />
        <input
          id={id}
          className={`input bk-colour-code${message ? ' is-invalid' : ''}`}
          value={text}
          maxLength={7}
          spellCheck={false}
          autoComplete="off"
          disabled={!writable}
          aria-invalid={message ? true : undefined}
          onChange={(event) => typed(event.target.value)}
          onBlur={() => {
            const hex = normaliseHex(text)
            if (hex) setText(hex)
          }}
        />
      </div>
    </Field>
  )
}

function ContrastHint({ label, foreground, background }: { label: string; foreground: string; background: string }) {
  const ratio = contrastRatio(foreground, background)
  const verdict = ratio >= 4.5 ? 'easy to read' : ratio >= 3 ? 'fine for big headlines only' : 'hard to read'
  const tone = ratio >= 4.5 ? 'is-good' : ratio >= 3 ? 'is-ok' : 'is-bad'
  return (
    <div className={`bk-contrast-row ${tone}`}>
      <span className="bk-contrast-sample" style={{ color: foreground, background }} aria-hidden="true">
        Aa
      </span>
      <span>
        {label}: <strong>{verdict}</strong>
        <span className="bk-contrast-ratio"> (contrast {ratio.toFixed(1)} to 1)</span>
      </span>
    </div>
  )
}

function FontField({
  id,
  label,
  value,
  fonts,
  error,
  writable,
  onChange,
}: {
  id: string
  label: string
  value: string
  fonts: FontOption[]
  error?: string
  writable: boolean
  onChange: (value: string) => void
}) {
  const known = fonts.some((font) => font.id === value)
  return (
    <Field label={label} htmlFor={id} error={error}>
      <select
        id={id}
        className={`input${error ? ' is-invalid' : ''}`}
        value={value}
        disabled={!writable || fonts.length === 0}
        onChange={(event) => onChange(event.target.value)}
      >
        {!known ? <option value={value}>{value}</option> : null}
        {fonts.map((font) => (
          <option key={font.id} value={font.id}>
            {font.label}
          </option>
        ))}
      </select>
    </Field>
  )
}

/** Shared upload control: a labelled file input, the rules, and the server's message on failure. */
function UploadButton({
  id,
  label,
  accessibleName,
  kind,
  disabled,
  onUploaded,
  onUploading,
}: {
  id: string
  label: string
  /** What the file input is for, when the visible label alone is ambiguous. */
  accessibleName?: string
  kind: UploadKind
  disabled: boolean
  onUploaded: (fileId: string) => void
  onUploading?: (on: boolean) => void
}) {
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const choose = async (file: File | undefined) => {
    if (inputRef.current) inputRef.current.value = ''
    if (!file) return
    const local = checkUpload(file)
    if (local) {
      setProblem(local)
      return
    }
    setProblem('')
    setBusy(true)
    onUploading?.(true)
    try {
      const stored = await brandKitApi.upload(kind, file)
      if (!isId(stored?.id)) throw new Error('bad reply')
      onUploaded(stored.id)
    } catch (caught) {
      setProblem(uploadErrorMessage(caught))
    } finally {
      setBusy(false)
      onUploading?.(false)
    }
  }

  return (
    <div className="bk-upload">
      <label htmlFor={id} className={`btn btn-secondary btn-sm bk-upload-btn${disabled || busy ? ' is-disabled' : ''}`}>
        {busy ? 'Uploading…' : label}
      </label>
      <input
        ref={inputRef}
        id={id}
        type="file"
        className="bk-file-input"
        accept={UPLOAD_ACCEPT}
        disabled={disabled || busy}
        aria-label={accessibleName}
        aria-describedby={`${id}-rules${problem ? ` ${id}-problem` : ''}`}
        onChange={(event) => void choose(event.target.files?.[0])}
      />
      <div id={`${id}-rules`} className="field-hint">
        {UPLOAD_RULES}
      </div>
      {problem ? (
        <div id={`${id}-problem`} className="field-error" role="alert">
          {problem}
        </div>
      ) : null}
    </div>
  )
}

function LogoField({
  id,
  label,
  fileId,
  dark,
  writable,
  error,
  onChange,
  onUploading,
}: {
  id: string
  label: string
  fileId: string | null
  dark: boolean
  writable: boolean
  error?: string
  onChange: (fileId: string | null) => void
  onUploading: (on: boolean) => void
}) {
  return (
    <div className="bk-logo">
      <div className="bk-logo-label" id={`${id}-label`}>
        {label}
      </div>
      <div className={`bk-logo-preview${dark ? ' is-dark' : ''}`}>
        <FileImage id={fileId} alt={fileId ? label : ''} className="bk-logo-img" />
      </div>
      {writable ? (
        <div className="bk-logo-actions">
          <UploadButton
            id={id}
            label={fileId ? 'Replace logo' : 'Upload logo'}
            accessibleName={`${fileId ? 'Replace' : 'Upload'} ${label.toLowerCase()}`}
            kind="logo"
            disabled={false}
            onUploaded={(newId) => onChange(newId)}
            onUploading={onUploading}
          />
          {fileId ? (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => onChange(null)}>
              Remove logo
            </button>
          ) : null}
        </div>
      ) : null}
      {error ? <div className="field-error">{error}</div> : null}
    </div>
  )
}

// ── Products ───────────────────────────────────────────────────────────────
type ProductDraftSeed = { product: Product | null; seedName?: string }

function ProductsTab({ writable, limits }: { writable: boolean; limits: Required<BrandKitLimits> }) {
  const { push, fromError, fromResult } = useToasts()
  const [showArchived, setShowArchived] = useState(false)
  const { data, loading, error, reload } = useResource(
    (signal) => brandKitApi.products(showArchived, signal),
    [showArchived],
    `brand-kit:products:${showArchived ? 'all' : 'active'}`,
  )
  const [editing, setEditing] = useState<ProductDraftSeed | null>(null)
  const [archiving, setArchiving] = useState<Product | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const history = useHistoryTarget()
  const { prefill, loading: prefillLoading, load: loadPrefill } = usePrefill()

  const archive = async () => {
    if (!archiving) return
    setBusyId(archiving.id)
    try {
      fromResult(await brandKitApi.archiveProduct(archiving.id))
      setArchiving(null)
      setEditing(null)
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  const restore = async (product: Product) => {
    setBusyId(product.id)
    try {
      await brandKitApi.updateProduct(product.id, { is_archived: false })
      push(`"${product.name}" is back in your products`, 'success')
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusyId(null)
    }
  }

  const products = data ?? []
  const existingNames = new Set(products.map((p) => p.name.toLowerCase()))

  return (
    <section className="bk-section" aria-label="Products">
      <div className="bk-toolbar">
        <label className="checkbox bk-archived-toggle">
          <input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />
          <span>Show archived products</span>
        </label>
        {writable ? (
          <button type="button" className="btn btn-primary" onClick={() => setEditing({ product: null })}>
            Add product
          </button>
        ) : null}
      </div>

      {writable ? (
        <div className="bk-suggest">
          <div className="bk-suggest-head">
            <div>
              <div className="bk-suggest-title">Fill from my website profile</div>
              <div className="bk-suggest-hint">
                See products from your brand profile. Choosing one opens it for you to check and save.
              </div>
            </div>
            <button type="button" className="btn btn-secondary btn-sm" disabled={prefillLoading} onClick={() => void loadPrefill()}>
              {prefillLoading ? 'Loading…' : prefill ? 'Refresh suggestions' : 'Show suggestions'}
            </button>
          </div>
          {prefill && !prefill.available ? <NoProfileNote /> : null}
          {prefill?.available ? (
            prefill.products.length === 0 ? (
              <p className="bk-suggest-empty">Your website profile does not list any products or services.</p>
            ) : (
              <ul className="bk-suggest-list">
                {prefill.products.map((item) => (
                  <li key={item.name} className="bk-suggest-item">
                    <div className="bk-suggest-text">
                      <span>{item.name}</span>
                    </div>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={existingNames.has(item.name.toLowerCase())}
                      onClick={() => setEditing({ product: null, seedName: item.name })}
                    >
                      {existingNames.has(item.name.toLowerCase()) ? 'Already added' : 'Add as product'}
                    </button>
                  </li>
                ))}
              </ul>
            )
          ) : null}
        </div>
      ) : null}

      {loading && !data ? (
        <Loading label="Loading products…" />
      ) : error && !data ? (
        <ErrorState message={error} onRetry={() => void reload()} />
      ) : products.length === 0 ? (
        <EmptyState
          title={showArchived ? 'No products' : 'No products yet'}
          description={
            writable
              ? 'Add the products or services you want designs for, with the claims you are happy for the AI to use.'
              : 'No one has added products for this workspace yet.'
          }
        />
      ) : (
        <ul className="bk-list">
          {products.map((product) => (
            <li key={product.id} className={`panel bk-card${product.is_archived ? ' is-archived' : ''}`}>
              <FileImage id={product.image_file_ids[0] ?? null} alt="" className="bk-card-logo" />
              <div className="bk-card-main">
                <div className="bk-card-title">
                  <span className="bk-card-name">{product.name}</span>
                  {product.is_archived ? <Tag tone="neutral">Archived</Tag> : null}
                </div>
                <div className="bk-card-meta">
                  {product.price_text ? `${product.price_text} · ` : ''}
                  {product.approved_claims.length} approved {product.approved_claims.length === 1 ? 'claim' : 'claims'} ·{' '}
                  {product.image_file_ids.length} {product.image_file_ids.length === 1 ? 'photo' : 'photos'}
                </div>
              </div>
              <div className="bk-card-actions">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setEditing({ product })}>
                  {writable && !product.is_archived ? 'Edit' : 'View'}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  aria-label={`History of ${product.name}`}
                  onClick={() => history.open({ id: product.id, name: product.name })}
                >
                  History
                </button>
                {writable && !product.is_archived ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm bk-danger-text"
                    disabled={busyId !== null}
                    onClick={() => setArchiving(product)}
                  >
                    Archive
                  </button>
                ) : null}
                {writable && product.is_archived ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    disabled={busyId !== null}
                    onClick={() => void restore(product)}
                  >
                    {busyId === product.id ? 'Restoring…' : 'Restore'}
                  </button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}

      {history.target ? (
        <EntityHistory
          kind="product"
          target={history.target}
          panelRef={history.ref}
          onClose={history.close}
          onRestored={reload}
        />
      ) : null}

      {editing ? (
        <ProductEditor
          product={editing.product}
          seedName={editing.seedName}
          writable={writable && !editing.product?.is_archived}
          limits={limits}
          onClose={() => setEditing(null)}
          onArchive={editing.product && writable && !editing.product.is_archived ? () => setArchiving(editing.product) : undefined}
          onSaved={async () => {
            setEditing(null)
            await reload()
          }}
        />
      ) : null}

      {archiving ? (
        <ConfirmDialog
          title="Archive this product?"
          message={`"${archiving.name}" will no longer be offered in Creative Studio. Designs already made are not changed. You can restore it later from "Show archived products".`}
          confirmLabel="Archive product"
          busy={busyId === archiving.id}
          onConfirm={() => void archive()}
          onCancel={() => setArchiving(null)}
        />
      ) : null}
    </section>
  )
}

function productToInput(product: Product | null, seedName?: string): ProductInput {
  if (!product) return { ...NEW_PRODUCT, name: seedName ?? '' }
  return {
    name: product.name,
    description: product.description,
    url: product.url,
    price_text: product.price_text,
    approved_claims: product.approved_claims,
    image_file_ids: product.image_file_ids,
    disclaimer: product.disclaimer,
  }
}

function ProductEditor({
  product,
  seedName,
  writable,
  limits,
  onClose,
  onArchive,
  onSaved,
}: {
  product: Product | null
  seedName?: string
  writable: boolean
  limits: Required<BrandKitLimits>
  onClose: () => void
  onArchive?: () => void
  onSaved: () => Promise<void>
}) {
  const { push, fromError } = useToasts()
  const [draft, setDraft] = useState<ProductInput>(() => productToInput(product, seedName))
  const [claims, setClaims] = useState(() => (product?.approved_claims ?? []).join('\n'))
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(0)
  const { prefill, loading: prefillLoading, load: loadPrefill } = usePrefill()

  const set = <K extends keyof ProductInput>(key: K, value: ProductInput[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    setErrors((current) => {
      if (!current[key]) return current
      const next = { ...current }
      delete next[key]
      return next
    })
  }

  const claimList = lines(claims)
  const claimSet = new Set(claimList.map((claim) => claim.toLowerCase()))

  const addClaim = (claim: string) => {
    if (claimSet.has(claim.toLowerCase())) return
    if (claimList.length >= limits.approved_claims) {
      push(`You can keep up to ${limits.approved_claims} approved claims per product.`, 'warning')
      return
    }
    const next = [...claimList, claim.slice(0, limits.claim_chars)].join('\n')
    setClaims(next)
    set('approved_claims', lines(next))
  }

  const save = async () => {
    const local: Record<string, string> = {}
    if (!draft.name.trim()) local.name = 'Please give this product a name'
    const url = draft.url.trim()
    if (url && !/^https:\/\/[^\s/]+/i.test(url)) local.url = 'Please enter a web address starting with https://'
    if (claimList.length > limits.approved_claims)
      local.approved_claims = `Please keep to ${limits.approved_claims} claims or fewer`
    else if (claimList.some((claim) => claim.length > limits.claim_chars))
      local.approved_claims = `Each claim must be ${limits.claim_chars} characters or fewer`
    if (Object.keys(local).length > 0) {
      setErrors(local)
      push(CHECK_FIELDS, 'warning')
      return
    }
    const body: ProductInput = { ...draft, name: draft.name.trim(), url, approved_claims: claimList }
    setSaving(true)
    try {
      if (product) await brandKitApi.updateProduct(product.id, body)
      else await brandKitApi.createProduct(body)
      push(product ? 'Product saved' : 'Product added', 'success')
      await onSaved()
    } catch (caught) {
      const fields = fieldErrorsFrom(caught)
      if (fields) {
        setErrors(fields)
        push(CHECK_FIELDS, 'warning')
      } else {
        fromError(caught)
      }
    } finally {
      setSaving(false)
    }
  }

  const photosFull = draft.image_file_ids.length >= limits.product_images
  const title = product ? (writable ? `Edit ${product.name}` : product.name) : 'Add product'

  return (
    <Drawer
      title={title}
      subtitle={writable ? 'Nothing is saved until you press Save.' : 'View only'}
      width={640}
      onClose={onClose}
      actions={
        <>
          {onArchive ? (
            <button type="button" className="btn btn-ghost bk-danger-text bk-actions-left" onClick={onArchive}>
              Archive
            </button>
          ) : null}
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {writable ? 'Cancel' : 'Close'}
          </button>
          {writable ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={saving || uploading > 0}
              onClick={() => void save()}
            >
              {saving ? 'Saving…' : product ? 'Save changes' : 'Add product'}
            </button>
          ) : null}
        </>
      }
    >
      <FormSection title="About the product">
        <Field label="Product name" htmlFor="bp-name" required error={errors.name} hint="For example “Deep tissue massage, 60 minutes”.">
          <input
            id="bp-name"
            className={`input${errors.name ? ' is-invalid' : ''}`}
            value={draft.name}
            maxLength={limits.product_name_chars}
            disabled={!writable}
            required
            aria-invalid={errors.name ? true : undefined}
            onChange={(event) => set('name', event.target.value)}
          />
        </Field>
        <Field
          label="Description"
          htmlFor="bp-description"
          error={errors.description}
          hint="A few sentences on what it is and who it is for. The AI uses this as background, not as claims."
        >
          <textarea
            id="bp-description"
            className={`input${errors.description ? ' is-invalid' : ''}`}
            rows={4}
            maxLength={limits.description_chars}
            value={draft.description}
            disabled={!writable}
            aria-invalid={errors.description ? true : undefined}
            onChange={(event) => set('description', event.target.value)}
          />
        </Field>
        <div className="bk-two">
          <Field label="Price" htmlFor="bp-price" error={errors.price_text} hint="As you want it shown, for example “From £49”.">
            <input
              id="bp-price"
              className={`input${errors.price_text ? ' is-invalid' : ''}`}
              value={draft.price_text}
              maxLength={limits.price_text_chars}
              disabled={!writable}
              aria-invalid={errors.price_text ? true : undefined}
              onChange={(event) => set('price_text', event.target.value)}
            />
          </Field>
          <Field label="Link" htmlFor="bp-url" error={errors.url} hint="The product page, starting with https://">
            <input
              id="bp-url"
              type="url"
              inputMode="url"
              className={`input${errors.url ? ' is-invalid' : ''}`}
              placeholder="https://"
              value={draft.url}
              maxLength={1000}
              disabled={!writable}
              aria-invalid={errors.url ? true : undefined}
              onChange={(event) => set('url', event.target.value)}
            />
          </Field>
        </div>
      </FormSection>

      <FormSection
        title="Approved claims"
        description="The AI may only use these claims about this product. Add only what is true and you can back up."
      >
        <Field
          label="Approved claims"
          htmlFor="bp-claims"
          error={errors.approved_claims}
          hint={`One claim per line, for example “Free delivery over £30” or “Rated 4.8 by 1,200 customers”. Up to ${limits.approved_claims} claims of ${limits.claim_chars} characters each.`}
        >
          <textarea
            id="bp-claims"
            className={`input${errors.approved_claims ? ' is-invalid' : ''}`}
            rows={5}
            value={claims}
            disabled={!writable}
            aria-invalid={errors.approved_claims ? true : undefined}
            onChange={(event) => {
              setClaims(event.target.value)
              set('approved_claims', lines(event.target.value))
            }}
          />
        </Field>
        {writable ? (
          <div className="bk-suggest">
            <div className="bk-suggest-head">
              <div>
                <div className="bk-suggest-title">Suggested claims from my website profile</div>
                <div className="bk-suggest-hint">These are suggestions only. Add a claim only if it is true for this product.</div>
              </div>
              <button type="button" className="btn btn-secondary btn-sm" disabled={prefillLoading} onClick={() => void loadPrefill()}>
                {prefillLoading ? 'Loading…' : prefill ? 'Refresh suggestions' : 'Show suggestions'}
              </button>
            </div>
            {prefill && !prefill.available ? <NoProfileNote /> : null}
            {prefill?.available ? (
              prefill.claim_suggestions.length === 0 ? (
                <p className="bk-suggest-empty">Your website profile has no claims to suggest.</p>
              ) : (
                <ul className="bk-suggest-list">
                  {prefill.claim_suggestions.map((claim) => (
                    <Suggestion
                      key={claim}
                      label="Claim"
                      value={claim}
                      used={claimSet.has(claim.toLowerCase())}
                      onUse={() => addClaim(claim)}
                    />
                  ))}
                </ul>
              )
            ) : null}
          </div>
        ) : null}
      </FormSection>

      <FormSection title="Photos" description={`Up to ${limits.product_images} photos of the product. ${UPLOAD_RULES}`}>
        {draft.image_file_ids.length === 0 ? <p className="bk-note">No photos yet.</p> : null}
        <ul className="bk-photos" aria-label="Product photos">
          {draft.image_file_ids.map((fileId, index) => (
            <li key={fileId} className="bk-photo">
              <FileImage id={fileId} alt={`Product photo ${index + 1}`} className="bk-photo-img" />
              {writable ? (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  aria-label={`Remove product photo ${index + 1}`}
                  onClick={() => set('image_file_ids', draft.image_file_ids.filter((other) => other !== fileId))}
                >
                  Remove
                </button>
              ) : null}
            </li>
          ))}
        </ul>
        {errors.image_file_ids ? <div className="field-error">{errors.image_file_ids}</div> : null}
        {writable ? (
          photosFull ? (
            <p className="bk-note">You have reached the limit of {limits.product_images} photos. Remove one to add another.</p>
          ) : (
            <UploadButton
              id="bp-photo"
              label="Add a photo"
              kind="product_photo"
              disabled={false}
              onUploading={(on) => setUploading((n) => n + (on ? 1 : -1))}
              onUploaded={(newId) =>
                setDraft((current) =>
                  current.image_file_ids.includes(newId) || current.image_file_ids.length >= limits.product_images
                    ? current
                    : { ...current, image_file_ids: [...current.image_file_ids, newId] },
                )
              }
            />
          )
        ) : null}
      </FormSection>

      <FormSection title="Small print">
        <Field
          label="Product disclaimer"
          htmlFor="bp-disclaimer"
          error={errors.disclaimer}
          hint={`Small print for designs of this product, for example “Results vary. Subject to availability.” Up to ${limits.disclaimer_chars} characters.`}
        >
          <textarea
            id="bp-disclaimer"
            className={`input${errors.disclaimer ? ' is-invalid' : ''}`}
            rows={2}
            maxLength={limits.disclaimer_chars}
            value={draft.disclaimer}
            disabled={!writable}
            aria-invalid={errors.disclaimer ? true : undefined}
            onChange={(event) => set('disclaimer', event.target.value)}
          />
        </Field>
      </FormSection>
    </Drawer>
  )
}
