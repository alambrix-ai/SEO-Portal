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

A full redirect flow is a separate piece of work and is honestly a different
feature: it needs a registered OAuth client per vendor, a callback route on a
public URL, state and PKCE, and consent screens no test here can drive. This
module is what makes the connectors work now, and the place that flow would
deposit its tokens when it exists.
"""
from __future__ import annotations

import time

import httpx

from app.connectors.base.credentials import secret, text
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Refreshed this many seconds before the vendor's stated expiry, so a call
#: cannot start with a valid token and finish with an expired one.
_EARLY = 120


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
            self._token, self._token_expires = self._exchange(refresh, now)
            return self._token

        pasted = self.credentials.get("accessToken")  # type: ignore[attr-defined]
        if pasted:
            return pasted
        # Named so the message says which field to fill in, not "accessToken".
        raise ConnectorConfigError(
            f"{self.name} has no access token. Enter the token the vendor "  # type: ignore[attr-defined]
            "issued, or a refresh token with its client id and secret."
        )

    def _exchange(self, refresh: str, now: float) -> tuple[str, float]:
        """Swap the refresh token for an access token."""
        client_id = self.credentials.get("clientId")  # type: ignore[attr-defined]
        client_secret = self.credentials.get("clientSecret")  # type: ignore[attr-defined]
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
        try:
            with httpx.Client(timeout=settings.connector_timeout_seconds) as client:
                response = client.post(self.token_url, data=payload)
        except httpx.HTTPError as exc:
            raise ConnectorError(
                f"Could not reach {self.token_url} to refresh the access token: {exc}"
            ) from exc

        if response.status_code >= 400:
            # The body carries the vendor's own reason — invalid_grant when a
            # refresh token has been revoked, which is the common one and
            # means somebody has to re-authorise.
            raise ConnectorError(
                f"{self.name} could not refresh its access token "  # type: ignore[attr-defined]
                f"({response.status_code}): {response.text[:300]}"
            )

        data = response.json()
        token = data.get("access_token") or ""
        if not token:
            raise ConnectorError(
                f"{self.name} got no access_token back from the refresh call."  # type: ignore[attr-defined]
            )
        # Vendors report expiry inconsistently; an hour is the common default
        # and erring short only costs an extra exchange.
        lifetime = float(data.get("expires_in") or 3600)
        log.info("%s: refreshed its access token", getattr(self, "slug", "connector"))
        return token, now + max(lifetime - _EARLY, 30)
