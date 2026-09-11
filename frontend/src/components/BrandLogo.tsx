/**
 * The Alambrix wordmark.
 *
 * The supplied file is a white monochrome wordmark on transparency - 86%
 * transparent, 12% white, no other ink. Dropped in as an `<img>` it would be
 * invisible against this interface's off-white background, which is the
 * default theme, so something has to give.
 *
 * It is drawn as a CSS mask instead: the image supplies the shape and
 * `currentColor` supplies the ink. The mark's geometry is untouched and it
 * takes the same colour as the text beside it, so it reads correctly on the
 * light theme and on the dark one without a second asset, an inverted copy,
 * or a filter that would also wash out anything else in the file.
 *
 * That treatment is right *because* the mark is monochrome. If the brand ever
 * ships a two-colour version this has to become a plain `<img>` and the
 * surfaces behind it have to be dark enough for it - a mask would flatten the
 * second colour away.
 */
import logoUrl from '@/assets/logo.png'

/** The cropped asset's aspect ratio, so only a height is ever specified. */
const RATIO = 502 / 160

export function BrandLogo({
  height = 26,
  title = 'Alambrix',
}: {
  height?: number
  title?: string
}) {
  return (
    <span
      className="brand-logo"
      role="img"
      aria-label={title}
      style={{
        height,
        width: Math.round(height * RATIO),
        backgroundColor: 'currentColor',
        WebkitMaskImage: `url(${logoUrl})`,
        maskImage: `url(${logoUrl})`,
      }}
    />
  )
}
