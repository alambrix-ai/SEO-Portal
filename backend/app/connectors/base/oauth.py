"""Obtaining the access token these connectors have always required.

Ten connectors rendered a button reading "Authorize with Google" (or Meta, or
Slack). It was wired to ``onClick={() => setOauthDone(true)}`` — it set a
boolean in the browser and relabelled itself "Authorised". No redirect, no
code exchange, no token. The connector was then saved as connected, and
failed on its first real call, because all ten do

    self.credentials.require("accessToken")

and ``accessToken`` was declared by no connector's field list, so nothing
could ever have supplied it. The button did not fail to authorise; it
asserted that authorisation had happened.

What replaces it is the credential the vendor actually issues, entered like
any other. That is not a lesser thing than a redirect flow: for a
single-workspace integration it is the *documented* path — a Slack app's Bot
User OAuth Token, a Meta system-user token, a LinkedIn access token. It is
also the only path that works without the operator registering an OAuth
client, which a redirect flow cannot do without.

The complication is expiry. A Google access token lives an hour, so pasting
one is useless by lunchtime. Google, Microsoft and Salesforce issue a
long-lived **refresh** token instead, which is exchanged for a short-lived
access token on demand — so where a connector names a token endpoint, this
does that exchange and caches the result until shortly before it expires.

The redirect flow now exists as well — "Sign in with Google" — in
:mod:`app.services.oauth_flow`, using the platform's own registered app
(:mod:`app.connectors.base.oauth_providers`). It deposits its tokens in the
same encrypted fields, marked ``oauthSource=platform``; this mixin then
refreshes with the platform's client id and secret from settings instead of
asking the workspace for them. The paste-the-refresh-token path stays, under
Advanced, for workspaces connected that way and for deployments without a
platform app.
"""
from __future__ import annotations

import base64
import json
import re
import time

import httpx

from app.connectors.base.credentials import (
    ACCESS_TOKEN_EXPIRY_KEY,
    from_platform_sign_in,
    secret,
    text,
)
from app.core import egress
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger, log_failure
from app.core.user_messages import bounded_redacted

log = get_logger(__name__)

#: Refreshed this many seconds before the vendor's stated expiry, so a call
#: cannot start with a valid token and finish with an expired one.
_EARLY = 120
#: Largest token reply read from a customer-typed sign-in address.
_TOKEN_MAX_BYTES = 100_000


def token_fields(*, refreshable: bool, vendor: str, docs: str = "") -> tuple:
    """The credential fields an OAuth-based connector needs.

    ``refreshable`` connectors get the client id, client secret and refresh
    token that let this module mint access tokens indefinitely. The others
    take the long-lived token the vendor issues directly.
    """
    where = f" {docs}" if docs else ""
    if not refreshable:
        return (
            secret(
                "accessToken",
                "Access token",
                "",
                help_text=(
                    f"The long-lived token {vendor} issues for a workspace "
                    f"integration.{where}"
                ),
            ),
        )
    return (
        secret(
            "refreshToken",
            "Refresh token",
            "",
            help_text=(
                f"{vendor} access tokens expire within the hour, so the "
                f"refresh token is what is stored — a new access token is "
                f"obtained from it for each call.{where}"
            ),
        ),
        text(
            "clientId",
            "OAuth client ID",
            "",
            help_text=f"From the {vendor} app you created the refresh token with.",
        ),
        secret("clientSecret", "OAuth client secret", ""),
        secret(
            "accessToken",
            "Access token",
            "",
            required=False,
            help_text=(
                "Optional. Only used if no refresh token is set, and it will "
                "stop working when the token expires."
            ),
        ),
    )


