/**
 * Brand kit API (`/brand-kit/*` and `GET /files/{id}`).
 *
 * Types mirror `backend/app/api/v1/brand_kit.py`. Everything goes through the
 * shared client in `./client`, so auth, token refresh and the `{code, detail,
 * fields}` error shape behave exactly as on every other screen.
 */
import type { ActionResult } from './types'
import { request, requestBlob, requestForm } from './client'

// ── Types ──────────────────────────────────────────────────────────────────
export interface BrandKit {
  id: string
  name: string
  is_default: boolean
  logo_file_id: string | null
  logo_dark_file_id: string | null
  primary_color: string
  secondary_color: string
  accent_color: string
  text_color: string
  background_color: string
  heading_font: string
  body_font: string
  voice: string
  banned_phrases: string[]
  disclaimer: string
  created_at: string
  updated_at: string
}

/** What the kit editor sends. On edit, `null` logos remove the logo. */
export interface BrandKitInput {
  name: string
  logo_file_id: string | null
  logo_dark_file_id: string | null
  primary_color: string
  secondary_color: string
  accent_color: string
  text_color: string
  background_color: string
  heading_font: string
  body_font: string
  voice: string
  banned_phrases: string[]
  disclaimer: string
}

export interface Product {
  id: string
  name: string
  description: string
  url: string
  price_text: string
  approved_claims: string[]
  image_file_ids: string[]
  disclaimer: string
  is_archived: boolean
  created_at: string
  updated_at: string
}

export interface ProductInput {
  name: string
  description: string
  url: string
  price_text: string
  approved_claims: string[]
  image_file_ids: string[]
  disclaimer: string
}

export type UploadKind = 'logo' | 'product_photo' | 'background'

export interface StoredFileInfo {
  id: string
  kind: string
  mime: string
  width: number
  height: number
  size_bytes: number
  display_name: string
  created_at: string
}

export interface FontOption {
  id: string
  label: string
}

/** Limits the server enforces; keys match `brand_kits.options()`. */
export interface BrandKitLimits {
  kits?: number
  products?: number
  kit_name_chars?: number
  voice_chars?: number
  disclaimer_chars?: number
  banned_phrases?: number
  banned_phrase_chars?: number
  product_name_chars?: number
  description_chars?: number
  price_text_chars?: number
  approved_claims?: number
  claim_chars?: number
  product_images?: number
}

export interface BrandKitOptions {
  fonts: FontOption[]
  limits: BrandKitLimits
}

/** Suggestions from the brand profile. Nothing here is saved until a person saves it. */
export interface BrandKitPrefill {
  available: boolean
  kit: { name: string; voice: string } | null
  products: { name: string }[]
  claim_suggestions: string[]
}

// ── Ids ────────────────────────────────────────────────────────────────────
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * True for a well-formed record id. Ids come back from the server, but they
 * are still checked before they become part of a request path, so a bad
 * value can never point a request somewhere else.
 */
export function isId(value: string | null | undefined): value is string {
  return typeof value === 'string' && UUID.test(value)
}

function idPath(value: string): string {
  if (!isId(value)) throw new Error('Invalid id')
  return encodeURIComponent(value)
}

// ── Upload rules (mirrors the server; the server still decides) ────────────
export const UPLOAD_MAX_BYTES = 5 * 1024 * 1024
export const SVG_MAX_BYTES = 1024 * 1024
export const UPLOAD_ACCEPT = '.png,.jpg,.jpeg,.webp,.svg,image/png,image/jpeg,image/webp,image/svg+xml'
const UPLOAD_EXTENSIONS = /\.(png|jpe?g|webp|svg)$/i
const UPLOAD_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml', ''])

/**
 * A quick check before uploading, so an obviously wrong file gets a plain
 * answer without a round trip. Returns the message to show, or null.
 */
export function checkUpload(file: File): string | null {
  if (file.size === 0) return 'That file is empty. Please choose an image to upload.'
  if (!UPLOAD_EXTENSIONS.test(file.name) || !UPLOAD_TYPES.has(file.type)) {
    return 'Please upload a PNG, JPG, WebP or SVG image.'
  }
  const isSvg = /\.svg$/i.test(file.name) || file.type === 'image/svg+xml'
  if (isSvg && file.size > SVG_MAX_BYTES) {
    return 'That SVG file is too big. Please upload one under 1 MB, or a PNG.'
  }
  if (file.size > UPLOAD_MAX_BYTES) return 'That file is too big. Please upload an image under 5 MB.'
  return null
}

// ── Endpoints ──────────────────────────────────────────────────────────────
export const brandKitApi = {
  options: (signal?: AbortSignal) => request<BrandKitOptions>('/brand-kit/options', { signal }),
  /** Needs Brand Kit write access; call it only for writers. */
  prefill: (signal?: AbortSignal) => request<BrandKitPrefill>('/brand-kit/prefill', { signal }),

  kits: (signal?: AbortSignal) => request<BrandKit[]>('/brand-kit/kits', { signal }),
  createKit: (body: BrandKitInput) => request<BrandKit>('/brand-kit/kits', { method: 'POST', body }),
  updateKit: (id: string, body: BrandKitInput) =>
    request<BrandKit>(`/brand-kit/kits/${idPath(id)}`, { method: 'PATCH', body }),
  deleteKit: (id: string) => request<ActionResult>(`/brand-kit/kits/${idPath(id)}`, { method: 'DELETE' }),
  makeDefaultKit: (id: string) =>
    request<BrandKit>(`/brand-kit/kits/${idPath(id)}/default`, { method: 'POST' }),

  products: (includeArchived: boolean, signal?: AbortSignal) =>
    request<Product[]>(`/brand-kit/products?include_archived=${includeArchived ? 'true' : 'false'}`, { signal }),
  createProduct: (body: ProductInput) =>
    request<Product>('/brand-kit/products', { method: 'POST', body }),
  updateProduct: (id: string, body: Partial<ProductInput> & { is_archived?: boolean }) =>
    request<Product>(`/brand-kit/products/${idPath(id)}`, { method: 'PATCH', body }),
  archiveProduct: (id: string) =>
    request<ActionResult>(`/brand-kit/products/${idPath(id)}`, { method: 'DELETE' }),

  upload: (kind: UploadKind, file: File) => {
    const form = new FormData()
    form.append('kind', kind)
    form.append('file', file, file.name)
    return requestForm<StoredFileInfo>('/brand-kit/uploads', form)
  },

  /** An uploaded image as a blob, fetched with the session's auth header. */
  fileBlob: (id: string, signal?: AbortSignal) => requestBlob(`/files/${idPath(id)}`, { signal }),
}
