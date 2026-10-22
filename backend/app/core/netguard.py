"""Outbound URL guard: never let a user-supplied address reach internal hosts.

Several features fetch an address a customer typed — the brand profile reads
their website, the Site Crawler reads every page. Without a check, the same
box accepts ``http://169.254.169.254/`` (cloud metadata credentials),
``http://localhost:6379`` or a private-network admin panel, and the server
fetches it on the customer's behalf.

``check_public_url`` resolves the host and refuses anything that is not a
public internet address. The address policy itself lives in
:mod:`app.core.egress` (one source of truth, which also refuses CGNAT
``100.64.0.0/10``). ``safe_get`` fetches through egress's pinned client, so
the address is re-resolved and re-checked at connect time (no DNS-rebinding
gap) and every redirect hop is re-checked too.
"""
from __future__ import annotations

import socket  # noqa: F401  (kept so ``netguard.socket.getaddrinfo`` stays stubbable)
from urllib.parse import urlsplit

import httpx

from app.core import egress
from app.core.exceptions import InvalidInputError

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = egress.MAX_REDIRECTS
_SCHEME_MESSAGE = "Enter a full website address starting with https://"


def _is_public(address: str) -> bool:
    return egress.ip_allowed(address)


def _not_public(host: str) -> InvalidInputError:
    return InvalidInputError(
        f"{host} is not a public website address, so it cannot be read from here."
    )


def check_public_url(url: str) -> str:
    """Return ``url`` if it points at a public internet host; raise otherwise."""
    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
        raise InvalidInputError(_SCHEME_MESSAGE)
    host = parts.hostname
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        raise InvalidInputError(_SCHEME_MESSAGE) from None
    try:
        addresses = egress.resolve(host, port)
    except egress.EgressBlockedError:
        raise InvalidInputError(f"{host} could not be found. Check the address.") from None
    if not all(_is_public(address) for address in addresses):
        raise _not_public(host)
    return url


def safe_get(url: str, *, timeout: float = 15.0, max_bytes: int = 2_000_000) -> httpx.Response:
    """GET a public URL, re-checking every redirect hop and capping the size."""
    current = check_public_url(url)
    host = urlsplit(current.strip()).hostname or ""
    with egress.pinned_client(
        timeout=timeout, headers={"User-Agent": "Synaprank/1.0 (+brand profile)"}
    ) as client:
        try:
            return egress.fetch(
                client, "GET", current.strip(), require_https=False,
                max_bytes=max_bytes, truncate=True,
            )
        except egress.EgressBlockedError as exc:
            if exc.reason == "redirects":
                raise InvalidInputError("That address redirects too many times.") from None
            if exc.reason in ("scheme", "invalid"):
                raise InvalidInputError(_SCHEME_MESSAGE) from None
            if exc.reason == "unresolvable":
                raise InvalidInputError(f"{host} could not be found. Check the address.") from None
            raise _not_public(host) from None