class OAuthTokenMixin:
    """Supplies ``access_token`` to a connector that authenticates with one.

    Mix in *before* the connector base so the attribute resolves, and set
    ``token_url`` on the class for a vendor that supports refresh.
    """

    #: The vendor's token endpoint. Empty means "this vendor issues a
    #: long-lived token and there is nothing to refresh".
    token_url: str = ""
    #: Extra form fields some vendors require on the refresh call.
    token_extra: dict[str, str] = {}
    #: How the customer's own app authenticates on refresh (``body`` or
    #: ``basic``). A platform sign-in follows its provider's setting.
    token_auth: str = "body"

    _token: str = ""
    _token_expires: float = 0.0

    @property
    def access_token(self) -> str:
        """A usable bearer token, minted from the refresh token if needed."""
        refresh = self.credentials.get("refreshToken")  # type: ignore[attr-defined]
        if refresh and self.token_url:
            now = time.time()
            if self._token and now < self._token_expires:
                return self._token
            stored = self._stored_token(now)
            if stored:
                return stored
            self._token, self._token_expires = self._exchange(refresh, now)
            return self._token

        pasted = self.credentials.get("accessToken")  # type: ignore[attr-defined]
        if pasted:
            self._check_sign_in_current()
            return pasted
        # Named so the message says which field to fill in, not "accessToken".
        raise ConnectorConfigError(
            f"{self.name} has no access token. Enter the token the vendor "  # type: ignore[attr-defined]
            "issued, or a refresh token with its client id and secret."
        )

    def _check_sign_in_current(self) -> None:
        """Refuse a signed-in token that has run out and cannot renew itself.

        Meta user tokens last about 60 days, LinkedIn's 60 days without a
        refresh token. Saying so beats the vendor's "invalid token".
        """
        values = self.credentials.values  # type: ignore[attr-defined]
        if not from_platform_sign_in(values):
            return
        raw = values.get(ACCESS_TOKEN_EXPIRY_KEY)
        try:
            expiry = float(raw) if raw else 0.0
        except ValueError:
            expiry = 0.0
        if expiry and expiry <= time.time():
            raise ConnectorConfigError(sign_in_expired_message(self))

    def _stored_token(self, now: float) -> str:
        """The access token saved by the sign-in, while it is still fresh.

        Only a token stored with its expiry counts: a pasted one has no
        known lifetime, and the refresh token is the better source.
        """
        token = self.credentials.get("accessToken")  # type: ignore[attr-defined]
        raw_expiry = self.credentials.get(ACCESS_TOKEN_EXPIRY_KEY)  # type: ignore[attr-defined]
        try:
            expiry = float(raw_expiry) if raw_expiry else 0.0
        except ValueError:
            expiry = 0.0
        if token and expiry - _EARLY > now:
            self._token, self._token_expires = token, expiry - _EARLY
            return token
        return ""

    def _provider(self):  # noqa: ANN202
        from app.connectors.base import oauth_providers

        return oauth_providers.provider_for(getattr(self, "spec", None))

    def _client_credentials(self) -> tuple[str, str]:
        """The OAuth client the refresh token was issued to.

        A connection made through the platform's sign-in was issued to the
        platform's own app, whose id and secret come from settings and are
        never stored per workspace. A pasted refresh token belongs to the
        customer's app, whose id and secret were pasted with it.
        """
        values = self.credentials.values  # type: ignore[attr-defined]
        if from_platform_sign_in(values):
            from app.connectors.base import oauth_providers

            provider = oauth_providers.provider_for(getattr(self, "spec", None))
            if provider is None or not provider.configured:
                raise ConnectorConfigError(
                    f"{self.name} was connected by signing in, but the "  # type: ignore[attr-defined]
                    "platform's sign-in app is no longer set up. Ask your "
                    "platform administrator, or reconnect under Advanced."
                )
            return provider.client_id, provider.client_secret
        return (
            self.credentials.get("clientId"),  # type: ignore[attr-defined]
            self.credentials.get("clientSecret"),  # type: ignore[attr-defined]
        )

    def _exchange(self, refresh: str, now: float) -> tuple[str, float]:
        """Swap the refresh token for an access token.

        When the vendor sends a new refresh token back (X, Microsoft,
        Pinterest), it replaces the old one in memory and in the database.
        A single-use token another worker has just spent is recovered from
        the database instead of failing.
        """
        try:
            data = self._refresh_call(refresh)
        except _RefreshRefused:
            recovered = self._recover_rotated(refresh, now)
            if recovered is not None:
                return recovered
            raise
        return self._accept(data, refresh, now)

    def _recover_rotated(self, used: str, now: float) -> tuple[str, float] | None:
        org_id = getattr(self, "org_id", "")
        if not org_id:
            return None
        from app.services import oauth_tokens

        stored = oauth_tokens.load_current(str(org_id), self.slug)  # type: ignore[attr-defined]
        replacement = stored.get("refreshToken", "")
        if not replacement or replacement == used:
            return None
        self.credentials.values.update(stored)  # type: ignore[attr-defined]
        fresh = self._stored_token(now)
        if fresh:
            return fresh, self._token_expires
        return self._accept(self._refresh_call(replacement), replacement, now)

    def _accept(self, data: dict, used: str, now: float) -> tuple[str, float]:
        token = data.get("access_token") or ""
        if not token:
            raise ConnectorError(
                f"{self.name} got no access_token back from the refresh call."  # type: ignore[attr-defined]
            )
        # Vendors report expiry inconsistently; an hour is the common default
        # and erring short only costs an extra exchange.
        lifetime = float(data.get("expires_in") or 3600)
        log.info("%s: refreshed its access token", getattr(self, "slug", "connector"))
        renewed = str(data.get("refresh_token") or "")
        if renewed and renewed != used:
            values = self.credentials.values  # type: ignore[attr-defined]
            values["refreshToken"] = renewed
            values["accessToken"] = token
            values[ACCESS_TOKEN_EXPIRY_KEY] = str(int(now + lifetime))
            org_id = getattr(self, "org_id", "")
            if org_id:
                from app.services import oauth_tokens

                oauth_tokens.save_rotated(
                    str(org_id),
                    self.slug,  # type: ignore[attr-defined]
                    used_refresh=used,
                    refresh=renewed,
                    access=token,
                    expires_at=now + lifetime,
                )
        return token, now + max(lifetime - _EARLY, 30)

    def _refresh_call(self, refresh: str) -> dict:
        """The refresh request. Raises :class:`_RefreshRefused` when refused."""
        client_id, client_secret = self._client_credentials()
        if not client_id or not client_secret:
            raise ConnectorConfigError(
                f"{self.name} has a refresh token but no OAuth client id and "  # type: ignore[attr-defined]
                "secret. All three come from the same app registration."
            )

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
            "client_secret": client_secret,
            **self.token_extra,
        }
        headers: dict[str, str] = {}
        values = self.credentials.values  # type: ignore[attr-defined]
        provider = self._provider() if from_platform_sign_in(values) else None
        if (provider.token_auth if provider is not None else self.token_auth) == "basic":
            # Client authentication by Basic header (X, Pinterest).
            payload.pop("client_secret")
            raw = f"{client_id}:{client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        try:
            if self._token_url_guarded():
                response = self._guarded_token_post(payload, headers)
            else:
                with httpx.Client(timeout=settings.connector_timeout_seconds) as client:
                    response = client.post(self.token_url, data=payload, headers=headers)
        except egress.EgressBlockedError as exc:
            # The guard logged a hash of the host; never the address itself.
            log.warning(
                "%s token endpoint refused by the egress guard: reason=%s",
                getattr(self, "slug", "connector"),
                exc.reason,
            )
            raise ConnectorConfigError(
                _token_refusal_message(self.name, exc),  # type: ignore[attr-defined]
                details={"reason": exc.reason},
            ) from None
        except httpx.HTTPError as exc:
            slug = getattr(self, "slug", "connector")
            if self._token_url_guarded():
                # The address is the customer's; the raw error may name what it
                # resolved to, so only the type is logged and a plain sentence shown.
                log.warning("%s token endpoint unreachable: %s", slug, type(exc).__name__)
                raise ConnectorError(
                    f"{self.name} could not reach its sign-in address to refresh the "  # type: ignore[attr-defined]
                    "access token. Check the login address and try again."
                ) from None
            # Never the raw error or the address in the message: the error can
            # echo the request, and the message reaches people and job records.
            log_failure(log, "token endpoint unreachable", exc, slug=slug)
            raise ConnectorError(
                f"{self.name} could not reach its sign-in service to refresh the "  # type: ignore[attr-defined]
                "access token. Try again in a few minutes."
            ) from None

        if response.status_code >= 400:
            # invalid_grant (a revoked or expired refresh token) is the common
            # one and means somebody has to re-authorise. The vendor's body is
            # kept, redacted, on the error for diagnosis, never in the message.
            refused = _RefreshRefused.from_response(
                self.name, response.status_code, response.text  # type: ignore[attr-defined]
            )
            log_failure(
                log, "token refresh refused", refused,
                slug=getattr(self, "slug", "connector"),
                status=refused.vendor_status,
                oauth_error=refused.oauth_error or "none",
            )
            raise refused
        try:
            data = response.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _token_url_guarded(self) -> bool:
        """Whether the token endpoint comes from what the customer typed.

        True for a connector whose addresses are customer-supplied
        (``BaseConnector.egress_guarded``): Salesforce builds its token URL
        from the free-text Login URL. Vendor-fixed endpoints, and Shopify's
        (only ever ``<store>.myshopify.com``, checked by ``SHOP_DOMAIN``),
        keep the plain client.
        """
        return bool(getattr(self, "egress_guarded", False))

    def _guarded_token_post(self, payload: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        """POST to a customer-typed token endpoint through the egress guard:
        https only, public addresses only, pinned, no redirect, capped reply."""
        with egress.pinned_client(
            timeout=settings.connector_timeout_seconds,
            allow=egress.NO_ALLOWLIST,
            inner_backend=getattr(self, "_egress_inner_backend", None),
        ) as client:
            return egress.fetch(
                client,
                "POST",
                self.token_url,
                allow=egress.NO_ALLOWLIST,
                require_https=True,
                max_bytes=_TOKEN_MAX_BYTES,
                # A token endpoint has no business redirecting; following one
                # would carry the client secret and refresh token elsewhere.
                max_redirects=0,
                data=payload,
                headers=headers,
            )


def _token_refusal_message(name: str, exc: egress.EgressBlockedError) -> str:
    """Plain sentence for a refused customer-typed token endpoint."""
    if exc.reason == "redirects":
        return (
            f"The sign-in address for {name} answered with a redirect, which is not "
            "allowed when renewing access. Check the login address."
        )
    return f"{exc.message} Check the login address for {name}."


#: Standard OAuth 2.0 token-endpoint error codes (RFC 6749 section 5.2).
#: Only these are ever repeated in a message: they are fixed words, never
#: vendor prose. ``invalid_grant`` is what callers look for to ask for a new
#: sign-in.
_OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)
_OAUTH_ERROR_IN_TEXT = re.compile(r"\b(" + "|".join(sorted(_OAUTH_ERROR_CODES)) + r")\b")
#: Largest piece of a refusal body inspected for an error code.
_REFUSAL_SCAN_CHARS = 4_000


