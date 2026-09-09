/**
 * A headed group of cards.
 *
 * Both catalogue screens were one flat grid, and both were already *sorted*
 * by how much attention a row wants — errored, then working, then set up,
 * then untouched. The sections were implied by that sort and never drawn, so
 * the top row of the Connectors screen put two connected cards, each with a
 * status, a timestamp and two buttons, beside three untouched cards with a
 * single Connect button — and CSS grid stretched all five to the tallest.
 * Three quarters of that row was empty space, and the eye had no way to tell
 * that the row was two different kinds of thing.
 *
 * Grouping fixes the ordering and the ragged height at once, because the
 * cards inside a section have the same amount to say.
 *
 * The count is in the heading rather than as a separate badge: "Connected 2"
 * is what somebody is looking for, and it saves them counting a grid.
 */
import type { ReactNode } from 'react'

export function CardSection({
  title,
  count,
  description,
  children,
  tone = 'plain',
}: {
  title: string
  count?: number
  description?: string
  children: ReactNode
  /** `attention` tints the rule, for the section nobody should scroll past. */
  tone?: 'plain' | 'attention'
}) {
  return (
    <section className={`card-section${tone === 'attention' ? ' is-attention' : ''}`}>
      <div className="card-section-head">
        <h2 className="card-section-title">
          {title}
          {count === undefined ? null : <span className="card-section-count">{count}</span>}
        </h2>
        {description ? <p className="card-section-note">{description}</p> : null}
      </div>
      {children}
    </section>
  )
}
