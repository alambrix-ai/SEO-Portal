/**
 * The mark on a connector card.
 *
 * Three sources, in order, because there is no single honest one:
 *
 * 1. **The vendor's own mark**, from simple-icons, for the seventeen brands
 *    that publish one there under CC0. Solid 24x24 shapes — a brand mark
 *    redrawn in this interface's thin-stroke style is not the brand's mark
 *    any more, so these are used exactly as published.
 *
 * 2. **A lettermark on the brand's colour**, for the eight brands simple-icons
 *    has removed on trademark grounds: LinkedIn, Slack, Microsoft, Adobe,
 *    Salesforce, Magento, OpenAI, Bing. Their marks are not ours to ship, and
 *    reinstating them from an older release of the package would be ignoring
 *    the reason they left it. A colour is a fact about a brand rather than a
 *    mark, so the tile still reads as the right vendor at a glance.
 *
 * 3. **A drawn glyph**, in the interface's own style, for the four connectors
 *    that are not a product at all — a webhook, a crawler, an exchange, an
 *    SMTP server. There is no logo to get right.
 *
 * Everything sits in the same tinted tile at the same size, so a grid of
 * twenty-nine cards reads as one set even though the glyphs come from three
 * places.
 */
import type { SVGProps } from 'react'

import { BRAND_MARKS } from './brand-icons'

/** Brands whose mark this project does not ship — see (2) above. */
const LETTERMARKS: Record<string, { letters: string; hex: string; title: string }> = {
  linkedin_ads: { letters: 'in', hex: '#0A66C2', title: 'LinkedIn' },
  slack: { letters: 'S', hex: '#4A154B', title: 'Slack' },
  microsoft_ads: { letters: 'MS', hex: '#0078D4', title: 'Microsoft' },
  adobe_analytics: { letters: 'A', hex: '#EB1000', title: 'Adobe' },
  salesforce: { letters: 'SF', hex: '#00A1E0', title: 'Salesforce' },
  magento: { letters: 'M', hex: '#EE672F', title: 'Magento' },
  openai: { letters: 'AI', hex: '#412991', title: 'OpenAI' },
  bing_webmaster: { letters: 'b', hex: '#174AE4', title: 'Bing' },
}

/** The neutral tint for a drawn glyph, which has no brand colour to use. */
const GENERIC_HEX = 'var(--color-text)'

type GlyphProps = SVGProps<SVGSVGElement>

/** Thin-stroke glyphs, matching the geometry in icons.tsx. */
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
  // A webhook: the payload arriving from somewhere unspecified.
  custom_api: () => (
    <Glyph>
      <path d="M7 5.5 3.5 10 7 14.5" />
      <path d="M13 5.5 16.5 10 13 14.5" />
      <line x1="11.2" y1="4.8" x2="8.8" y2="15.2" />
    </Glyph>
  ),
  // A crawler: the site, walked.
  site_crawler: () => (
    <Glyph>
      <circle cx="10" cy="10" r="7" />
      <ellipse cx="10" cy="10" rx="3" ry="7" />
      <line x1="3" y1="10" x2="17" y2="10" />
      <path d="M4.2 6.2h11.6M4.2 13.8h11.6" />
    </Glyph>
  ),
  // An exchange: inventory bid in both directions.
  dsp_exchange: () => (
    <Glyph>
      <path d="M3 7.5h11l-2.5-2.5" />
      <path d="M17 12.5H6l2.5 2.5" />
    </Glyph>
  ),
  // An envelope, for the one connector that is a mail server.
  smtp_email: () => (
    <Glyph>
      <rect x="2.5" y="5" width="15" height="10" rx="1" />
      <path d="M2.9 5.7 10 11l7.1-5.3" />
    </Glyph>
  ),
}

type Props = { slug: string; name: string; size?: number }

/**
 * The tile. `size` is the tile, not the glyph — the mark is inset so a dense
 * logo and a sparse one carry the same visual weight.
 */
export function ConnectorIcon({ slug, name, size = 34 }: Props) {
  const brand = BRAND_MARKS[slug]
  const letters = LETTERMARKS[slug]
  const glyph = GLYPHS[slug]
  const hex = brand?.hex ?? letters?.hex ?? GENERIC_HEX
  const label = brand?.title ?? letters?.title ?? name
  const inner = Math.round(size * 0.56)

  return (
    <span
      className="connector-icon"
      style={{
        width: size,
        height: size,
        // The brand's own colour, at a tint that survives both themes. A
        // full-strength logo tile would make a grid of these shout.
        background: `color-mix(in srgb, ${hex} 13%, transparent)`,
        color: hex,
      }}
      title={label}
    >
      {brand ? (
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
      ) : letters ? (
        <span className="connector-icon-letters" style={{ fontSize: Math.round(size * 0.4) }}>
          {letters.letters}
        </span>
      ) : (
        // Nothing is registered for this slug. The initial of its name is
        // still better than an empty tile, and it means a connector added
        // without an icon looks unfinished rather than broken.
        <span className="connector-icon-letters" style={{ fontSize: Math.round(size * 0.4) }}>
          {name.slice(0, 1).toUpperCase()}
        </span>
      )}
    </span>
  )
}
