/**
 * Brand profile: paste the website, get what the business is, edit it.
 *
 * Every content agent writes from this profile, so it is the first thing a
 * new workspace should have. Generating reads the site's own pages with the
 * workspace's connected AI model; every field can then be corrected by hand.
 */
import { useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type { BrandProfile } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { BrandVersions } from '@/components/AuditHistory'
import { PageTabs } from '@/components/PageTabs'
import { useToasts } from '@/components/Toasts'
import { EmptyState, ErrorState, Field, Loading, PageHeader } from '@/components/ui'
import { useResource } from '@/hooks/useResource'
import { when } from '@/lib/time'
import { browserTimeZone, isKnownTimeZone, searchTimeZones, timeZoneLabel } from '@/lib/timezones'

const TEXT_FIELDS: { key: keyof BrandProfile; label: string; hint?: string; long?: boolean }[] = [
  { key: 'business_name', label: 'Business name' },
  { key: 'summary', label: 'What the business does', hint: 'One or two sentences.', long: true },
  { key: 'industry', label: 'Industry', hint: 'Short, for example “hair salon” or “B2B software”.' },
  { key: 'tone', label: 'Tone of voice', hint: 'How your copy should sound, for example “warm and informal”.' },
  { key: 'language', label: 'Language' },
  { key: 'call_to_action', label: 'Main call to action', hint: 'What you want visitors to do, for example “Book an appointment”.' },
]

const LIST_FIELDS: { key: keyof BrandProfile; label: string; hint: string }[] = [
  { key: 'products_services', label: 'Products and services', hint: 'One per line.' },
  { key: 'audiences', label: 'Who you sell to', hint: 'One audience per line.' },
  { key: 'value_propositions', label: 'Why customers choose you', hint: 'One per line, in your own claims.' },
  { key: 'differentiators', label: 'What makes you different', hint: 'One per line.' },
  { key: 'keywords', label: 'What customers search for', hint: 'One search phrase per line.' },
  { key: 'locations', label: 'Areas you serve', hint: 'Cities or areas, one per line. Leave empty if not local.' },
  { key: 'competitors', label: 'Competitors', hint: 'Names or website domains, one per line.' },
]

type Draft = Record<string, string>

function toDraft(profile: BrandProfile | null): Draft {
  const draft: Draft = {}
  for (const field of TEXT_FIELDS) draft[field.key] = String(profile?.[field.key] ?? '')
  for (const field of LIST_FIELDS) draft[field.key] = ((profile?.[field.key] as string[] | undefined) ?? []).join('\n')
  return draft
}

export function BrandProfilePage() {
  const { canWrite } = useAuth()
  const { push, fromError } = useToasts()
  const writable = canWrite('content')
  const { data, loading, error, reload, set } = useResource((signal) => api.brandProfile(signal), [], 'brand-profile')
  const [url, setUrl] = useState('')
  const [draft, setDraft] = useState<Draft>(() => toDraft(null))
  const [busy, setBusy] = useState<'generate' | 'save' | null>(null)
  const [timezone, setTimezone] = useState('')
  const [view, setView] = useState<'profile' | 'versions'>('profile')
  // Bumped after a save or rebuild, so the versions list is fetched afresh.
  const [saves, setSaves] = useState(0)

  useEffect(() => {
    if (!data) return
    setDraft(toDraft(data.profile))
    setTimezone(data.timezone ?? '')
    setUrl((current) => current || data.profile?.source_url || data.suggested_url || '')
  }, [data])

  const generate = async () => {
    if (!url.trim()) {
      push('Enter your website address', 'warning')
      return
    }
    setBusy('generate')
    try {
      const next = await api.generateBrandProfile(url.trim())
      set(next)
      setSaves((n) => n + 1)
      push('Brand profile created from your website. Check it and correct anything that is off.', 'success')
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(null)
    }
  }

  const save = async () => {
    setBusy('save')
    try {
      const values: Record<string, unknown> = { timezone }
      for (const field of TEXT_FIELDS) values[field.key] = draft[field.key] ?? ''
      for (const field of LIST_FIELDS)
        values[field.key] = (draft[field.key] ?? '').split('\n').map((line) => line.trim()).filter(Boolean)
      set(await api.updateBrandProfile(values as Partial<BrandProfile>))
      setSaves((n) => n + 1)
      push('Brand profile saved', 'success')
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(null)
    }
  }

  if (loading && !data) return <Loading label="Loading brand profile…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const profile = data.profile

  return (
    <>
      <PageHeader
        title="Brand profile"
        description="What your business is, in one place. Every content agent writes from it — articles, ads, social posts and emails — so they sound like you."
      />
      <PageTabs
        value={view}
        options={[
          { value: 'profile', label: 'Profile' },
          { value: 'versions', label: 'Versions' },
        ]}
        onChange={setView}
      />
      {view === 'versions' ? (
        <BrandVersions
          refreshKey={String(saves)}
          writable={writable}
          onRestored={(state) => {
            set(state)
            setSaves((n) => n + 1)
          }}
        />
      ) : (
      <>

      {!profile && !writable ? (
        <EmptyState
          title="No brand profile yet"
          description="Someone with edit access to Content needs to create it, either from your website or by hand. Ask a workspace admin if you are not sure who."
        />
      ) : null}

      <section className="panel brand-generate">
        <div className="panel-head">
          <div>
            <div className="panel-title">{profile ? 'Rebuild from your website' : 'Start from your website'}</div>
            <div className="panel-subtitle">
              Reads your home page and up to five pages about your business, then summarises them. Takes under a minute.
            </div>
          </div>
        </div>
        <div className="brand-generate-body">
          <label className="search-input brand-url">
            <input
              type="url"
              placeholder="https://yourbusiness.com"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              aria-label="Your website address"
              disabled={!writable}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!writable || busy !== null || !data.can_generate}
            onClick={() => void generate()}
          >
            {busy === 'generate' ? 'Reading your website…' : profile ? 'Rebuild profile' : 'Create profile'}
          </button>
        </div>
        {!data.can_generate ? (
          <div className="banner is-warning brand-banner">
            Connect an AI model first — <Link to="/connectors">OpenAI, Anthropic Claude, Google Gemini or Perplexity</Link>.
            It reads your site and writes the profile. You can also fill in the profile by hand below.
          </div>
        ) : profile ? (
          <p className="guide-text muted small brand-note">
            Rebuilding replaces the fields below with a fresh reading of your site
            {profile.edited ? ', including the edits you made' : ''}.
          </p>
        ) : null}
      </section>

      {profile?.generated_at ? (
        <p className="guide-text muted small">
          From {profile.source_url} · {profile.pages_read?.length ?? 0} pages read · {when(profile.generated_at)}
          {profile.edited ? ' · edited by you' : ''}
        </p>
      ) : null}

      <section className="panel brand-form">
        <div className="brand-grid">
          {TEXT_FIELDS.map((field) => (
            <Field key={field.key} label={field.label} htmlFor={`bp-${field.key}`} hint={field.hint}>
              {field.long ? (
                <textarea
                  id={`bp-${field.key}`}
                  className="input"
                  rows={3}
                  value={draft[field.key] ?? ''}
                  disabled={!writable}
                  onChange={(event) => setDraft((d) => ({ ...d, [field.key]: event.target.value }))}
                />
              ) : (
                <input
                  id={`bp-${field.key}`}
                  className="input"
                  value={draft[field.key] ?? ''}
                  disabled={!writable}
                  onChange={(event) => setDraft((d) => ({ ...d, [field.key]: event.target.value }))}
                />
              )}
            </Field>
          ))}
          <Field
            label="Time zone"
            htmlFor="bp-timezone"
            hint="Social posts are scheduled at local times in this zone. Type a city near you, for example London or Mumbai."
          >
            <TimeZonePicker id="bp-timezone" value={timezone} disabled={!writable} onChange={setTimezone} />
          </Field>
          {LIST_FIELDS.map((field) => (
            <Field key={field.key} label={field.label} htmlFor={`bp-${field.key}`} hint={field.hint}>
              <textarea
                id={`bp-${field.key}`}
                className="input"
                rows={5}
                value={draft[field.key] ?? ''}
                disabled={!writable}
                onChange={(event) => setDraft((d) => ({ ...d, [field.key]: event.target.value }))}
              />
            </Field>
          ))}
        </div>
        {writable ? (
          <div className="brand-actions">
            <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={() => void save()}>
              {busy === 'save' ? 'Saving…' : 'Save profile'}
            </button>
          </div>
        ) : null}
      </section>
      </>
      )}
    </>
  )
}

