/**
 * Money formatting for the console.
 *
 * Mirrors `app/core/money.py`: the same currency and the same digit grouping,
 * because a figure rendered by the API and the same figure rendered by a React
 * component must not disagree about how it is written.
 *
 * `Intl.NumberFormat` with the `en-IN` locale already does lakh/crore
 * grouping — ₹12,34,567 rather than ₹1,234,567 — so this is a thin wrapper
 * over the platform's own implementation rather than hand-rolled arithmetic.
 * That grouping is not cosmetic: a figure grouped in thousands is genuinely
 * misread by someone who expects lakhs.
 */

/** Kept in sync with `CURRENCY_SYMBOL` / `CURRENCY_GROUPING` on the server. */
const LOCALE = 'en-IN'
const CURRENCY = 'INR'

const whole = new Intl.NumberFormat(LOCALE, {
  style: 'currency',
  currency: CURRENCY,
  maximumFractionDigits: 0,
})

const precise = new Intl.NumberFormat(LOCALE, {
  style: 'currency',
  currency: CURRENCY,
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

/** `₹12,34,567` — for totals, where paise are noise. */
export function money(amount: number): string {
  return whole.format(amount)
}

/**
 * `₹1,234.50` — for a cost per acquisition, where the paise are the point:
 * the whole job of the budget engine is moving spend between channels a few
 * rupees apart.
 */
export function moneyExact(amount: number): string {
  return precise.format(amount)
}

/** `₹4.57Cr`, `₹1.23L` — for tiles, in the units an Indian team reads in. */
export function moneyCompact(amount: number): string {
  const abs = Math.abs(amount)
  if (abs >= 10_000_000) return `${trim(amount / 10_000_000)}Cr`
  if (abs >= 100_000) return `${trim(amount / 100_000)}L`
  return money(amount)
}

function trim(value: number): string {
  // Two decimals, then drop trailing zeros: ₹4.5Cr, not ₹4.50Cr.
  return precise.format(value).replace(/\.?0+$/, '')
}
