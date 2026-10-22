/**
 * Connect / manage one integration.
 *
 * Written for marketers, not developers. The server says how each
 * integration is best connected (`connector.connect.mode`), and the drawer
 * shows only that path:
 *
 * - sign_in   — one "Sign in with Google" button, then pick the property,
 *               site or account. No fields.
 * - wordpress — type the site address, approve on the WordPress site.
 * - key       — three plain steps with a button to the exact vendor page,
 *               a format check as you paste, and an automatic test.
 * - form      — a few plain fields (the Site Crawler needs one).
 * - advanced  — developer integrations, labelled as such.
 *
 * Client ids, secrets, refresh and developer tokens never appear on the
 * default path; where a manual route still exists it sits under Advanced.
 * Secrets are write-only: the API never returns them.
 *
 * Until the platform's sign-in for a vendor is switched on (Portal Admin →
 * Sign-in providers), the button shows switched off with one sentence and
 * "Notify my admin". Admins also see who can switch it on and, as a
 * secondary link, the connect-with-your-own-app-credentials form.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type { ConnectorOut, CredentialField, ModelList, ModelOption, OAuthChoice } from '@/api/types'
import { useConfirm } from '@/components/Confirm'
import { BetaPill } from '@/components/BetaPill'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useToasts } from '@/components/Toasts'
import { CheckIcon, ChevronDownIcon, ExternalIcon, LockIcon } from '@/components/icons'
import { Drawer, Field, FormSection } from '@/components/ui'
import { categoryTitle } from '@/lib/plain'
import { softenErrorMessage } from '@/lib/softenError'

const SIGN_IN_LINK_PROBLEM = 'The sign-in page address was not valid. Please try again or contact support.'

/** Only an https sign-in page (no embedded credentials) is ever navigated to. */
function isHttpsAuthorizeUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && url.hostname !== '' && url.username === '' && url.password === ''
  } catch {
    return false
  }
}

/** What the browser came back with after a sign-in (see OAuthReturn). */
export interface ConnectNotice {
  kind: 'connected' | 'choose' | 'error'
  message?: string
}

/** Our own query parameters, removed before choosing where to come back to. */
export const OAUTH_PARAMS = ['connected', 'oauth_choose', 'oauth_error', 'oauth_slug'] as const

/** Tell any screen showing integrations that one changed. */
export function announceConnectorsChanged() {
  window.dispatchEvent(new Event('connectors:changed'))
}

function currentReturnPath(): string {
  const url = new URL(window.location.href)
  for (const key of OAUTH_PARAMS) url.searchParams.delete(key)
  const query = url.searchParams.toString()
  return `${url.pathname}${query ? `?${query}` : ''}`
}

/**
 * Essential: required, and with nothing sensible to pre-fill. AI model
 * pickers are always shown, including the optional image model, because
 * which model writes (and draws) is the main choice on those connections.
 */
function isEssential(field: CredentialField): boolean {
  return field.type === 'model' || (field.required && !field.default)
}

/** A model list that could not be loaded at all (network, server error). */
function failedModelList(message: string): ModelList {
  return { listable: true, models: [], suggested: '', source: '', error: message, stale: '', models_url: '' }
}

/** The address field of a self-hosted model server (vLLM, Ollama). */
const BASE_URL_FIELD = 'baseUrl'

