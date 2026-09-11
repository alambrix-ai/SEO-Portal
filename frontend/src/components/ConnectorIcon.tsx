/**
 * The mark on a connector card.
 *
 * Prefer the brand's own product favicon (official-logos.ts). Fall back to a
 * product-specific simple-icons SVG when we ship one. Drawn glyphs only for
 * non-brand connectors (custom API, crawler, DSP, SMTP).
 */
import { useState, type SVGProps } from 'react'

import { BRAND_MARKS } from './brand-icons'
import {
  OFFICIAL_LOGO_DOMAINS,
  officialLogoFallbackUrl,
  officialLogoUrl,
} from './official-logos'

const NEUTRAL_TILE =
  'var(--color-surface-2, color-mix(in srgb, var(--color-text) 8%, transparent))'
const GENERIC_HEX = 'var(--color-text)'

type GlyphProps = SVGProps<SVGSVGElement>

function Glyph({ children, ...rest }: GlyphProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  )
}

const GLYPHS: Record<string, () => React.ReactNode> = {
  custom_api: () => (
    <Glyph>
      <path d="M7 5.5 3.5 10 7 14.5" />
      <path d="M13 5.5 16.5 10 13 14.5" />
      <line x1="11.2" y1="4.8" x2="8.8" y2="15.2" />
    </Glyph>
  ),
  site_crawler: () => (
    <Glyph>
      <circle cx="10" cy="10" r="7" />
      <ellipse cx="10" cy="10" rx="3" ry="7" />
      <line x1="3" y1="10" x2="17" y2="10" />
      <path d="M4.2 6.2h11.6M4.2 13.8h11.6" />
    </Glyph>
  ),
  dsp_exchange: () => (
    <Glyph>
      <path d="M3 7.5h11l-2.5-2.5" />
      <path d="M17 12.5H6l2.5 2.5" />
    </Glyph>
  ),
  smtp_email: () => (
    <Glyph>
      <rect x="2.5" y="5" width="15" height="10" rx="1" />
      <path d="M2.9 5.7 10 11l7.1-5.3" />
    </Glyph>
  ),
}

function OfficialLogo({
  domain,
  title,
  size,
  fallback,
}: {
  domain: string
  title: string
  size: number
  fallback: string
}) {
  const [src, setSrc] = useState(officialLogoUrl(domain))
  const [failed, setFailed] = useState(false)

  if (failed) {
    return (
      <span className="connector-icon-letters" style={{ fontSize: Math.round(size * 0.4) }}>
        {fallback}
      </span>
    )
  }

  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => {
        const next = officialLogoFallbackUrl(domain)
        if (src !== next) {
          setSrc(next)
          return
        }
        setFailed(true)
      }}
      style={{ display: 'block', objectFit: 'contain' }}
      title={title}
    />
  )
}

type Props = { slug: string; name: string; size?: number }

export function ConnectorIcon({ slug, name, size = 34 }: Props) {
  // Prefer the product's own favicon whenever we know its domain.
  const official = OFFICIAL_LOGO_DOMAINS[slug]
  const brand = official ? undefined : BRAND_MARKS[slug]
  const glyph = GLYPHS[slug]
  const hex = brand?.hex ?? GENERIC_HEX
  const label = official?.title ?? brand?.title ?? name
  const inner = Math.round(size * 0.56)
  const usesPhoto = Boolean(official)

  return (
    <span
      className="connector-icon"
      style={{
        width: size,
        height: size,
        background: usesPhoto
          ? NEUTRAL_TILE
          : `color-mix(in srgb, ${hex} 13%, transparent)`,
        color: hex,
      }}
      title={label}
    >
      {official ? (
        <OfficialLogo
          domain={official.domain}
          title={official.title}
          size={inner}
          fallback={name.slice(0, 1).toUpperCase()}
        />
      ) : brand ? (
        <svg
          width={inner}
          height={inner}
          viewBox="0 0 24 24"
          fill="currentColor"
          aria-hidden="true"
          focusable="false"
        >
          <path d={brand.path} />
        </svg>
      ) : glyph ? (
        <span style={{ width: inner, height: inner, display: 'block' }}>{glyph()}</span>
      ) : (
        <span className="connector-icon-letters" style={{ fontSize: Math.round(size * 0.4) }}>
          {name.slice(0, 1).toUpperCase()}
        </span>
      )}
    </span>
  )
}
