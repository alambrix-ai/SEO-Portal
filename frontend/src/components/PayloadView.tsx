/**
 * What an approval will actually do, in words.
 *
 * The review dialog used to render the stored payload as `JSON.stringify(…,
 * null, 2)` inside a `<pre>`: braces, quoted keys, snake_case, and internal
 * database ids sitting alongside the one thing the reviewer needs to read.
 * The person approving is a marketing lead deciding whether copy goes on
 * their own website. Asking them to read a JSON object to find out is asking
 * them to approve something they cannot see.
 *
 * So the payload is presented, not printed:
 *
 * * **Prose** — the rewrite, the pitch, the headline — gets room and its own
 *   line breaks. It is the substance of the decision, so it is the biggest
 *   thing on screen.
 * * **Figures** get a label in words and, where the unit is known, a unit.
 * * **Lists** become chips, because "three missing topics" is read at a
 *   glance and `["a","b","c"]` is not.
 * * **Internal ids are dropped.** `page_id: 'f3c1…'` is how the platform
 *   finds the row; it is not information about the change.
 *
 * Nothing here invents meaning. An unrecognised key still appears, with its
 * name humanised — the alternative is hiding data from somebody who is
 * accountable for the decision.
 */
import type { ReactNode } from 'react'

import { moneyExact } from '@/lib/money'

/** Fields that are prose and deserve reading room, longest-form first. */
const PROSE = new Set([
  'proposed_body',
  'body',
  'body_copy',
  'message',
  'rationale',
  'reason',
  'subject',
  'headline',
  'call_to_action',
  'pitch',
])

/** Fields that are a list of short labels. */
const CHIPS = new Set([
  'missing_topics',
  'top_signals',
  'platforms',
  'schema_types',
  'target_keywords',
  'dimensions',
])

/** Amounts, rendered in the workspace's currency rather than as a bare float. */
const MONEY = new Set(['spend_saved', 'spend', 'budget', 'monthly_budget', 'cost'])

/** Figures that are a 0–100 score. */
const SCORES = new Set(['gap_score', 'authority', 'relevance', 'confidence', 'cohesion'])

/**
 * Keys that exist so the platform can find a row again. A reviewer gains
 * nothing from a UUID, and every one of them displaced something that
 * mattered.
 */
function isInternal(key: string): boolean {
  return key === 'id' || key.endsWith('_id') || key.endsWith('_ids')
}

/** `source_page_url` -> `Source page url` -> `Source page URL`. */
function humanise(key: string): string {
  const words = key.replace(/_/g, ' ').trim()
  const sentence = words.charAt(0).toUpperCase() + words.slice(1)
  return sentence
    .replace(/\burl\b/gi, 'URL')
    .replace(/\bcta\b/gi, 'CTA')
    .replace(/\bcac\b/gi, 'CAC')
    .replace(/\bseo\b/gi, 'SEO')
}

function scalar(key: string, value: unknown): ReactNode {
  if (typeof value === 'boolean') {
    // `estimates_unverified: true` is a caveat about the numbers above it,
    // not a datum. It reads as one.
    return value ? 'Yes' : 'No'
  }
  if (typeof value === 'number') {
    // Exact, because a saving of ₹1,234.50 rounded to ₹1,235 is a
    // different claim about somebody's money.
    if (MONEY.has(key)) return moneyExact(value)
    if (SCORES.has(key)) return `${value} / 100`
    return new Intl.NumberFormat('en-IN').format(value)
  }
  return String(value)
}

export function PayloadView({ payload }: { payload: Record<string, unknown> }) {
  const entries = Object.entries(payload).filter(
    ([key, value]) =>
      !isInternal(key) && value !== null && value !== undefined && value !== '',
  )
  if (entries.length === 0) {
    return <p className="payload-empty">Nothing was stored with this proposal.</p>
  }

  const prose = entries.filter(([key, value]) => PROSE.has(key) && typeof value === 'string')
  const rest = entries.filter(([key, value]) => !(PROSE.has(key) && typeof value === 'string'))

  return (
    <div className="payload">
      {/* The substance of the decision, first and with room. */}
      {prose.map(([key, value]) => (
        <div className="payload-prose" key={key}>
          <div className="payload-label">{humanise(key)}</div>
          <div className="payload-text">{String(value)}</div>
        </div>
      ))}

      {rest.length > 0 ? (
        <dl className="payload-facts">
          {rest.map(([key, value]) => (
            <div className="payload-fact" key={key}>
              <dt>{humanise(key)}</dt>
              <dd>
                {Array.isArray(value) ? (
                  value.length === 0 ? (
                    <span className="muted">None</span>
                  ) : (
                    <span className="payload-chips">
                      {value.map((item, index) => (
                        <span className="payload-chip" key={`${String(item)}-${index}`}>
                          {typeof item === 'object'
                            ? Object.values(item as object).join(' · ')
                            : String(item)}
                        </span>
                      ))}
                    </span>
                  )
                ) : typeof value === 'object' ? (
                  // A nested object still gets rows rather than braces.
                  <span className="payload-nested">
                    {Object.entries(value as Record<string, unknown>)
                      .filter(([inner]) => !isInternal(inner))
                      .map(([inner, innerValue]) => (
                        <span className="payload-nested-row" key={inner}>
                          <span className="payload-nested-key">{humanise(inner)}</span>
                          {scalar(inner, innerValue)}
                        </span>
                      ))}
                  </span>
                ) : CHIPS.has(key) ? (
                  <span className="payload-chip">{String(value)}</span>
                ) : (
                  scalar(key, value)
                )}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  )
}
