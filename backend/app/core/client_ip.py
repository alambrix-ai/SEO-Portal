"""The caller's real address, and the scheme it connected with.

``X-Forwarded-For`` and ``X-Forwarded-Proto`` are written by whoever sends the
request, so they are only believed when the connecting peer is one of our own
proxies (``TRUSTED_PROXIES``). The server runs with ``--no-proxy-headers`` so
this module is the one place those headers are read.

The walk follows the usual right-to-left rule: each trusted proxy appends the
address it received from, so the right-most entry that is *not* one of ours is
the first hop we did not write — the client. Anything to its left was supplied
by that client and is ignored, which is what defeats a spoofed left-most entry.
"""
from __future__ import annotations

from functools import lru_cache
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)

from starlette.requests import HTTPConnection

from app.core.config import settings

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network

#: Longest single entry worth parsing; "[v6%zone]:port" fits comfortably.
_MAX_ENTRY_LENGTH = 100
#: IPv6 clients are rate-limited per /64: one subscriber usually holds a whole
#: /64, so limiting per address would let them rotate through 2^64 keys.
_IPV6_RATE_LIMIT_PREFIX = 64


@lru_cache(maxsize=8)
def _networks(cidrs: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    return tuple(ip_network(cidr, strict=False) for cidr in cidrs)


def trusted_networks() -> tuple[IPNetwork, ...]:
    """``TRUSTED_PROXIES`` as network objects (never compared as strings)."""
    return _networks(tuple(settings.trusted_proxies))


def _normalise(ip: IPAddress) -> IPAddress:
    """Map ``::ffff:a.b.c.d`` to ``a.b.c.d`` so one client has one identity."""
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_ip(raw: str) -> IPAddress | None:
    """Parse one address as a proxy writes it, or ``None`` if it isn't one.

    Accepts ``a.b.c.d``, ``a.b.c.d:port``, a bare IPv6 address,
    ``[v6]`` and ``[v6]:port``, with any ``%zone`` suffix dropped.
    """
    value = raw.strip().strip('"').strip()
    if not value or len(value) > _MAX_ENTRY_LENGTH:
        return None

    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host, rest = value[1:end], value[end + 1 :]
        if rest and not (rest.startswith(":") and _is_port(rest[1:])):
            return None
    elif value.count(":") == 1:
        # Exactly one colon can only be IPv4 with a port; IPv6 has at least two.
        host, _, port = value.partition(":")
        if not _is_port(port):
            return None
    else:
        host = value

    host = host.split("%", 1)[0]
    try:
        return _normalise(ip_address(host))
    except ValueError:
        return None


def _is_port(value: str) -> bool:
    return 0 < len(value) <= 5 and value.isascii() and value.isdigit()


def _is_trusted(ip: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    # ``in`` across IP versions is simply False, so mixed lists are safe.
    return any(ip in network for network in networks)


def resolve_from(
    peer: str | None,
    forwarded_for: list[str],
    networks: tuple[IPNetwork, ...],
) -> str:
    """The client address, given the peer and every ``X-Forwarded-For`` value.

    Pure, so the walk can be tested without a request. Returns ``""`` when
    there is no peer at all.
    """
    peer_ip = parse_ip(peer) if peer else None
    if peer_ip is None:
        # Not an address we can reason about (a test client, a Unix socket):
        # it cannot be a trusted proxy, so the headers are not consulted.
        return (peer or "")[:_MAX_ENTRY_LENGTH]
    if not networks or not _is_trusted(peer_ip, networks):
        return str(peer_ip)

    # Repeated headers are one list, in order (RFC 9110 §5.3).
    entries = [part for value in forwarded_for for part in value.split(",")]
    entries = [entry for entry in (part.strip() for part in entries) if entry]
    if not entries:
        return str(peer_ip)

    last_good: IPAddress = peer_ip
    for entry in reversed(entries):
        hop = parse_ip(entry)
        if hop is None:
            # Garbage in the chain: stop at the last hop we could read rather
            # than guess past it.
            return str(last_good)
        if not _is_trusted(hop, networks):
            return str(hop)
        last_good = hop
    # Every hop is one of ours (an internal caller): the left-most is the origin.
    return str(last_good)


def resolve(connection: HTTPConnection) -> str:
    """The address to audit and rate-limit this request by."""
    peer = connection.client.host if connection.client else None
    return resolve_from(
        peer, connection.headers.getlist("x-forwarded-for"), trusted_networks()
    )


def forwarded_proto(connection: HTTPConnection) -> str:
    """``"https"`` or ``"http"``: the scheme the client actually used.

    ``X-Forwarded-Proto`` counts only from a trusted peer, and only its
    right-most value (the one our proxy wrote). Anything other than a plain
    ``http``/``https`` fails closed to ``"http"``.
    """
    peer = connection.client.host if connection.client else None
    peer_ip = parse_ip(peer) if peer else None
    networks = trusted_networks()
    if peer_ip is None or not networks or not _is_trusted(peer_ip, networks):
        return "https" if connection.url.scheme in ("https", "wss") else "http"

    values = [
        part.strip().lower()
        for value in connection.headers.getlist("x-forwarded-proto")
        for part in value.split(",")
    ]
    values = [value for value in values if value]
    if not values:
        return "https" if connection.url.scheme in ("https", "wss") else "http"
    return values[-1] if values[-1] in ("http", "https") else "http"


def rate_limit_key(address: str) -> str:
    """The bucket an address counts against: itself, or its /64 for IPv6."""
    ip = parse_ip(address) if address else None
    if ip is None:
        return address or "unknown"
    if isinstance(ip, IPv6Address):
        return str(ip_network(f"{ip}/{_IPV6_RATE_LIMIT_PREFIX}", strict=False))
    return str(ip)
