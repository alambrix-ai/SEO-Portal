/**
 * Defensive helpers for API payloads that may be missing, mistyped, or shaped
 * differently across environments (e.g. catalog as a dict vs a list).
 */

/** Always return an array; never throw on null / object / scalar. */
export function asList<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

/**
 * Billing plan catalog: the API should send `BillingPlanOut[]`, but an older
 * (or mis-shaped) payload was a `{ [slug]: planMeta }` map. Normalise both.
 */
export function asBillingPlans(
  value: unknown,
): { slug: string; name: string; [key: string]: unknown }[] {
  if (Array.isArray(value)) {
    return value
      .map((row) => {
        if (!row || typeof row !== 'object' || Array.isArray(row)) return null
        const rec = row as Record<string, unknown>
        const slug = typeof rec.slug === 'string' ? rec.slug : ''
        if (!slug) return null
        const name = typeof rec.name === 'string' ? rec.name : slug
        return { ...rec, slug, name }
      })
      .filter((row): row is { slug: string; name: string; [key: string]: unknown } => row != null)
  }
  if (value && typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([slug, meta]) => {
        if (!slug || !meta || typeof meta !== 'object' || Array.isArray(meta)) return null
        const rec = meta as Record<string, unknown>
        const name = typeof rec.name === 'string' ? rec.name : slug
        return { ...rec, slug, name }
      })
      .filter((row): row is { slug: string; name: string; [key: string]: unknown } => row != null)
  }
  return []
}

/** String list (e.g. MFA recovery codes) that must never be undefined at render. */
export function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string')
}
