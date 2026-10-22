"""Sign-in for the business apps (phase 2B): what each vendor does differently.

The shared flow (:mod:`app.services.oauth_flow`) handles state, PKCE, the
callback and the choose step. A provider whose ``extension`` names one of
the vendors below routes three steps through here instead:

* **prepare** — check what the person typed before leaving (a Shopify store
  address, the Salesforce sandbox switch). The result is kept server-side
  in the sign-in's state row, so the callback uses exactly what was checked
  rather than anything the vendor or the browser sends back.
* **authorize_url** — the vendor's own sign-in or install address (a
  Shopify store's own host, GitHub's app installation page).
* **verify_callback** and **exchange** — Shopify's signed callback, and the
  code exchange with what each vendor returns beside the token (Slack's
  workspace, HubSpot's account, Salesforce's instance address, Mailchimp's
  data centre, the GitHub App installation).

Non-secret facts learned here travel in :attr:`TokenSet.extra` and are kept
as readable hints on the connection; tokens are encrypted as always.

Every failure carries one of :data:`app.services.oauth_flow.OAUTH_ERROR_CODES`
(the console turns it into a sentence): ``link_invalid`` for an address or a
Shopify signature that does not check out, ``site_mismatch`` for another
store, ``provider_unreachable`` when the vendor cannot be reached and
``provider_refused`` when it answers no.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import urlencode, urlsplit

import httpx

from app.connectors.base import oauth_providers
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.oauth_providers import OAuthProvider
from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.core.logging import get_logger

log = get_logger(__name__)

#: A shop's permanent Shopify address. Anchored at both ends, lower case, no
#: port, path or user info: the token request goes to this host.
SHOP_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9\-]{0,59}\.myshopify\.com$")
SALESFORCE_LOGIN = "login.salesforce.com"
SALESFORCE_SANDBOX_LOGIN = "test.salesforce.com"
#: Hosts a Salesforce token response may name as the org's instance. The
#: access token is sent there, so nothing else is accepted.
_SALESFORCE_INSTANCE_SUFFIXES = (
    ".salesforce.com",
    ".force.com",
    ".cloudforce.com",
    ".salesforce.mil",
)
#: Stored as the connection's "access token" for a GitHub App installation:
#: the connector mints short-lived installation tokens from the platform's
#: app key instead, so the person's own GitHub token is not kept at all.
GITHUB_INSTALLATION_MARKER = "github-app-installation"


def _flow_error(message: str, code: str) -> Exception:
    """A sign-in failure with its code: one of the ``oauth_flow`` code names
    ``LINK_INVALID``, ``SITE_MISMATCH``, ``PROVIDER_UNREACHABLE`` or
    ``PROVIDER_REFUSED`` (looked up there, so the values stay in one place)."""
    from app.services import oauth_flow

    return oauth_flow.OAuthFlowError(message, code=getattr(oauth_flow, code))


# ── Inputs typed before leaving ────────────────────────────────────────────
def normalise_shop(raw: str) -> str:
    """``mystore.myshopify.com`` from what a merchant is likely to type.

    Accepted: the .myshopify.com address (with or without https:// or a
    trailing slash), the bare store name, or the admin address
    ``admin.shopify.com/store/<name>``. Anything else — a custom domain, a
    port, a path, another host — is refused, because this host receives the
    platform's client secret in the token exchange.
    """
    text = (raw or "").strip().lower()
    if not text:
        raise InvalidInputError("Enter your Shopify store address, like mystore.myshopify.com")
    if len(text) > 200:
        raise InvalidInputError("That store address is too long.")
    text = text.removeprefix("https://").removeprefix("http://").rstrip("/")
    admin = re.fullmatch(r"admin\.shopify\.com/store/([a-z0-9][a-z0-9\-]{0,59})", text)
    if admin:
        text = f"{admin.group(1)}.myshopify.com"
    elif re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,59}", text):
        text = f"{text}.myshopify.com"
    if not SHOP_DOMAIN.fullmatch(text):
        raise InvalidInputError(
            "Enter the store's .myshopify.com address, like mystore.myshopify.com. "
            "Shopify shows it under Settings → Domains. Your own web address "
            "(like www.mystore.com) will not work here."
        )
    return text


def prepare(provider: OAuthProvider, inputs: dict[str, str]) -> dict[str, str]:
    """The checked values a sign-in needs, from what was typed."""
    inputs = inputs or {}
    if provider.extension == "shopify":
        return {"shop": normalise_shop(inputs.get("shop", ""))}
    if provider.extension == "salesforce":
        sandbox = str(inputs.get("sandbox", "")).strip().lower() in ("1", "true", "yes", "on")
        return {"login_host": SALESFORCE_SANDBOX_LOGIN if sandbox else SALESFORCE_LOGIN}
    return {}


def _host(template: str, vars_: dict[str, str]) -> str:
    """Fill ``{shop}`` / ``{login_host}`` from values :func:`prepare` checked."""
    try:
        url = template.format(**vars_)
    except (KeyError, IndexError):
        raise _flow_error("That sign-in link is not recognised. Start again.", "LINK_INVALID") from None
    if "{" in url or not url.startswith("https://"):
        raise _flow_error("That sign-in link is not recognised. Start again.", "LINK_INVALID")
    return url


def token_url(provider: OAuthProvider, vars_: dict[str, str]) -> str:
    return _host(provider.token_url, vars_)


# ── Authorize ──────────────────────────────────────────────────────────────
def authorize_url(
    provider: OAuthProvider,
    spec: ConnectorSpec,
    *,
    state: str,
    code_challenge: str,
    vars_: dict[str, str],
) -> str:
    if provider.extension == "github":
        slug = oauth_providers.platform_setting("github_app_slug")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-]{0,99}", slug):
            raise InvalidInputError("Install on GitHub is not set up on this platform yet.")
        # The installation page itself. With "Request user authorization
        # (OAuth) during installation" switched on for the app, GitHub
        # returns to the callback with a code, the installation id and this
        # state.
        return f"https://github.com/apps/{slug}/installations/new?{urlencode({'state': state})}"

    scopes = oauth_providers.scopes_for(spec)
    params: dict[str, str] = {"client_id": provider.client_id, "redirect_uri": provider.redirect_uri}
    if provider.extension != "shopify":
        params["response_type"] = "code"
    # Mailchimp has no scopes; Bitbucket takes them from the consumer's own
    # settings. Neither gets an empty or ignored scope parameter.
    if scopes and provider.extension not in ("mailchimp", "bitbucket"):
        params["scope"] = provider.scope_separator.join(scopes)
    params["state"] = state
    params.update(provider.extra_params)
    if provider.pkce and code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{_host(provider.authorize_url, vars_)}?{urlencode(params)}"


# ── Callback verification ──────────────────────────────────────────────────
def shopify_hmac_message(items: list[tuple[str, str]]) -> str:
    """Shopify's signed message: every parameter except ``hmac`` (and the
    legacy ``signature``), sorted, as ``key=value`` joined with ``&``."""
    pairs = sorted((k, v) for k, v in items if k not in ("hmac", "signature"))
    return "&".join(f"{k}={v}" for k, v in pairs)


def shopify_hmac_valid(items: list[tuple[str, str]], secret: str) -> bool:
    """Constant-time check of the ``hmac`` Shopify adds to its redirect."""
    given = next((v for k, v in items if k == "hmac"), "")
    if not given or not secret:
        return False
    if sum(1 for k, _ in items if k == "hmac") != 1:
        return False
    key = secret.encode()
    expected = hmac.new(key, shopify_hmac_message(items).encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, given.lower()):
        return True
    # Shopify's own libraries build the message with URLSearchParams, which
    # percent-encodes characters such as "=" in the base64 ``host`` value.
    # Either form is a valid HMAC under the same secret.
    encoded = urlencode(sorted((k, v) for k, v in items if k not in ("hmac", "signature")))
    alternative = hmac.new(key, encoded.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(alternative, given.lower())


def verify_callback(
    provider: OAuthProvider, items: list[tuple[str, str]], vars_: dict[str, str]
) -> None:
    """Refuse a callback the vendor did not sign, or for another store."""
    if provider.extension != "shopify":
        return
    if not shopify_hmac_valid(items, provider.client_secret):
        log.warning("shopify: callback signature did not verify")
        raise _flow_error(
            "Shopify's reply could not be verified, so nothing was connected. Start again.",
            "LINK_INVALID",
        )
    shop = next((v for k, v in items if k == "shop"), "").strip().lower()
    if not SHOP_DOMAIN.fullmatch(shop) or shop != vars_.get("shop"):
        raise _flow_error(
            "Shopify sent the approval back for a different store than the one you "
            "entered. Start again with the store you approved.",
            "SITE_MISMATCH",
        )


# ── Code exchange ──────────────────────────────────────────────────────────
def _client() -> httpx.Client:
    return httpx.Client(timeout=settings.connector_timeout_seconds)


def _post_token(provider: OAuthProvider, url: str, payload: dict, headers: dict) -> dict:
    try:
        with _client() as client:
            response = client.post(url, data=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("%s token exchange unreachable: %s", provider.name, type(exc).__name__)
        raise _flow_error(
            f"{provider.label} could not be reached to finish signing in. Try again.",
            "PROVIDER_UNREACHABLE",
        ) from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # Slack answers HTTP 200 with ok:false on a refused exchange.
    refused = response.status_code >= 400 or not data.get("access_token") or data.get("ok") is False
    if refused:
        log.warning(
            "%s token exchange refused (%s): %s",
            provider.name,
            response.status_code,
            str(data.get("error") or "no access_token")[:80],
        )
        raise _flow_error(
            f"{provider.label} did not accept the sign-in. Start again from the connect screen.",
            "PROVIDER_REFUSED",
        )
    return data


def _granted_scopes(data: dict) -> tuple[str, ...]:
    raw = data.get("scope", data.get("scopes"))
    if isinstance(raw, list):
        return tuple(str(s) for s in raw if s)
    return tuple(s for s in re.split(r"[\s,]+", str(raw or "")) if s)


def exchange(
    provider: OAuthProvider,
    *,
    code: str,
    verifier: str,
    vars_: dict[str, str],
    params: dict[str, str],
    now: float | None = None,
):  # noqa: ANN201 - TokenSet, imported lazily to avoid a cycle
    """Trade the code for tokens, plus what each vendor returns beside them."""
    from app.services.oauth_flow import TokenSet, basic_auth

    issued = now or time.time()
    name = provider.extension
    url = token_url(provider, vars_)
    headers = {"Accept": "application/json"}
    if name == "shopify":
        # Exactly the documented fields. expiring=1 asks for an expiring
        # offline token with a refresh token, which Shopify requires of new
        # public apps.
        payload = {
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "code": code,
            "expiring": "1",
        }
    elif name == "github":
        payload = {
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "code": code,
            "redirect_uri": provider.redirect_uri,
        }
    else:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": provider.redirect_uri,
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
        }
        if provider.token_auth == "basic":
            payload.pop("client_id")
            payload.pop("client_secret")
            headers["Authorization"] = basic_auth(provider.client_id, provider.client_secret)
        if provider.pkce and verifier:
            payload["code_verifier"] = verifier

    data = _post_token(provider, url, payload, headers)
    refresh = str(data.get("refresh_token") or "")
    expires_in = data.get("expires_in")
    if expires_in:
        expires_at = issued + float(expires_in)
    elif refresh:
        # Salesforce states no lifetime; its shortest session timeout is
        # fifteen minutes, so the stored token is not trusted beyond that.
        expires_at = issued + (900 if name == "salesforce" else 3600)
    else:
        expires_at = 0.0
    tokens = TokenSet(
        access_token=str(data["access_token"]),
        refresh_token=refresh,
        expires_at=expires_at,
        scopes=() if name == "bitbucket" else _granted_scopes(data),
    )
    _after_exchange(provider, tokens, data, vars_=vars_, params=params)
    return tokens


def _after_exchange(provider: OAuthProvider, tokens, data: dict, *, vars_: dict, params: dict) -> None:  # noqa: ANN001
    name = provider.extension
    if name == "slack":
        team = data.get("team") or {}
        tokens.account = str(team.get("name") or "")
        if team.get("id"):
            tokens.extra["slackTeamId"] = str(team["id"])
        if str(data.get("token_type") or "bot") != "bot":
            raise _flow_error("Slack did not add the app to the workspace. Start again.", "PROVIDER_REFUSED")
    elif name == "hubspot":
        hub = data.get("hub_id") or data.get("hubId")
        if hub:
            tokens.extra["hubId"] = str(hub)
            tokens.account = f"HubSpot account {hub}"
    elif name == "salesforce":
        instance = str(data.get("instance_url") or "").rstrip("/")
        parts = urlsplit(instance)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or parts.port is not None
            or parts.path not in ("", "/")
            or not host.endswith(_SALESFORCE_INSTANCE_SUFFIXES)
        ):
            raise _flow_error(
                "Salesforce did not say which org to connect to. Start again.", "PROVIDER_REFUSED"
            )
        tokens.extra["instanceUrl"] = f"https://{host}"
        tokens.extra["loginUrl"] = f"https://{vars_.get('login_host') or SALESFORCE_LOGIN}"
    elif name == "shopify":
        tokens.extra["storeDomain"] = vars_["shop"]
        tokens.account = vars_["shop"]
    elif name == "mailchimp":
        centre, account = mailchimp_metadata(provider, tokens.access_token)
        tokens.extra["dataCentre"] = centre
        tokens.account = account
    elif name == "github":
        installation = str(params.get("installation_id") or "").strip()
        if not installation.isdigit():
            raise _flow_error(
                "GitHub did not say where the app was installed. Install it again from "
                "the connect screen.",
                "PROVIDER_REFUSED",
            )
        account = github_verify_installation(tokens.access_token, installation)
        tokens.extra["installationId"] = installation
        tokens.account = account
        # The person's own GitHub token is not kept: everything afterwards
        # uses installation tokens minted from the platform's app key.
        tokens.access_token = GITHUB_INSTALLATION_MARKER
        tokens.refresh_token = ""
        tokens.expires_at = 0.0
        tokens.scopes = ()


def mailchimp_metadata(provider: OAuthProvider, access_token: str) -> tuple[str, str]:
    """``(data centre, account name)`` for a Mailchimp OAuth token."""
    try:
        with _client() as client:
            response = client.get(
                "https://login.mailchimp.com/oauth2/metadata",
                headers={"Authorization": f"OAuth {access_token}", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        log.warning("mailchimp metadata unreachable: %s", type(exc).__name__)
        raise _flow_error(
            "Mailchimp did not say which server your account is on. Try again.", "PROVIDER_UNREACHABLE"
        ) from None
    try:
        data = response.json() if response.status_code < 400 else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    centre = str(data.get("dc") or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2,5}[0-9]{1,3}", centre):
        raise _flow_error(
            "Mailchimp did not say which server your account is on. Try again.", "PROVIDER_REFUSED"
        )
    return centre, str(data.get("accountname") or (data.get("login") or {}).get("email") or "")


def github_verify_installation(user_token: str, installation_id: str) -> str:
    """The account the installation belongs to, after checking that the
    person who signed in can actually reach it.

    GitHub warns that the installation id on the redirect can be forged;
    ``GET /user/installations`` with the person's own token lists only the
    installations they have access to.
    """
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with _client() as client:
            for page in range(1, 11):
                response = client.get(
                    "https://api.github.com/user/installations",
                    params={"per_page": 100, "page": page},
                    headers=headers,
                )
                if response.status_code >= 400:
                    break
                rows = (response.json() or {}).get("installations") or []
                for row in rows:
                    if str(row.get("id")) == installation_id:
                        return str((row.get("account") or {}).get("login") or "")
                if len(rows) < 100:
                    break
    except (httpx.HTTPError, ValueError):
        raise _flow_error(
            "GitHub could not be reached to finish the install. Try again.", "PROVIDER_UNREACHABLE"
        ) from None
    raise _flow_error(
        "That GitHub installation does not belong to the account that signed in. "
        "Install the app again from the connect screen.",
        "LINK_INVALID",
    )


__all__ = [
    "GITHUB_INSTALLATION_MARKER",
    "SHOP_DOMAIN",
    "authorize_url",
    "exchange",
    "normalise_shop",
    "prepare",
    "shopify_hmac_valid",
    "verify_callback",
]
