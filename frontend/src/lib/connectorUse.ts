/**
 * Which connectors an agent uses, for display and for the setup controls.
 *
 * The server resolves this with the same rules a run follows and sends the
 * sentences (`NeedUse.statement`, `automatic_statement`, `template`). The
 * only sentences built here are for a selection somebody has just made in
 * the form and not saved yet, and they use the server's own wording.
 */
import type { AgentNeed, AgentOut, NeedUse } from '@/api/types'

/** `A`, `A and B`, `A, B and C`. */
export function joinNames(names: string[]): string {
  if (names.length <= 1) return names.join('')
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** Every use that has a control, once each, in need order. */
export function controlUses(agent: AgentOut): NeedUse[] {
  const seen = new Set<string>()
  const out: NeedUse[] = []
  for (const need of agent.needs) {
    for (const use of need.uses ?? []) {
      if (!use.primary || !use.control || seen.has(use.capability)) continue
      seen.add(use.capability)
      out.push(use)
    }
  }
  return out
}

/** The form's starting selections: what each control shows pre-filled. */
export function initialSelections(agent: AgentOut): Record<string, string[]> {
  return Object.fromEntries(controlUses(agent).map((use) => [use.capability, [...use.selected]]))
}

const usable = (use: NeedUse) => use.options.filter((o) => o.connected && !o.locked)

function sameSet(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x) => b.includes(x))
}

/** True when nothing is ticked on a control that must have at least one. */
export function needsOne(use: NeedUse, value: string[]): boolean {
  return use.control === 'multi' && use.mode !== 'one' && value.length === 0 && usable(use).length > 0
}

/**
 * What to save for a control. Ticking every connected one on a "uses all" or
 * page-source control is saved as "every one" ([]), so an integration
 * connected later is included too — which is what "all" means.
 */
export function toSaved(use: NeedUse, value: string[]): string[] {
  if (use.mode !== 'one') {
    const every = usable(use).map((o) => o.slug)
    const staleTicked = value.some((slug) => !every.includes(slug))
    if (!staleTicked && every.length > 0 && sameSet(value, every)) return []
  }
  return value
}

/** The sentence under a control for the value in the form. */
export function useStatement(use: NeedUse, value: string[]): string {
  if (sameSet(value, use.selected)) return use.statement
  const nameOf = (slug: string) => use.options.find((o) => o.slug === slug)?.name ?? slug
  const connected = usable(use).map((o) => o.slug)
  const names = value.map(nameOf)
  if (use.mode === 'one') {
    if (value.length === 0) return use.automatic_statement
    if (use.control === 'single') return `Uses ${names[0]}.`
    return use.template.replace('{names}', joinNames(names))
  }
  if (value.length === 0) return ''
  if (sameSet(value, connected)) return use.automatic_statement
  if (use.mode === 'all') return `Uses ${joinNames(names)} (of ${connected.length} connected).`
  return use.template.replace('{names}', joinNames(names))
}

/** The read-only lines for a need: each use's sentence and note. */
export function needUseLines(need: AgentNeed): { key: string; text: string; stale: boolean }[] {
  return (need.uses ?? [])
    .map((use, index) => ({
      key: `${use.capability}:${index}`,
      text: [use.statement, use.note].filter(Boolean).join(' '),
      stale: use.stale,
    }))
    .filter((line) => line.text)
}