/**
 * A searchable list of time zones. Type part of a city or region, pick with
 * the arrow keys and Enter (or the mouse). Only listed zones can be chosen, so
 * a typo never reaches the server; leaving the field puts back the last choice.
 */
function TimeZonePicker({
  id,
  value,
  disabled,
  onChange,
}: {
  id: string
  value: string
  disabled: boolean
  onChange: (zone: string) => void
}) {
  const [open, setOpen] = useState(false)
  /** While focused the field holds the search text; otherwise the chosen zone. */
  const [focused, setFocused] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const listId = `${id}-options`
  const matches = useMemo(() => (open ? searchTimeZones(query, 60) : []), [open, query])
  const local = browserTimeZone()
  const optionId = (index: number) => `${listId}-${index}`

  // Keep the highlighted option in view while arrowing through a long list.
  useEffect(() => {
    if (!open) return
    document.getElementById(optionId(active))?.scrollIntoView({ block: 'nearest' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, open])

  const close = () => {
    setOpen(false)
    setQuery('')
    setActive(0)
  }

  const choose = (zone: string) => {
    onChange(zone)
    close()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (!open) setOpen(true)
      else setActive((i) => Math.min(i + 1, Math.max(matches.length - 1, 0)))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive((i) => Math.max(i - 1, 0))
    } else if (event.key === 'Enter') {
      // Never submit or lose the choice by pressing Enter in this field.
      event.preventDefault()
      const zone = matches[active]
      if (open && zone) choose(zone)
    } else if (event.key === 'Escape') {
      if (open) {
        event.preventDefault()
        close()
      }
    }
  }

  const display = value ? timeZoneLabel(value) : timeZoneLabel('')

  return (
    <div style={{ position: 'relative' }}>
      <input
        id={id}
        className="input"
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && matches[active] ? optionId(active) : undefined}
        autoComplete="off"
        spellCheck={false}
        placeholder={focused ? display : 'Type a city, for example London'}
        value={focused ? query : display}
        disabled={disabled}
        onFocus={() => setFocused(true)}
        onClick={() => setOpen(true)}
        onBlur={() => {
          setFocused(false)
          close()
        }}
        onChange={(event) => {
          setQuery(event.target.value)
          setActive(0)
          setOpen(true)
        }}
        onKeyDown={onKeyDown}
      />
      {open && matches.length > 0 ? (
        <ul
          id={listId}
          role="listbox"
          aria-label="Time zones"
          className="menu-panel is-start"
          style={{ maxWidth: 'none', maxHeight: 280, overflowY: 'auto', margin: 0, listStyle: 'none' }}
        >
          {matches.map((zone, index) => (
            <li
              key={zone}
              id={optionId(index)}
              role="option"
              aria-selected={zone === value || (!value && zone === 'UTC')}
              className={`menu-item${index === active ? ' is-checked' : ''}`}
              // Keep focus in the field so the click lands before blur closes the list.
              onMouseDown={(event) => event.preventDefault()}
              onMouseEnter={() => setActive(index)}
              onClick={() => choose(zone)}
            >
              <span className="menu-item-label">{timeZoneLabel(zone)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {open && matches.length === 0 ? (
        <div id={listId} className="menu-panel is-start" role="status" style={{ maxWidth: 'none' }}>
          <span className="menu-item-hint" style={{ padding: '7px 10px' }}>
            No time zone matches that. Try the name of a big city near you.
          </span>
        </div>
      ) : null}
      {!disabled && local && local !== value && isKnownTimeZone(local) ? (
        <button type="button" className="link-button small" style={{ marginTop: 6 }} onClick={() => onChange(local)}>
          Use this computer&apos;s time zone: {timeZoneLabel(local)}
        </button>
      ) : null}
    </div>
  )
}
