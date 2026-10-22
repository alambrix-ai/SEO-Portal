"""Vendor-specific steps of the sign-in flow (phase 2A: social and ads).

Most vendors follow standard OAuth 2.0 and need nothing here; the knobs on
:class:`~app.connectors.base.oauth_providers.OAuthProvider` (scope
separator, Basic client auth, refresh rules) cover the small differences.
A provider whose ``flavor`` names an entry below departs further:

* **meta** — the code is exchanged with a GET, the short-lived user token is
  swapped for a long-lived one (about 60 days, no refresh token), and the
  permissions actually granted are read back from ``/me/permissions``,
  because Facebook lets a person untick some of them.
* **tiktok** — TikTok for Business has its own authorize page (``app_id``,
  no scope: permissions are set on the developer app), returns
  ``auth_code``, and exchanges it with a JSON call wrapped in
  ``{code, message, data}``. The long-term token does not expire.
* **x**, **pinterest** — standard, plus a call to name the signed-in account.
* **reddit** — standard (Basic client auth, ``duration=permanent``), except
  that Reddit refuses requests without a descriptive User-Agent, so the code
  exchange and the account lookup send ``settings.reddit_user_agent``. The
  whole flow is refused while Reddit is switched off (``reddit_enabled``) or
  the User-Agent is unset.

As in :mod:`app.services.oauth_flow`, tokens and response bodies are never
logged; failures become one plain sentence and one of its
:data:`~app.services.oauth_flow.OAUTH_ERROR_CODES` (``provider_refused``,
``provider_unreachable``, or ``not_set_up`` while Reddit is off).
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

import httpx

from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.oauth_providers import OAuthProvider
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: Meta does not always state the long-lived lifetime; this is its documented
#: "about 60 days".
META_LONG_LIVED_SECONDS = 60 * 24 * 3600


def _client() -> httpx.Client:
    return httpx.Client(timeout=settings.connector_timeout_seconds)


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _failed(provider: OAuthProvider):  # noqa: ANN202
    from app.services.oauth_flow import PROVIDER_REFUSED, OAuthFlowError

    return OAuthFlowError(
        f"{provider.label} did not accept the sign-in. Start again from the connect screen.",
        code=PROVIDER_REFUSED,
    )


def _unreachable(provider: OAuthProvider):  # noqa: ANN202
    from app.services.oauth_flow import PROVIDER_UNREACHABLE, OAuthFlowError

    return OAuthFlowError(
        f"{provider.label} could not be reached to finish signing in. Try again.",
        code=PROVIDER_UNREACHABLE,
    )


# ── Authorize URL ──────────────────────────────────────────────────────────
def authorize_url(
    provider: OAuthProvider, spec: ConnectorSpec, *, state: str, code_challenge: str, login_hint: str
) -> str | None:
    """The vendor's own authorize URL, or None for the standard one."""
    if provider.flavor == "reddit":
        # The standard URL fits Reddit; this only refuses to send anyone
        # there while Reddit is switched off or cannot be called.
        _reddit_user_agent(provider)
        return None
    if provider.flavor == "tiktok":
        # TikTok for Business: the advertiser authorization page. The
        # permissions are those ticked on the developer app.
        params = {"app_id": provider.client_id, "state": state, "redirect_uri": provider.redirect_uri}
        return f"{provider.authorize_url}?{urlencode(params)}"
    return None


# ── Code exchange ──────────────────────────────────────────────────────────
def exchange(provider: OAuthProvider, *, code: str, verifier: str, now: float):  # noqa: ANN201
    """A :class:`~app.services.oauth_flow.TokenSet`, or None for the standard exchange."""
    if provider.flavor == "meta":
        return _meta_exchange(provider, code=code, now=now)
    if provider.flavor == "tiktok":
        return _tiktok_exchange(provider, code=code)
    if provider.flavor == "reddit":
        return _reddit_exchange(provider, code=code, now=now)
    return None


def graph_root(provider: OAuthProvider) -> str:
    """``https://graph.facebook.com/vNN.N`` from the provider's token URL."""
    return provider.token_url.rsplit("/oauth/", 1)[0]


def _meta_exchange(provider: OAuthProvider, *, code: str, now: float):  # noqa: ANN202
    from app.services.oauth_flow import TokenSet

    try:
        with _client() as client:
            short = client.get(
                provider.token_url,
                params={
                    "client_id": provider.client_id,
                    "client_secret": provider.client_secret,
                    "redirect_uri": provider.redirect_uri,
                    "code": code,
                },
            )
            short_data = _json(short)
            if short.status_code >= 400 or not short_data.get("access_token"):
                log.warning("meta code exchange refused (%s)", short.status_code)
                raise _failed(provider)
            # Short-lived (about an hour) → long-lived (about 60 days).
            long = client.get(
                provider.token_url,
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": provider.client_id,
                    "client_secret": provider.client_secret,
                    "fb_exchange_token": short_data["access_token"],
                },
            )
            long_data = _json(long)
            if long.status_code >= 400 or not long_data.get("access_token"):
                log.warning("meta long-lived exchange refused (%s)", long.status_code)
                raise _failed(provider)
            token = str(long_data["access_token"])
            granted = _meta_granted(client, provider, token)
    except httpx.HTTPError as exc:
        log.warning("meta token exchange unreachable: %s", type(exc).__name__)
        raise _unreachable(provider) from None

    lifetime = float(long_data.get("expires_in") or META_LONG_LIVED_SECONDS)
    return TokenSet(access_token=token, expires_at=now + lifetime, scopes=granted)


