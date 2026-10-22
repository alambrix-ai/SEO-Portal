/**
 * Time zones for pickers, in words a marketer recognises.
 *
 * The list comes from the browser (`Intl.supportedValuesOf('timeZone')`). Older
 * browsers lack that call, so a short list of common zones stands in. The
 * server checks every zone again before saving, so this list only guides the
 * choice; it is not what keeps bad values out.
 */

/** Used when the browser cannot list its zones. Covers the main business hubs. */
export const FALLBACK_TIME_ZONES: readonly string[] = [
  'UTC',
  'Africa/Cairo',
  'Africa/Johannesburg',
  'Africa/Lagos',
  'Africa/Nairobi',
  'America/Anchorage',
  'America/Argentina/Buenos_Aires',
  'America/Bogota',
  'America/Chicago',
  'America/Denver',
  'America/Halifax',
  'America/Los_Angeles',
  'America/Mexico_City',
  'America/New_York',
  'America/Phoenix',
  'America/Santiago',
  'America/Sao_Paulo',
  'America/Toronto',
  'America/Vancouver',
  'Asia/Bangkok',
  'Asia/Dhaka',
  'Asia/Dubai',
  'Asia/Hong_Kong',
  'Asia/Jakarta',
  'Asia/Jerusalem',
  'Asia/Karachi',
  'Asia/Kathmandu',
  'Asia/Kolkata',
  'Asia/Manila',
  'Asia/Riyadh',
  'Asia/Seoul',
  'Asia/Shanghai',
  'Asia/Singapore',
  'Asia/Taipei',
  'Asia/Tokyo',
  'Atlantic/Reykjavik',
  'Australia/Adelaide',
  'Australia/Brisbane',
  'Australia/Melbourne',
  'Australia/Perth',
  'Australia/Sydney',
  'Europe/Amsterdam',
  'Europe/Athens',
  'Europe/Berlin',
  'Europe/Dublin',
  'Europe/Helsinki',
  'Europe/Istanbul',
  'Europe/Lisbon',
  'Europe/London',
  'Europe/Madrid',
  'Europe/Moscow',
  'Europe/Paris',
  'Europe/Rome',
  'Europe/Stockholm',
  'Europe/Warsaw',
  'Europe/Zurich',
  'Pacific/Auckland',
  'Pacific/Honolulu',
]

/**
 * Browsers still list some cities under their old names (Chrome and Node give
 * "Asia/Calcutta"). Show the current name, which is what people search for;
 * the server accepts both.
 */
const CURRENT_NAMES: Readonly<Record<string, string>> = {
  'America/Buenos_Aires': 'America/Argentina/Buenos_Aires',
  'Asia/Calcutta': 'Asia/Kolkata',
  'Asia/Katmandu': 'Asia/Kathmandu',
  'Asia/Rangoon': 'Asia/Yangon',
  'Asia/Saigon': 'Asia/Ho_Chi_Minh',
  'Atlantic/Faeroe': 'Atlantic/Faroe',
  'Europe/Kiev': 'Europe/Kyiv',
  'Pacific/Enderbury': 'Pacific/Kanton',
}

/** The current name, if this browser knows it; otherwise the name as given. */
function currentName(zone: string): string {
  const renamed = CURRENT_NAMES[zone]
  if (!renamed) return zone
  try {
    new Intl.DateTimeFormat('en', { timeZone: renamed })
    return renamed
  } catch {
    return zone
  }
}

let cached: readonly string[] | null = null

/** Every zone the browser knows, sorted, always including UTC. */
export function allTimeZones(): readonly string[] {
  if (cached) return cached
  let zones: string[] = []
  try {
    if (typeof Intl.supportedValuesOf === 'function') zones = Intl.supportedValuesOf('timeZone')
  } catch {
    // Some engines throw for an unsupported key; the fallback list covers it.
    zones = []
  }
  const list = (zones.length ? zones : [...FALLBACK_TIME_ZONES]).map(currentName)
  cached = [...new Set(['UTC', ...list])].sort((a, b) => (a === 'UTC' ? -1 : b === 'UTC' ? 1 : a.localeCompare(b)))
  return cached
}

/** The zone this browser is in, or '' if it cannot say. */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch {
    return ''
  }
}

/** Is this a zone the browser can use? '' (meaning UTC) counts as valid. */
export function isKnownTimeZone(zone: string): boolean {
  if (!zone) return true
  if (allTimeZones().includes(zone)) return true
  try {
    new Intl.DateTimeFormat('en', { timeZone: zone })
    return true
  } catch {
    return false
  }
}

/** "GMT+5:30" style offset for right now, or '' if the browser cannot work it out. */
export function timeZoneOffset(zone: string, at: Date = new Date()): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      // 'shortOffset' is in every current browser but not yet in TypeScript's types.
      timeZoneName: 'shortOffset' as Intl.DateTimeFormatOptions['timeZoneName'],
    }).formatToParts(at)
    return parts.find((part) => part.type === 'timeZoneName')?.value ?? ''
  } catch {
    return ''
  }
}

/**
 * "Kolkata, Asia (GMT+5:30)" for "Asia/Kolkata": city first, because that is
 * what people look for. '' stands for UTC.
 */
const labels = new Map<string, string>()

export function timeZoneLabel(zone: string): string {
  if (!zone || zone === 'UTC') return 'UTC (no time zone set)'
  const known = labels.get(zone)
  if (known !== undefined) return known
  const pieces = zone.split('/').map((piece) => piece.replace(/_/g, ' '))
  const city = pieces[pieces.length - 1] ?? zone
  const region = pieces.slice(0, -1).reverse().join(', ')
  const offset = timeZoneOffset(zone)
  const place = region ? `${city}, ${region}` : city
  const label = offset ? `${place} (${offset})` : place
  // Offsets shift with daylight saving, but not within one visit to the page.
  labels.set(zone, label)
  return label
}

/** Zones whose id or label contains every word of the query, best matches first. */
export function searchTimeZones(query: string, limit = 50): string[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const zones = allTimeZones()
  if (!words.length) return zones.slice(0, limit)
  const scored: { zone: string; score: number }[] = []
  for (const zone of zones) {
    const id = zone.toLowerCase().replace(/_/g, ' ')
    const city = id.split('/').pop() ?? id
    const haystack = `${id} ${timeZoneLabel(zone).toLowerCase()}`
    if (!words.every((word) => haystack.includes(word))) continue
    // City names starting with the query rank above matches elsewhere.
    scored.push({ zone, score: city.startsWith(words[0] ?? '') ? 0 : 1 })
  }
  return scored
    .sort((a, b) => a.score - b.score || a.zone.localeCompare(b.zone))
    .slice(0, limit)
    .map((item) => item.zone)
}