/** Enough of an address to be worth asking the server: scheme and host. */
const ADDRESS_SHAPE = /^https?:\/\/[^\s/?#]+/i

/**
 * The name a marketer sees for a model. The vendor's own label when it gives
 * one; otherwise the id without its organisation prefix ("org/name" from a
 * vLLM server), unless that would make two entries look the same.
 */
function friendlyModelName(option: ModelOption, options: ModelOption[]): string {
  if (option.label && option.label !== option.id) return option.label
  const short = option.id.slice(option.id.lastIndexOf('/') + 1)
  if (!short || short === option.id) return option.id
  const clash = options.some((other) => other !== option && other.id.slice(other.id.lastIndexOf('/') + 1) === short)
  return clash ? option.id : short
}

function GoogleMark() {
  return (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62z" />
      <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18z" />
      <path fill="#FBBC05" d="M3.97 10.72A5.4 5.4 0 0 1 3.68 9c0-.6.1-1.18.29-1.72V4.95H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.05l3.01-2.33z" />
      <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58z" />
    </svg>
  )
}

/**
 * The vendor's mark on its sign-in button, for providers without an official
 * multi-colour mark here: a small badge in the vendor's colour. Data-driven
 * by `guide.provider`; unknown providers get none.
 */
const PROVIDER_BADGES: Record<string, { bg: string; fg: string; text: string }> = {
  meta: { bg: '#1877F2', fg: '#fff', text: 'f' },
  linkedin: { bg: '#0A66C2', fg: '#fff', text: 'in' },
  linkedin_pages: { bg: '#0A66C2', fg: '#fff', text: 'in' },
  x: { bg: '#000', fg: '#fff', text: 'X' },
  pinterest: { bg: '#E60023', fg: '#fff', text: 'P' },
  tiktok: { bg: '#000', fg: '#fff', text: '♪' },
  bing_webmaster: { bg: '#008373', fg: '#fff', text: 'b' },
  // Phase 2B: business apps.
  slack: { bg: '#4A154B', fg: '#fff', text: '#' },
  hubspot: { bg: '#FF7A59', fg: '#fff', text: 'H' },
  salesforce: { bg: '#00A1E0', fg: '#fff', text: 'sf' },
  mailchimp: { bg: '#FFE01B', fg: '#241C15', text: 'M' },
  klaviyo: { bg: '#232426', fg: '#fff', text: 'K' },
  shopify: { bg: '#95BF47', fg: '#fff', text: 'S' },
  github: { bg: '#24292F', fg: '#fff', text: 'GH' },
  bitbucket: { bg: '#0052CC', fg: '#fff', text: 'B' },
  webflow: { bg: '#146EF5', fg: '#fff', text: 'W' },
}

function MicrosoftMark() {
  return (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <rect x="0" y="0" width="8.5" height="8.5" fill="#F25022" />
      <rect x="9.5" y="0" width="8.5" height="8.5" fill="#7FBA00" />
      <rect x="0" y="9.5" width="8.5" height="8.5" fill="#00A4EF" />
      <rect x="9.5" y="9.5" width="8.5" height="8.5" fill="#FFB900" />
    </svg>
  )
}

function ProviderMark({ provider }: { provider: string }) {
  if (provider === 'google') return <GoogleMark />
  if (provider === 'microsoft') return <MicrosoftMark />
  const badge = PROVIDER_BADGES[provider]
  if (!badge) return null
  return (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <rect width="18" height="18" rx="4" fill={badge.bg} />
      <text
        x="9"
        y="13"
        textAnchor="middle"
        fontSize={badge.text.length > 1 ? 9 : 12}
        fontWeight="700"
        fontFamily="Arial, Helvetica, sans-serif"
        fill={badge.fg}
      >
        {badge.text}
      </text>
    </svg>
  )
}

export function ConnectDialog({
  connector,
  onClose,
  onConnected,
  notice,
}: {
  connector: ConnectorOut
  onClose: () => void
  onConnected: () => Promise<void>
  /** Set when the browser has just come back from a sign-in. */
  notice?: ConnectNotice
}) {
  const { push, fromError } = useToasts()
  const confirm = useConfirm()
  const [current, setCurrent] = useState<ConnectorOut>(connector)
  const guide = current.connect ?? ({ mode: 'form' } as ConnectorOut['connect'])
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const field of connector.fields) {
      if (field.is_oauth) continue
      initial[field.key] = connector.hints[field.key] ?? field.default ?? ''
    }
    return initial
  })
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [tried, setTried] = useState(false)
  /** The paste-it-yourself route for sign-in and WordPress integrations. */
  const [manual, setManual] = useState(false)
  /** Inline result of the last attempt, so it is read in place. */
  const [problem, setProblem] = useState<string>(notice?.kind === 'error' ? notice.message ?? '' : '')
  const [justConnected, setJustConnected] = useState(notice?.kind === 'connected')
  const [picking, setPicking] = useState(
    notice?.kind === 'choose' || (guide.mode === 'sign_in' && guide.pending_choice),
  )

  const signInPath = guide.mode === 'sign_in' && guide.sign_in_available && !manual
  /** Sign-in is the way, but it is not switched on yet: no form, ever, by default. */
  const pendingPath = guide.mode === 'sign_in' && !!guide.setup_pending && !manual
  const wordpressPath = guide.mode === 'wordpress' && !manual
  const signedIn = guide.mode === 'sign_in' && guide.signed_in
  const hidden = new Set(guide.sign_in_hidden_fields ?? [])
  /** "Use the included access": the platform supplies the key (PageSpeed, Places). */
  const platformKeyPath = guide.mode === 'key' && !!guide.platform_key_available && !manual && !current.connected
  /** Guided steps: the key path, a sign-in's key route under Advanced, or a developer integration's plain steps. */
  const showSteps =
    (guide.mode === 'key' && !platformKeyPath) ||
    (guide.mode === 'sign_in' && manual && (guide.steps ?? []).length > 0) ||
    (guide.mode === 'advanced' && (guide.steps ?? []).length > 0)

  const allFields = connector.fields.filter((field) => !field.is_oauth)
  // On the sign-in path, only the settings the picker does not set remain.
  // With the included key, the key field is not asked for at all.
  const fields = signInPath
    ? allFields.filter((field) => !hidden.has(field.key))
    : platformKeyPath
      ? allFields.filter((field) => field.key !== guide.key_field)
      : allFields
  const keyField =
    showSteps && guide.key_field ? allFields.find((field) => field.key === guide.key_field) : undefined
  const essential = fields.filter((field) => isEssential(field) && field !== keyField)
  const advanced = fields.filter((field) => !isEssential(field))

  // Saving replaces every stored credential (services/connectors.connect),
  // and secrets are never sent back, so a secret must be typed again on edit
  // — except on a signed-in connection, whose tokens the server keeps.
  const isBlank = (field: CredentialField) => !(values[field.key] ?? '').trim()
  const secretKept = (field: CredentialField) => signInPath && signedIn && field.type === 'password'
  const missingRequired = fields.filter((field) => field.required && isBlank(field) && !secretKept(field))

  const keyValue = keyField ? (values[keyField.key] ?? '').trim() : ''
  const keyPattern = useMemo(() => {
    try {
      return guide.key_pattern ? new RegExp(guide.key_pattern) : null
    } catch {
      return null
    }
  }, [guide.key_pattern])
  const keyLooksRight = keyValue && keyPattern ? keyPattern.test(keyValue) : null

  // ── AI models: the vendor's own live list ─────────────────────────────
  // Loaded with the key as soon as it is pasted (before saving), or with the
  // saved key when managing an existing connection. Nothing here names a
  // model: the list, and the suggestion, come from the vendor.
  // A self-hosted server (vLLM, Ollama) may need no key: its list loads as
  // soon as its address is typed, with the key only if one was given.
  const modelFields = allFields.filter((field) => field.type === 'model')
  const hasModelFields = modelFields.length > 0
  const apiKeyField = guide.key_field || 'apiKey'
  const typedKey = (values[apiKeyField] ?? '').trim()
  const selfHosted = hasModelFields && allFields.some((field) => field.key === BASE_URL_FIELD)
  const typedBase = selfHosted ? (values[BASE_URL_FIELD] ?? '').trim() : ''
  const typedAny = selfHosted ? !!typedBase : !!typedKey
  const [models, setModels] = useState<ModelList | null>(null)
  const [modelsLoading, setModelsLoading] = useState(false)
  /** Fields switched to "Type a model name". */
  const [typingModel, setTypingModel] = useState<Record<string, boolean>>({})

  useEffect(() => {
    if (!hasModelFields) return
    if (!typedAny && !current.connected) {
      setModels(null)
      setModelsLoading(false)
      return
    }
    // Wait for something that looks like a whole key before asking the vendor.
    if (typedKey && keyPattern && !keyPattern.test(typedKey)) return
    // And for a whole address before asking a self-hosted server.
    if (typedBase && !ADDRESS_SHAPE.test(typedBase)) return
    const typed: Record<string, string> = {}
    if (typedKey) typed[apiKeyField] = typedKey
    if (typedBase) typed[BASE_URL_FIELD] = typedBase
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      setModelsLoading(true)
      const load = Object.keys(typed).length
        ? api.connectorModelsFor(connector.slug, typed, controller.signal)
        : api.connectorModels(connector.slug, controller.signal)
      load
        .then((list) => {
          setModels(list)
          // Pre-select the vendor's newest writing model, only when the
          // vendor said which one that is, and only into an empty field.
          if (list.suggested) {
            setValues((prev) => {
              const next = { ...prev }
              for (const field of modelFields) {
                if (field.model_kind !== 'image' && !(next[field.key] ?? '').trim()) next[field.key] = list.suggested
              }
              return next
            })
          }
        })
        .catch((caught) => {
          if (controller.signal.aborted) return
          const fallback = 'The model list could not be loaded. You can type a model name instead.'
          setModels(failedModelList(caught instanceof Error ? softenErrorMessage(caught.message, fallback) : fallback))
        })
        .finally(() => {
          if (!controller.signal.aborted) setModelsLoading(false)
        })
    }, typedBase ? 600 : typedKey ? 400 : 0)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connector.slug, hasModelFields, typedKey, typedBase, current.connected])

  // Test as soon as a key is pasted, once the pasted value is in state —
  // when it looks well formed and nothing else is still to fill in.
  const [pasted, setPasted] = useState(false)
  useEffect(() => {
    if (!pasted || !keyValue) return
    setPasted(false)
    if (keyLooksRight === false || missingRequired.length > 0 || busy) return
    void save({ auto: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pasted, keyValue])

  const finish = async (message: string) => {
    push(message, 'success')
    announceConnectorsChanged()
    await onConnected()
  }

  const save = async (opts: { auto?: boolean } = {}) => {
    setTried(true)
    setProblem('')
    if (missingRequired.length > 0) {
      if (opts.auto) return
      if (missingRequired.some((field) => !isEssential(field))) setShowAdvanced(true)
      push(`Fill in: ${missingRequired.map((field) => field.label).join(', ')}`, 'warning')
      return
    }
    setBusy(true)
    try {
      const submitted = signInPath
        ? Object.fromEntries(fields.map((field) => [field.key, values[field.key] ?? '']))
        : platformKeyPath
          ? {
              ...Object.fromEntries(fields.map((field) => [field.key, values[field.key] ?? ''])),
              keySource: 'platform',
            }
          : values
      await api.connect(connector.slug, submitted)
      await finish(current.connected ? `${connector.name} updated` : `${connector.name} connected`)
    } catch (caught) {
      // The server checked the details with the provider; its message says
      // what was wrong, and it is shown where the person is looking.
      setProblem(
        caught instanceof Error
          ? softenErrorMessage(caught.message, 'That did not work. Try again.')
          : 'That did not work. Try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  /** Answers to the sign-in's own questions (Shopify store, Salesforce sandbox). */
  const [signInInputs, setSignInInputs] = useState<Record<string, string>>(() =>
    Object.fromEntries((guide.sign_in_inputs ?? []).map((item) => [item.key, item.value ?? ''])),
  )

  const startSignIn = async (opts: { reuse?: boolean; slug?: string } = {}) => {
    const slug = opts.slug ?? connector.slug
    const missingInput = slug === connector.slug
      ? (guide.sign_in_inputs ?? []).find((item) => item.kind === 'text' && !(signInInputs[item.key] ?? '').trim())
      : undefined
    if (missingInput) {
      setProblem(`Enter ${missingInput.label.toLowerCase()} first.`)
      return
    }
    setBusy(true)
    setProblem('')
    try {
      const result = await api.oauthStart(slug, {
        return_path: currentReturnPath(),
        reuse: opts.reuse ?? false,
        ...(slug === connector.slug && (guide.sign_in_inputs ?? []).length ? { inputs: signInInputs } : {}),
      })
      if (result.authorize_url) {
        if (!isHttpsAuthorizeUrl(result.authorize_url)) {
          setProblem(SIGN_IN_LINK_PROBLEM)
          return
        }
        window.location.assign(result.authorize_url)
        return
      }
      // An existing sign-in already covered it.
      if (slug !== connector.slug) {
        const name = guide.also_connect.find((link) => link.slug === slug)?.name ?? 'It'
        announceConnectorsChanged()
        push(
          result.status === 'connected'
            ? `${name} connected with the same ${guide.provider_label} account`
            : `Signed in. Open ${name} to choose what to connect.`,
          'success',
        )
        await onConnected()
        return
      }
      if (result.status === 'connected') {
        await finish(`${connector.name} connected`)
      } else {
        setPicking(true)
      }
    } catch (caught) {
      setProblem(
        caught instanceof Error
          ? softenErrorMessage(caught.message, 'Sign-in could not start. Try again.')
          : 'Sign-in could not start. Try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    const ok = await confirm({
      title: `Disconnect ${connector.name}?`,
      message: 'The saved details for this integration will be deleted.',
      detail: 'Agents that use it will pause their work until it is connected again.',
      confirmLabel: 'Disconnect',
      tone: 'danger',
      icon: <ConnectorIcon slug={connector.slug} name={connector.name} size={40} />,
    })
    if (!ok) return
    setRemoving(true)
    try {
      await api.disconnect(connector.slug)
      push(`${connector.name} disconnected`, 'info')
      announceConnectorsChanged()
      await onConnected()
    } catch (caught) {
      fromError(caught)
    } finally {
      setRemoving(false)
    }
  }

  /** A model picker: the vendor's list, with "Type a model name" as the fallback. */
  const renderModelField = (field: CredentialField) => {
    const id = `cx-${connector.slug}-${field.key}`
    const value = values[field.key] ?? ''
    const set = (next: string) => setValues((prev) => ({ ...prev, [field.key]: next }))
    const forImage = field.model_kind === 'image'
    const invalid = tried && field.required && !value.trim()
    const listed = (models?.models ?? []).filter((option) => option.kinds.includes(forImage ? 'image' : 'text'))
    // The vendor's recommendation first, then its own order.
    const recommended = forImage ? '' : models?.suggested ?? ''
    const options = recommended
      ? [...listed.filter((option) => option.id === recommended), ...listed.filter((option) => option.id !== recommended)]
      : listed
    const listUsable = !!models && models.listable && !models.error && options.length > 0
    const typing = !!typingModel[field.key] || (models !== null && !listUsable)
    const known = options.some((option) => option.id === value)
    // Messages about the list are said once, on the writing model's field.
    const speaks = !forImage
    const errorText = invalid ? (forImage ? undefined : 'Choose a model') : undefined

    let control: ReactNode
    if (models === null && !typingModel[field.key]) {
      control = (
        <p className="guide-text muted small" id={id}>
          {modelsLoading
            ? selfHosted
              ? 'Loading the models your server offers…'
              : `Loading the models your ${connector.name} account can use…`
            : selfHosted
              ? 'Enter your server’s address above first.'
              : `Paste your ${connector.name} API key above first.`}
        </p>
      )
    } else if (typing) {
      control = (
        <input
          id={id}
          name={id}
          className={`input${invalid ? ' is-invalid' : ''}`}
          type="text"
          placeholder={forImage ? 'Leave blank for no pictures' : 'Model name'}
          autoComplete="off"
          spellCheck={false}
          aria-required={field.required}
          aria-invalid={invalid || undefined}
          value={value}
          onChange={(event) => set(event.target.value)}
        />
      )
    } else {
      control = (
        <select
          id={id}
          name={id}
          className={`input${invalid ? ' is-invalid' : ''}`}
          aria-required={field.required}
          aria-invalid={invalid || undefined}
          value={value}
          onChange={(event) => set(event.target.value)}
        >
          <option value="">{forImage ? 'No pictures' : 'Choose a model'}</option>
          {value && !known ? <option value={value}>{value} (no longer offered)</option> : null}
          {options.map((option) => (
            <option key={option.id} value={option.id}>
              {friendlyModelName(option, options)}
              {option.id === recommended ? ' (recommended)' : ''}
            </option>
          ))}
        </select>
      )
    }
    // The exact id is for people who need it (support, a proxy's allow-list);
    // everyone else sees the name above.
    const showId = !typing && listUsable && !!value

    return (
      <Field
        key={field.key}
        label={field.label}
        required={field.required}
        htmlFor={id}
        error={errorText}
        hint={field.help_text || undefined}
      >
        {speaks && models?.stale ? (
          <div className="connect-key-check is-warning" role="status">
            {models.stale}
          </div>
        ) : null}
        {control}
        {speaks && models?.error ? <div className="connect-key-check is-warning">{models.error}</div> : null}
        {showId ? (
          <details className="advanced-disclosure">
            <summary>Advanced: model ID</summary>
            <code className="mono-sub">{value}</code>
          </details>
        ) : null}
        <div className="connect-row">
          {models?.models_url && (typing || !models.listable) ? (
            <a className="btn btn-link btn-sm" href={models.models_url} target="_blank" rel="noreferrer noopener">
              Open {connector.name}&rsquo;s models page
              <ExternalIcon size={14} />
            </a>
          ) : null}
          {listUsable ? (
            <button
              type="button"
              className="btn btn-link btn-sm"
              onClick={() => setTypingModel((prev) => ({ ...prev, [field.key]: !prev[field.key] }))}
            >
              {typingModel[field.key] ? 'Choose from the list' : 'Type a model name'}
            </button>
          ) : models === null && !typingModel[field.key] && !modelsLoading ? (
            <button
              type="button"
              className="btn btn-link btn-sm"
              onClick={() => setTypingModel((prev) => ({ ...prev, [field.key]: true }))}
            >
              Type a model name
            </button>
          ) : null}
        </div>
      </Field>
    )
  }

  const renderField = (field: CredentialField, extra?: { onPaste?: () => void; feedback?: ReactNode }) => {
    if (field.type === 'model') return renderModelField(field)
    const id = `cx-${connector.slug}-${field.key}`
    const reenter = current.connected && field.type === 'password' && !secretKept(field)
    const invalid = tried && field.required && isBlank(field) && !secretKept(field)
    const serverAddress = selfHosted && field.key === BASE_URL_FIELD
    return (
      <Field
        key={field.key}
        label={field.label}
        required={field.required}
        htmlFor={id}
        error={invalid ? 'Required' : undefined}
        hint={
          [
            reenter ? 'Saved values are never shown. Enter it again to save changes.' : '',
            serverAddress ? 'Your server’s https address.' : '',
            field.help_text,
          ]
            .filter(Boolean)
            .join(' ') || undefined
        }
      >
        <input
          id={id}
          name={id}
          className={`input${invalid ? ' is-invalid' : ''}`}
          type={field.type === 'password' ? 'password' : serverAddress ? 'url' : 'text'}
          inputMode={field.type === 'number' ? 'numeric' : serverAddress ? 'url' : undefined}
          placeholder={field.placeholder}
          autoComplete={field.type === 'password' ? 'new-password' : 'off'}
          aria-required={field.required}
          aria-invalid={invalid || undefined}
          value={values[field.key] ?? ''}
          onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
          onPaste={extra?.onPaste}
        />
        {extra?.feedback}
      </Field>
    )
  }

  const advancedBlock = (list: CredentialField[], label = 'Advanced settings') =>
    list.length ? (
      <div className="advanced">
        <button
          type="button"
          className="advanced-toggle"
          aria-expanded={showAdvanced}
          onClick={() => setShowAdvanced((value) => !value)}
        >
          <ChevronDownIcon size={16} />
          {label}
          <span className="advanced-hint">
            {list.filter((f) => f.required).length
              ? 'Pre-filled — change only if you need to'
              : 'Optional — most people can skip these'}
          </span>
        </button>
        {showAdvanced ? <div className="advanced-body">{list.map((field) => renderField(field))}</div> : null}
      </div>
    ) : null

  // ── Footer ────────────────────────────────────────────────────────────
  const settingsOnly = signInPath && signedIn && current.connected && !picking
  const showSave =
    !picking && !wordpressPath && !pendingPath && (!signInPath || (settingsOnly && fields.length > 0))
  const saveLabel = busy
    ? 'Checking the connection…'
    : settingsOnly
      ? 'Save settings'
      : current.connected
        ? 'Save changes'
        : platformKeyPath
          ? 'Use the included access'
          : guide.mode === 'key'
            ? 'Connect and test'
            : 'Connect'

  return (
    <Drawer
      title={current.connected ? `Manage ${connector.name}` : `Connect ${connector.name}`}
      subtitle={
        guide.mode === 'advanced' ? (
          <>
            {categoryTitle(connector.category)} · <span className="connect-badge">Advanced</span>
          </>
        ) : (
          <>
            {categoryTitle(connector.category)}
            <BetaPill verification={connector.verification} />
          </>
        )
      }
      icon={<ConnectorIcon slug={connector.slug} name={connector.name} size={36} />}
      onClose={onClose}
      actions={
        <>
          {current.connected ? (
            <button
              type="button"
              className="btn btn-ghost danger"
              disabled={busy || removing}
              onClick={() => void remove()}
            >
              {removing ? 'Disconnecting…' : 'Disconnect'}
            </button>
          ) : null}
          <div className="dialog-actions-spacer" />
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {showSave ? 'Cancel' : 'Close'}
          </button>
          {showSave ? (
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void save()}>
              {saveLabel}
            </button>
          ) : null}
        </>
      }
    >
      {/* One sentence on the default sign-in path: the access summary (or why it is switched off). */}
      {connector.description && !(signInPath && !current.connected) && !pendingPath ? (
        <p className="drawer-lede">{connector.description}</p>
      ) : null}

      {justConnected && current.connected ? (
        <div className="banner is-success" role="status">
          <strong>{connector.name} is connected.</strong> Your agents can use it now.
        </div>
      ) : null}

      {problem ? (
        <div className="banner is-danger" role="alert">
          {problem}
        </div>
      ) : null}

      {guide.admin_note ? <div className="connect-admin-note">{guide.admin_note}</div> : null}

      {selfHosted ? (
        <div className="banner is-warning" role="note">
          <strong>Your data goes to your own server.</strong> Prompts, briefs and the content your agents write
          are sent to the server address below, which your company runs. Only use a server you trust and that
          asks for a key.
        </div>
      ) : null}

      {/* ── Sign-in not switched on yet ───────────────────────────────── */}
      {pendingPath ? (
        <SetupPendingPanel connector={current} onDeveloper={() => setManual(true)} onNavigate={onClose} />
      ) : null}

      {/* ── Sign in with <Vendor> ─────────────────────────────────────── */}
      {signInPath ? (
        picking ? (
          <ChoicePicker
            connector={connector}
            noun={guide.choice_noun || 'account'}
            providerLabel={guide.provider_label}
            account={guide.signed_in_account}
            onSignInAgain={() => void startSignIn()}
            onChosen={async (record) => {
              // Stay open on the result, which offers the other
              // integrations the same sign-in can connect.
              setCurrent(record)
              setPicking(false)
              setJustConnected(true)
              push(`${connector.name} connected`, 'success')
              announceConnectorsChanged()
            }}
          />
        ) : (
          <SignInPanel
            connector={current}
            busy={busy}
            inputs={signInInputs}
            onInput={(key, value) => setSignInInputs((prev) => ({ ...prev, [key]: value }))}
            onSignIn={(reuse) => void startSignIn({ reuse })}
            onChange={() => setPicking(true)}
            onAlsoConnect={(slug) => void startSignIn({ slug, reuse: true })}
          />
        )
      ) : null}

      {/* ── WordPress: approve on the site ────────────────────────────── */}
      {wordpressPath ? (
        <WordPressPanel
          connector={current}
          initialSite={connector.hints.siteUrl ?? ''}
          onError={setProblem}
        />
      ) : null}

      {/* ── Included access: the platform's own key, one click ────────── */}
      {platformKeyPath ? (
        <section className="connect-sign-in">
          <p className="guide-text">
            This platform includes access to {connector.name}, so there is no key to create.
            {essential.length ? ' Fill in the details below, then connect.' : ''}
          </p>
          {guide.platform_key_summary ? (
            <p className="guide-text connect-access">{guide.platform_key_summary}</p>
          ) : null}
        </section>
      ) : null}

      {/* ── Guided key steps ──────────────────────────────────────────── */}
      {showSteps ? (
        <section className="connect-steps" aria-label={`How to connect ${connector.name}`}>
          <ol className="connect-step-list">
            {guide.steps.map((step, index) => (
              <li key={step} className="connect-step">
                <span className="connect-step-number" aria-hidden="true">
                  {index + 1}
                </span>
                <div className="connect-step-body">
                  <p>{step}</p>
                  {index === 0 && guide.key_url ? (
                    <a className="btn btn-secondary btn-sm" href={guide.key_url} target="_blank" rel="noreferrer noopener">
                      {guide.key_url_label || `Open ${connector.name}`}
                      <ExternalIcon size={14} />
                    </a>
                  ) : null}
                  {index === guide.steps.length - 1 && keyField
                    ? renderField(keyField, {
                        // Test as soon as a key is pasted (see the effect above).
                        onPaste: () => setPasted(true),
                        feedback: keyValue ? (
                          keyLooksRight === false ? (
                            <div className="connect-key-check is-warning">
                              That does not look like a {connector.name} key ({guide.key_hint}). Check you copied all
                              of it.
                            </div>
                          ) : keyLooksRight ? (
                            <div className="connect-key-check is-ok">
                              <CheckIcon size={14} /> Looks right
                            </div>
                          ) : null
                        ) : guide.key_hint ? (
                          <div className="connect-key-check">{guide.key_hint}.</div>
                        ) : null,
                      })
                    : null}
                </div>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {/* ── The form: key extras, plain forms, advanced, manual routes ── */}
      {(!signInPath && !wordpressPath && !pendingPath) || (settingsOnly && fields.length > 0) ? (
        <>
          {guide.mode !== 'key' && connector.requirements.length > 0 && !settingsOnly ? (
            <FormSection title="Check before you start">
              <ul className="checklist">
                {connector.requirements.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </FormSection>
          ) : null}

          {guide.mode === 'advanced' && !(guide.steps ?? []).length ? (
            <p className="guide-text muted small">
              This is a developer integration. If someone set up a webhook or server for you, they can fill these in.
            </p>
          ) : null}

          <FormSection
            title={
              settingsOnly
                ? 'More settings'
                : guide.mode === 'key'
                  ? essential.length
                    ? 'A few more details'
                    : 'Settings'
                  : essential.length
                    ? 'Your details'
                    : 'Settings'
            }
            description={
              !settingsOnly && guide.mode !== 'key' && connector.docs_url ? (
                <>
                  Find these in your {connector.name} account.{' '}
                  <a href={connector.docs_url} target="_blank" rel="noreferrer noopener">
                    {connector.name} help
                  </a>
                </>
              ) : undefined
            }
          >
            {essential.map((field) => renderField(field))}
            {!essential.length && !advanced.length && guide.mode !== 'key' ? (
              <p className="guide-text muted">Nothing to fill in.</p>
            ) : null}
            {advancedBlock(advanced, settingsOnly ? 'Optional settings' : 'Advanced settings')}
          </FormSection>
        </>
      ) : null}

      {/* ── The manual route, for sign-in and WordPress integrations ──── */}
      {(guide.mode === 'sign_in' && guide.sign_in_available) || guide.mode === 'wordpress' ? (
        <div className="connect-manual-toggle">
          <button type="button" className="btn btn-link btn-sm" onClick={() => setManual((value) => !value)}>
            {manual
              ? `Back to ${guide.mode === 'wordpress' ? 'approving on your site' : guide.button_label}`
              : guide.mode === 'wordpress'
                ? 'Advanced: enter an application password yourself'
                : guide.manual_label || `Advanced: connect with your own ${guide.provider_label} Cloud app`}
          </button>
        </div>
      ) : null}
      {guide.mode === 'sign_in' && guide.setup_pending && manual ? (
        <div className="connect-manual-toggle">
          <button type="button" className="btn btn-link btn-sm" onClick={() => setManual(false)}>
            Back
          </button>
        </div>
      ) : null}
      {/* A vendor whose guided key is the route until its sign-in is on (Bing, Mailchimp, Klaviyo). */}
      {guide.mode === 'key' && (guide.admin_setup_url || guide.admin_hint) ? (
        <AdminSetupNote guide={guide} onNavigate={onClose} />
      ) : null}
      {guide.mode === 'key' && guide.platform_key_available && !current.connected ? (
        <div className="connect-manual-toggle">
          <button type="button" className="btn btn-link btn-sm" onClick={() => setManual((value) => !value)}>
            {manual ? 'Back to the included access' : 'Advanced: use my own key'}
          </button>
        </div>
      ) : null}

      {pendingPath ? null : (
        <div className="security-note">
          <LockIcon size={16} />
          <span>
            {guide.mode === 'sign_in' && signInPath
              ? 'Stored encrypted. You can disconnect at any time.'
              : `Where ${connector.name} allows it, these details are checked before they are saved. They are encrypted, never shown again and never written to logs.`}
          </span>
        </div>
      )}
    </Drawer>
  )
}

// ── Sign-in panel ──────────────────────────────────────────────────────────
function SignInPanel({
  connector,
  busy,
  inputs,
  onInput,
  onSignIn,
  onChange,
  onAlsoConnect,
}: {
  connector: ConnectorOut
  busy: boolean
  inputs: Record<string, string>
  onInput: (key: string, value: string) => void
  onSignIn: (reuse: boolean) => void
  onChange: () => void
  onAlsoConnect: (slug: string) => void
}) {
  const guide = connector.connect
  const questions = guide.sign_in_inputs ?? []
  // The label of what was picked (a channel name rather than its id) when
  // the server recorded one; otherwise the first value the picker set.
  const chosen =
    connector.hints.oauthChoice ||
    Object.entries(connector.hints)
      .filter(([key]) => (guide.sign_in_hidden_fields ?? []).includes(key))
      .map(([, value]) => value)
      .filter(Boolean)[0]
  const questionFields = questions.length ? (
    <div className="connect-sign-in-inputs">
      {questions.map((item) => {
        const id = `si-${connector.slug}-${item.key}`
        return item.kind === 'toggle' ? (
          <label key={item.key} className="connect-toggle" htmlFor={id}>
            <input
              id={id}
              type="checkbox"
              checked={(inputs[item.key] ?? '') === 'true'}
              onChange={(event) => onInput(item.key, event.target.checked ? 'true' : '')}
            />
            <span>{item.label}</span>
            {item.help_text ? <span className="guide-text muted small"> {item.help_text}</span> : null}
          </label>
        ) : (
          <Field key={item.key} label={item.label} htmlFor={id} hint={item.help_text || undefined}>
            <input
              id={id}
              className="input"
              type="text"
              inputMode="url"
              autoComplete="off"
              placeholder={item.placeholder}
              value={inputs[item.key] ?? ''}
              onChange={(event) => onInput(item.key, event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') onSignIn(false)
              }}
            />
          </Field>
        )
      })}
    </div>
  ) : null

  if (connector.connected && guide.signed_in) {
    return (
      <section className="connect-signed-in">
        <div className="connect-signed-in-line">
          <CheckIcon size={16} />
          <span>
            Signed in{guide.signed_in_account ? <> as <strong>{guide.signed_in_account}</strong></> : null}
            {chosen ? (
              <>
                {' '}
                · {guide.choice_noun}: <strong>{chosen}</strong>
              </>
            ) : null}
          </span>
        </div>
        {guide.sign_in_note ? <p className="guide-text connect-access">{guide.sign_in_note}</p> : null}
        <div className="connect-row">
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onChange}>
            Change {guide.choice_noun}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onSignIn(false)}>
            Sign in again
          </button>
        </div>
        {guide.also_connect.length ? (
          <div className="connect-also">
            <div className="guide-title">Also connect with this {guide.provider_label} account</div>
            <div className="connect-row">
              {guide.also_connect.map((link) => (
                <button
                  key={link.slug}
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={busy}
                  onClick={() => onAlsoConnect(link.slug)}
                >
                  <ConnectorIcon slug={link.slug} name={link.name} size={16} />
                  {link.name}
                </button>
              ))}
            </div>
            <p className="guide-text muted small">
              {guide.provider_label} may ask you to allow the extra access for each one.
            </p>
          </div>
        ) : null}
      </section>
    )
  }

  return (
    <section className="connect-sign-in">
      {connector.connected && !guide.signed_in ? (
        <p className="guide-text">
          This is connected with your own {guide.provider === 'google' ? 'Google Cloud app' : `${guide.provider_label} app or token`}. You can switch to signing in, which is
          simpler to keep working.
        </p>
      ) : null}
      {questionFields}
      {guide.reuse_account ? (
        <>
          <button
            type="button"
            className="btn connect-provider-btn"
            disabled={busy}
            onClick={() => onSignIn(true)}
          >
            <ProviderMark provider={guide.provider} />
            <span>Continue as {guide.reuse_account}</span>
          </button>
          <button type="button" className="btn btn-link btn-sm" disabled={busy} onClick={() => onSignIn(false)}>
            Use a different {guide.provider_label} account
          </button>
        </>
      ) : (
        <button type="button" className="btn connect-provider-btn" disabled={busy} onClick={() => onSignIn(false)}>
          <ProviderMark provider={guide.provider} />
          <span>{busy ? 'Opening…' : guide.button_label}</span>
        </button>
      )}
      {guide.access_summary ? <p className="guide-text connect-access">{guide.access_summary}</p> : null}
    </section>
  )
}

// ── Sign-in not switched on yet ────────────────────────────────────────────
function SetupPendingPanel({
  connector,
  onDeveloper,
  onNavigate,
}: {
  connector: ConnectorOut
  onDeveloper: () => void
  onNavigate: () => void
}) {
  const guide = connector.connect
  const { fromResult, fromError } = useToasts()
  const [asking, setAsking] = useState(false)
  const [asked, setAsked] = useState(false)

  const notify = async () => {
    setAsking(true)
    try {
      fromResult(await api.requestSignIn(connector.slug))
      setAsked(true)
    } catch (caught) {
      fromError(caught)
    } finally {
      setAsking(false)
    }
  }

  return (
    <section className="connect-sign-in connect-pending" aria-label={`Connect ${connector.name}`}>
      <button type="button" className="btn connect-provider-btn" disabled aria-describedby={`pending-${connector.slug}`}>
        <ProviderMark provider={guide.provider} />
        <span>{guide.button_label}</span>
      </button>
      <p id={`pending-${connector.slug}`} className="guide-text connect-access">
        {guide.setup_message}
      </p>
      {guide.can_request_setup ? (
        asked ? (
          <p className="connect-pending-done" role="status">
            <CheckIcon size={14} /> Your admins have been notified.
          </p>
        ) : (
          <button type="button" className="btn btn-secondary btn-sm" disabled={asking} onClick={() => void notify()}>
            {asking ? 'Notifying…' : 'Notify my admin'}
          </button>
        )
      ) : null}
      <AdminSetupNote guide={guide} onNavigate={onNavigate} />
      {guide.developer_label ? (
        <button type="button" className="btn btn-link btn-sm connect-developer-link" onClick={onDeveloper}>
          {guide.developer_label}
        </button>
      ) : null}
    </section>
  )
}

/** Admins only: who can switch a sign-in on (portal admins get the link to do it). */
function AdminSetupNote({ guide, onNavigate }: { guide: ConnectorOut['connect']; onNavigate: () => void }) {
  if (guide.admin_setup_url) {
    return (
      <div className="connect-admin-note connect-admin-setup">
        <span>As a platform administrator, you can switch this on for every workspace.</span>
        <Link className="btn btn-secondary btn-sm" to={guide.admin_setup_url} onClick={onNavigate}>
          {guide.admin_setup_label}
        </Link>
      </div>
    )
  }
  if (guide.admin_hint) return <div className="connect-admin-note">{guide.admin_hint}</div>
  return null
}

// ── Choose step ────────────────────────────────────────────────────────────
function ChoicePicker({
  connector,
  noun,
  providerLabel,
  account,
  onSignInAgain,
  onChosen,
}: {
  connector: ConnectorOut
  noun: string
  providerLabel: string
  account: string
  onSignInAgain: () => void
  onChosen: (record: ConnectorOut) => Promise<void>
}) {
  const [choices, setChoices] = useState<OAuthChoice[] | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState('')
  const [saving, setSaving] = useState(false)
  const [query, setQuery] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    api
      .oauthChoices(connector.slug, controller.signal)
      .then((list) => {
        setChoices(list)
        if (list.length === 1) setSelected(list[0]!.id)
      })
      .catch((caught) => {
        if (controller.signal.aborted) return
        setError(caught instanceof Error ? caught.message : 'Could not load the list. Try again.')
        setChoices([])
      })
    return () => controller.abort()
  }, [connector.slug])

  const choose = async () => {
    if (!selected) return
    setSaving(true)
    setError('')
    try {
      await onChosen(await api.oauthChoose(connector.slug, selected))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'That did not connect. Try another.')
    } finally {
      setSaving(false)
    }
  }

  const visible = (choices ?? []).filter((choice) =>
    `${choice.label} ${choice.detail}`.toLowerCase().includes(query.trim().toLowerCase()),
  )

  return (
    <section className="connect-picker" aria-label={`Choose a ${noun}`}>
      <div className="guide-title">Choose the {noun} to connect</div>
      {account ? <p className="guide-text muted small">Signed in as {account}</p> : null}

      {choices === null ? <p className="guide-text muted">Looking up what this account can reach…</p> : null}

      {error ? (
        <div className="banner is-danger" role="alert">
          {error}
        </div>
      ) : null}

      {choices !== null && choices.length === 0 && !error ? (
        <p className="guide-text">
          This {providerLabel} account cannot see any {noun}s. Sign in with the account that has access, or ask
          whoever manages it to add you.
        </p>
      ) : null}

      {choices && choices.length > 6 ? (
        <input
          className="input"
          type="search"
          placeholder={`Search ${noun}s`}
          aria-label={`Search ${noun}s`}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      ) : null}

      {visible.length ? (
        <div className="connect-choices" role="radiogroup" aria-label={`${noun}s`}>
          {visible.map((choice) => (
            <label key={choice.id} className={`connect-choice${selected === choice.id ? ' is-selected' : ''}`}>
              <input
                type="radio"
                name={`choice-${connector.slug}`}
                value={choice.id}
                checked={selected === choice.id}
                onChange={() => setSelected(choice.id)}
              />
              <span className="connect-choice-text">
                <span className="connect-choice-label">{choice.label}</span>
                {choice.detail ? <span className="connect-choice-detail">{choice.detail}</span> : null}
              </span>
            </label>
          ))}
        </div>
      ) : null}

      <div className="connect-row">
        <button
          type="button"
          className="btn btn-primary"
          disabled={!selected || saving}
          onClick={() => void choose()}
        >
          {saving ? 'Checking the connection…' : 'Connect'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" disabled={saving} onClick={onSignInAgain}>
          Use a different {providerLabel} account
        </button>
      </div>
    </section>
  )
}

// ── WordPress approval ─────────────────────────────────────────────────────
function WordPressPanel({
  connector,
  initialSite,
  onError,
}: {
  connector: ConnectorOut
  initialSite: string
  onError: (message: string) => void
}) {
  const [site, setSite] = useState(initialSite)
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const insecure = /^http:\/\//i.test(site.trim())

  const go = async () => {
    if (!site.trim()) {
      inputRef.current?.focus()
      return
    }
    setBusy(true)
    onError('')
    try {
      const result = await api.oauthStart(connector.slug, { return_path: currentReturnPath(), site_url: site })
      if (!isHttpsAuthorizeUrl(result.authorize_url)) {
        onError(SIGN_IN_LINK_PROBLEM)
        setBusy(false)
        return
      }
      window.location.assign(result.authorize_url)
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : 'Could not reach that site. Check the address.')
      setBusy(false)
    }
  }

  return (
    <section className="connect-sign-in">
      {connector.connected && connector.hints.siteUrl ? (
        <div className="connect-signed-in-line">
          <CheckIcon size={16} />
          <span>
            Connected to <strong>{connector.hints.siteUrl}</strong>
            {connector.hints.username ? <> as {connector.hints.username}</> : null}
          </span>
        </div>
      ) : null}
      <Field
        label="Your website address"
        htmlFor={`wp-site-${connector.slug}`}
        error={
          insecure
            ? 'WordPress only allows this on a secure address. Use https:// — most hosts turn it on for free.'
            : undefined
        }
      >
        <input
          ref={inputRef}
          id={`wp-site-${connector.slug}`}
          className={`input${insecure ? ' is-invalid' : ''}`}
          type="url"
          inputMode="url"
          placeholder="https://yourcompany.com"
          value={site}
          onChange={(event) => setSite(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void go()
          }}
        />
      </Field>
      <button type="button" className="btn btn-primary" disabled={busy || insecure} onClick={() => void go()}>
        {busy ? 'Opening your site…' : connector.connected ? 'Approve again on WordPress' : 'Continue to WordPress'}
      </button>
      <p className="guide-text connect-access">{connector.connect.access_summary}</p>
      <p className="guide-text muted small">
        Sign in to your site if asked and click “Yes, I approve of this connection”. You will come straight back
        here. There is nothing to copy or paste.
      </p>
    </section>
  )
}
