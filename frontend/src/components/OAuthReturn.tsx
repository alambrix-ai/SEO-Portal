/**
 * Picks up where a sign-in left off.
 *
 * The sign-in sends the browser to the vendor and back to whichever screen
 * it started on, with `?connected=<slug>`, `?oauth_choose=<slug>` or
 * `?oauth_error=…&oauth_slug=<slug>`. Mounted once in the app shell, so a
 * sign-in started from Integrations, an agent's setup or onboarding all
 * land back in the same connect drawer: showing that it worked, the
 * "choose your property" step, or what went wrong — then the parameters are
 * removed from the address bar.
 *
 * `oauth_error` is a fixed code (backend `OAUTH_ERROR_CODES` in
 * services/oauth_flow.py). Only our own sentence for that code is shown;
 * whatever else the address carries is never displayed, and an unknown code
 * gets the generic sentence. Slugs are checked before they are used.
 */
import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { api } from '@/api/client'
import type { ConnectorOut } from '@/api/types'
import { ConnectDialog, OAUTH_PARAMS, announceConnectorsChanged, type ConnectNotice } from '@/components/ConnectDialog'
import { useToasts } from '@/components/Toasts'
import { invalidateResourceCache } from '@/hooks/useResource'

const GENERIC_ERROR = 'Something went wrong while finishing the sign-in. Please try again.'

/** One plain sentence per code. Keep in step with the backend's OAUTH_ERROR_CODES. */
const OAUTH_ERROR_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  connect_failed: GENERIC_ERROR,
  link_invalid: 'That sign-in link is not recognised. Start again from the connect screen.',
  link_expired: 'That sign-in took too long or was already used. Start again from the connect screen.',
  cancelled: 'The sign-in was cancelled, so nothing was connected. You can try again whenever you are ready.',
  provider_refused: 'The other service did not accept the sign-in. Start again from the connect screen.',
  provider_unreachable: 'The other service could not be reached to finish signing in. Please try again in a few minutes.',
  not_set_up: 'Signing in to this service is not set up on this platform yet. Ask your administrator.',
  permissions_missing: 'Some permissions were left unticked on the sign-in screen. Sign in again and tick every box.',
  no_lasting_access:
    'The sign-in did not give lasting access, so the connection would soon stop working. Start again and allow access.',
  session_ended: 'Your session for this workspace has ended. Sign in again, then reconnect.',
  role_changed: 'Your role can no longer manage integrations in this workspace. Ask your administrator.',
  site_not_secure:
    'Your website reported a non-secure (http://) address. Switch the site to https:// and try again.',
  site_mismatch:
    'The approval came back from a different website than the one you entered. Start again with the right address.',
})

/** The sentence for a code from the address. Never the address text itself. */
function oauthErrorMessage(code: string): string {
  const known = Object.prototype.hasOwnProperty.call(OAUTH_ERROR_MESSAGES, code) ? OAUTH_ERROR_MESSAGES[code] : undefined
  return known ?? GENERIC_ERROR
}

/** Connector slugs are lower-case letters, digits and underscores. */
const SLUG_PATTERN = /^[a-z0-9_]{1,64}$/

function validSlug(raw: string | null): string | null {
  return raw && SLUG_PATTERN.test(raw) ? raw : null
}

export function OAuthReturn() {
  const location = useLocation()
  const navigate = useNavigate()
  const { push } = useToasts()
  const [open, setOpen] = useState<{ connector: ConnectorOut; notice: ConnectNotice } | null>(null)

  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const rawConnected = params.get('connected')
    const rawChoose = params.get('oauth_choose')
    const rawError = params.get('oauth_error')
    if (rawConnected === null && rawChoose === null && rawError === null) return
    const connected = validSlug(rawConnected)
    const choose = validSlug(rawChoose)
    const error = rawError !== null ? oauthErrorMessage(rawError) : null
    const slug = connected || choose || validSlug(params.get('oauth_slug'))

    // Take our parameters off the address, so a reload does not replay them.
    for (const key of OAUTH_PARAMS) params.delete(key)
    const rest = params.toString()
    navigate({ pathname: location.pathname, search: rest ? `?${rest}` : '' }, { replace: true })

    const notice: ConnectNotice = connected
      ? { kind: 'connected' }
      : choose
        ? { kind: 'choose' }
        : { kind: 'error', message: error ?? GENERIC_ERROR }

    invalidateResourceCache('connectors')
    announceConnectorsChanged()
    if (!slug) {
      if (error) push(error, 'error')
      return
    }
    api
      .connector(slug)
      .then((connector) => setOpen({ connector, notice }))
      .catch(() => {
        if (error) push(error, 'error')
        else if (connected) push('Connected', 'success')
      })
  }, [location.pathname, location.search, navigate, push])

  if (!open) return null
  return (
    <ConnectDialog
      key={`${open.connector.slug}-${open.notice.kind}`}
      connector={open.connector}
      notice={open.notice}
      onClose={() => setOpen(null)}
      onConnected={async () => {
        invalidateResourceCache('connectors')
        announceConnectorsChanged()
        setOpen(null)
      }}
    />
  )
}