def _oauth_error_code(body: str) -> str:
    """The standard OAuth error code in a refusal body, or ``""``."""
    text = (body or "")[:_REFUSAL_SCAN_CHARS]
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        code = data.get("error")
        if isinstance(code, str) and code in _OAUTH_ERROR_CODES:
            return code
    match = _OAUTH_ERROR_IN_TEXT.search(text)
    return match.group(1) if match else ""


class _RefreshRefused(ConnectorError):
    """The vendor refused the refresh token (usually ``invalid_grant``).

    The message is a fixed sentence. When the vendor named a standard OAuth
    error code it is appended (``invalid_grant`` and the like, never vendor
    prose), because that is what tells a caller a new sign-in is needed.
    The status and a redacted, length-capped piece of the vendor's body are
    on ``vendor_status``, ``oauth_error`` and ``vendor_detail``, for logs.
    """

    def __init__(
        self, message: str, *, vendor_status: int = 0, oauth_error: str = "", vendor_detail: str = ""
    ) -> None:
        super().__init__(message)
        self.vendor_status = vendor_status
        self.oauth_error = oauth_error
        self.vendor_detail = vendor_detail

    @classmethod
    def from_response(cls, name: str, status: int, body: str) -> _RefreshRefused:
        code = _oauth_error_code(body)
        if code == "invalid_grant":
            message = (
                f"{name} could not refresh its access token because the sign-in has "
                "expired or was revoked. Reconnect it under Integrations (invalid_grant)."
            )
        elif code:
            message = (
                f"{name} could not refresh its access token. Check its sign-in "
                f"details under Integrations ({code})."
            )
        else:
            message = (
                f"{name} could not refresh its access token. Check its sign-in "
                "details under Integrations."
            )
        return cls(
            message,
            vendor_status=status,
            oauth_error=code,
            vendor_detail=bounded_redacted(body, limit=300),
        )


def sign_in_expired_message(connector: object) -> str:
    """The plain sentence for a sign-in that has run out."""
    from app.connectors.base import oauth_providers

    name = getattr(connector, "name", "This integration")
    provider = oauth_providers.provider_for(getattr(connector, "spec", None))
    label = provider.label if provider else "vendor"
    return (
        f"The {label} sign-in for {name} has expired. Open {name} under "
        "Integrations and choose Sign in again."
    )