def _meta_granted(client: httpx.Client, provider: OAuthProvider, token: str) -> tuple[str, ...]:
    """Permissions the person actually granted (they can untick some)."""
    response = client.get(
        f"{graph_root(provider)}/me/permissions", headers={"Authorization": "Bearer " + token}
    )
    rows = _json(response).get("data") or [] if response.status_code < 400 else []
    return tuple(
        str(row.get("permission"))
        for row in rows
        if isinstance(row, dict) and row.get("status") == "granted" and row.get("permission")
    )


def _tiktok_exchange(provider: OAuthProvider, *, code: str):  # noqa: ANN202
    from app.services.oauth_flow import TokenSet

    try:
        with _client() as client:
            response = client.post(
                provider.token_url,
                json={"app_id": provider.client_id, "secret": provider.client_secret, "auth_code": code},
            )
    except httpx.HTTPError as exc:
        log.warning("tiktok token exchange unreachable: %s", type(exc).__name__)
        raise _unreachable(provider) from None
    body = _json(response)
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if response.status_code >= 400 or body.get("code") not in (0, None) or not data.get("access_token"):
        log.warning("tiktok token exchange refused (%s, code %s)", response.status_code, body.get("code"))
        raise _failed(provider)
    # The long-term token has no expiry; TikTok's scope list is numeric
    # permission ids, so it is not compared with the connector's own.
    return TokenSet(access_token=str(data["access_token"]))


def _reddit_user_agent(provider: OAuthProvider) -> str:
    """The User-Agent Reddit requires, or a plain refusal to go on."""
    from app.connectors.reddit.connector import user_agent_or_none
    from app.services.oauth_flow import NOT_SET_UP, OAuthFlowError

    if not provider.enabled:
        raise OAuthFlowError(
            "Reddit is switched off on this platform. Reddit has to approve business use first; "
            "ask your platform administrator.",
            code=NOT_SET_UP,
        )
    agent = user_agent_or_none()
    if not agent:
        raise OAuthFlowError(
            "Reddit is not fully set up yet: the platform administrator still needs to enter "
            "the User-Agent under Sign-in providers → Reddit.",
            code=NOT_SET_UP,
        )
    return agent


def _reddit_exchange(provider: OAuthProvider, *, code: str, now: float):  # noqa: ANN202
    """Reddit's code exchange: Basic client auth and its required User-Agent.

    Reddit can answer a refused exchange with 200 and ``{"error": ...}``, so
    success is the presence of an access token, not the status alone.
    """
    from app.services.oauth_flow import TokenSet, basic_auth

    agent = _reddit_user_agent(provider)
    try:
        with _client() as client:
            response = client.post(
                provider.token_url,
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": provider.redirect_uri},
                headers={
                    "Accept": "application/json",
                    "Authorization": basic_auth(provider.client_id, provider.client_secret),
                    "User-Agent": agent,
                },
            )
    except httpx.HTTPError as exc:
        log.warning("reddit token exchange unreachable: %s", type(exc).__name__)
        raise _unreachable(provider) from None
    data = _json(response)
    if response.status_code >= 400 or not data.get("access_token"):
        # The error code only, bounded; never the body or a token.
        log.warning(
            "reddit token exchange refused (%s): %s",
            response.status_code,
            str(data.get("error") or "no access_token")[:80],
        )
        raise _failed(provider)
    lifetime = data.get("expires_in")
    try:
        expires_at = now + float(lifetime) if lifetime else now + 3600
    except (TypeError, ValueError):
        expires_at = now + 3600
    return TokenSet(
        access_token=str(data["access_token"]),
        refresh_token=str(data.get("refresh_token") or ""),
        expires_at=expires_at,
        scopes=tuple(s for s in re.split(r"[\s,]+", str(data.get("scope") or "")) if s),
    )


# ── Who signed in ──────────────────────────────────────────────────────────
def identify(provider: OAuthProvider, access_token: str) -> str:
    """A label for the signed-in account ("Jane Doe", "@brand"). Best effort."""
    url, pick = "", None
    headers = {"Authorization": "Bearer " + access_token}
    if provider.flavor == "meta":
        url = f"{graph_root(provider)}/me?fields=name"
        pick = lambda d: str(d.get("name") or "")  # noqa: E731
    elif provider.flavor == "x":
        url = "https://api.x.com/2/users/me"
        pick = lambda d: f"@{(d.get('data') or {}).get('username')}" if (d.get("data") or {}).get("username") else ""  # noqa: E731
    elif provider.flavor == "pinterest":
        url = "https://api.pinterest.com/v5/user_account"
        pick = lambda d: str(d.get("username") or "")  # noqa: E731
    elif provider.flavor == "reddit":
        from app.connectors.reddit.connector import API_HOST, user_agent_or_none

        agent = user_agent_or_none()
        if not agent:
            return ""
        url = f"{API_HOST}/api/v1/me"
        headers["User-Agent"] = agent
        pick = lambda d: f"u/{d.get('name')}" if d.get("name") else ""  # noqa: E731
    if not url or pick is None:
        return ""
    try:
        with _client() as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400:
        return ""
    return pick(_json(response))[:200]


