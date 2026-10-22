/**
 * The period an insights screen covers.
 *
 * Presets cover the common questions (last 7, 30 or 90 days, this month,
 * last month); "Custom" opens two native date inputs, which bring the
 * platform's own keyboard and screen-reader support. A custom range is
 * checked here in plain words before it is applied - start before end, not in
 * the future, no longer than the server allows - and the server checks again.
 *
 * Dates are calendar days in the viewer's local time, written `YYYY-MM-DD`,
 * and both ends are included.
 */
import { useId, useState } from 'react'

import { INSIGHTS_MAX_RANGE_DAYS } from '@/api/insights'
import { Segmented } from '@/components/ui'
import '@/styles/insights.css'

export type RangePreset = '7d' | '30d' | '90d' | 'this_month' | 'last_month' | 'custom'

export interface DateRange {
  preset: RangePreset
  /** `YYYY-MM-DD`, inclusive. */
  from: string
  /** `YYYY-MM-DD`, inclusive. */
  to: string
}

const PRESETS: { value: RangePreset; label: string }[] = [
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: '90d', label: '90 days' },
  { value: 'this_month', label: 'This month' },
  { value: 'last_month', label: 'Last month' },
  { value: 'custom', label: 'Custom' },
]

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const DAY_MS = 86_400_000

/** A local calendar date as `YYYY-MM-DD`. */
export function isoDay(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

function parseDay(value: string): Date | null {
  if (!ISO_DATE.test(value)) return null
  const [y, m, d] = value.split('-').map(Number) as [number, number, number]
  const date = new Date(y, m - 1, d)
  // Rejects 2026-02-31 and the like, which Date would quietly roll over.
  return date.getFullYear() === y && date.getMonth() === m - 1 && date.getDate() === d ? date : null
}

/** Days in a range, both ends included. */
export function rangeDays(from: string, to: string): number | null {
  const start = parseDay(from)
  const end = parseDay(to)
  if (!start || !end) return null
  // Rounded, because a daylight-saving change makes a local day 23 or 25 hours.
  return Math.round((end.getTime() - start.getTime()) / DAY_MS) + 1
}

/** The dates a preset stands for, as of `today`. `custom` keeps the last-30-day dates. */
export function rangeForPreset(preset: RangePreset, today: Date = new Date()): DateRange {
  const end = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  const back = (days: number) => new Date(end.getFullYear(), end.getMonth(), end.getDate() - (days - 1))
  switch (preset) {
    case '7d':
      return { preset, from: isoDay(back(7)), to: isoDay(end) }
    case '90d':
      return { preset, from: isoDay(back(90)), to: isoDay(end) }
    case 'this_month':
      return { preset, from: isoDay(new Date(end.getFullYear(), end.getMonth(), 1)), to: isoDay(end) }
    case 'last_month':
      return {
        preset,
        from: isoDay(new Date(end.getFullYear(), end.getMonth() - 1, 1)),
        to: isoDay(new Date(end.getFullYear(), end.getMonth(), 0)),
      }
    case '30d':
    case 'custom':
      return { preset, from: isoDay(back(30)), to: isoDay(end) }
  }
}

/** "the last 30 days", "1 Oct 2026 to 21 Oct 2026": for "Everything here covers …". */
export function describeRange(range: DateRange): string {
  switch (range.preset) {
    case '7d':
      return 'the last 7 days'
    case '30d':
      return 'the last 30 days'
    case '90d':
      return 'the last 90 days'
    case 'this_month':
      return 'this month so far'
    case 'last_month':
      return 'last month'
    case 'custom': {
      const start = parseDay(range.from)
      const end = parseDay(range.to)
      if (!start || !end) return 'the chosen dates'
      const format = (date: Date) =>
        date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
      return `${format(start)} to ${format(end)}`
    }
  }
}

/** The problem with a custom range in plain words, or null when it is fine. */
export function checkCustomRange(
  from: string,
  to: string,
  today: string,
  maxDays: number = INSIGHTS_MAX_RANGE_DAYS,
): string | null {
  if (!from) return 'Pick a start date.'
  if (!to) return 'Pick an end date.'
  if (!parseDay(from)) return 'The start date is not a real date. Please pick it again.'
  if (!parseDay(to)) return 'The end date is not a real date. Please pick it again.'
  if (to > today) return 'The end date cannot be in the future.'
  if (from > to) return 'The start date must be on or before the end date.'
  const days = rangeDays(from, to)
  if (days === null || days > maxDays) return `Please pick a range of ${maxDays} days or fewer.`
  return null
}

export interface DateRangePickerProps {
  value: DateRange
  onChange: (range: DateRange) => void
  /** Longest custom range; the server's `insights_max_range_days`. */
  maxDays?: number
  disabled?: boolean
  /** Fixed "today", for tests and stories. */
  today?: Date
}

export function DateRangePicker({
  value,
  onChange,
  maxDays = INSIGHTS_MAX_RANGE_DAYS,
  disabled = false,
  today,
}: DateRangePickerProps) {
  const id = useId()
  const [custom, setCustom] = useState(value.preset === 'custom')
  const [from, setFrom] = useState(value.from)
  const [to, setTo] = useState(value.to)
  const [problem, setProblem] = useState<string | null>(null)
  const todayIso = isoDay(today ?? new Date())
  const limit = Math.max(1, Math.min(maxDays, INSIGHTS_MAX_RANGE_DAYS))
  const selected: RangePreset = custom ? 'custom' : value.preset

  const pick = (preset: RangePreset) => {
    setProblem(null)
    if (preset === 'custom') {
      setCustom(true)
      setFrom(value.from)
      setTo(value.to)
      return
    }
    setCustom(false)
    onChange(rangeForPreset(preset, today))
  }

  const apply = () => {
    const issue = checkCustomRange(from, to, todayIso, limit)
    setProblem(issue)
    if (!issue) onChange({ preset: 'custom', from, to })
  }

  const errorId = `${id}-error`
  const hintId = `${id}-hint`

  return (
    <div className="ins-range" role="group" aria-label="Date range">
      <Segmented<RangePreset>
        name={`${id}-preset`}
        value={selected}
        options={PRESETS}
        disabled={disabled}
        onChange={pick}
      />
      {custom ? (
        <form
          className="ins-range-custom"
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            apply()
          }}
        >
          <div className="ins-range-field">
            <label htmlFor={`${id}-from`}>From</label>
            <input
              id={`${id}-from`}
              className="input w-auto"
              type="date"
              value={from}
              max={to || todayIso}
              disabled={disabled}
              aria-invalid={problem ? true : undefined}
              aria-describedby={problem ? errorId : hintId}
              onChange={(event) => {
                setFrom(event.target.value)
                setProblem(null)
              }}
            />
          </div>
          <div className="ins-range-field">
            <label htmlFor={`${id}-to`}>To</label>
            <input
              id={`${id}-to`}
              className="input w-auto"
              type="date"
              value={to}
              min={from || undefined}
              max={todayIso}
              disabled={disabled}
              aria-invalid={problem ? true : undefined}
              aria-describedby={problem ? errorId : hintId}
              onChange={(event) => {
                setTo(event.target.value)
                setProblem(null)
              }}
            />
          </div>
          <button type="submit" className="btn btn-secondary btn-sm" disabled={disabled}>
            Apply
          </button>
          {problem ? (
            <p className="field-error ins-range-note" id={errorId} role="alert">
              {problem}
            </p>
          ) : (
            <p className="card-meta ins-range-note" id={hintId}>
              Up to {limit} days. Both dates are included.
            </p>
          )}
        </form>
      ) : null}
    </div>
  )
}
