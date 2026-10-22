/**
 * The Synaprank.ai wordmark.
 *
 * Set as type rather than an image: the only supplied artwork carries the
 * product's former name, and a text mark takes `currentColor`, so it reads
 * correctly on the light theme and on the dark one without a second asset.
 * The visible text is hidden from assistive technology and the whole mark is
 * announced once, by its accessible name.
 *
 * `compact` draws the single-letter mark for the collapsed sidebar rail, which
 * is too narrow for the full name.
 */

export const BRAND_NAME = 'Synaprank.ai'

export function BrandLogo({
  height = 26,
  title = BRAND_NAME,
  compact = false,
}: {
  height?: number
  title?: string
  compact?: boolean
}) {
  return (
    <span
      className="brand-logo"
      role="img"
      aria-label={title}
      style={{
        height,
        lineHeight: `${height}px`,
        fontSize: Math.round(height * 0.8),
        fontFamily: 'var(--font-heading)',
        fontWeight: 700,
        letterSpacing: '-0.01em',
        whiteSpace: 'nowrap',
        color: 'currentColor',
      }}
    >
      <span aria-hidden="true">{compact ? 'S' : BRAND_NAME}</span>
    </span>
  )
}
