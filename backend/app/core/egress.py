"""Egress guard: outbound HTTP that cannot be steered at internal addresses.

A customer-supplied address (a self-hosted model's base URL, a connector's
API root, a website to read) must never let the server reach cloud metadata
(``169.254.169.254``), loopback, or a private network. Checking the URL once
and then letting httpx resolve it again is not enough: DNS can answer
differently the second time (rebinding), and decimal or octal host forms
dodge string checks.

So the check lives where the connection is made. The clients built here swap
httpcore's network backend for a guarded one whose ``connect_tcp`` resolves
the host itself, validates EVERY returned address against :func:`ip_allowed`,
and connects to a validated address. The URL, Host header, connection-pool
key, SNI and certificate check all stay on the real hostname; only the socket
is pinned. The backend is installed through ``transport._pool._network_backend``
(a private httpcore attribute; httpx 0.28.1 / httpcore 1.0.9), and
:func:`_install_backend` fails closed if that attribute ever moves.

Clients never follow redirects and ignore proxy environment variables. Use
:func:`fetch` / :func:`afetch` to follow redirects by hand (re-validated on
every hop) with a streamed byte cap.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import typing
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

import anyio
import anyio.to_thread
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend
from httpcore._backends.sync import SyncBackend

from app.core.exceptions import InvalidInputError
from app.core.logging import get_logger

log = get_logger(__name__)

IPAddress = typing.Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPNetwork = typing.Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
#: Validated addresses tried in turn before giving up on a host.
MAX_CONNECT_ATTEMPTS = 4
DEFAULT_MAX_BYTES = 2_000_000
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Headers that must not follow a redirect to a different origin.
_CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "cookie")

#: Blocked even when an administrator allow-lists them: cloud metadata
#: (AWS/GCP/Azure link-local, AWS IPv6, Alibaba, the Azure wire server,
#: Oracle Cloud's IPv6 metadata, and 192.0.0.192 which Oracle also answers
#: metadata on), "this network" and the unspecified address.
_ALWAYS_BLOCKED: tuple[IPNetwork, ...] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "169.254.0.0/16",
        "fd00:ec2::254/128",
        "100.100.100.200/32",
        "168.63.129.16/32",
        "fd20:ce::254/128",
        "192.0.0.192/32",
        "0.0.0.0/8",
        "::/128",
    )
)
#: IPv6 ranges that embed an IPv4 address (NAT64 and 6to4). They can carry an
#: internal IPv4 target, so they need an allow-list entry, and the embedded
#: address still faces the always-blocked list.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")

# Plain-English messages; the user typed the address, so tell them what to fix.
MSG_SCHEME = "Enter an address starting with https://"
MSG_INVALID = "That address isn't valid. Check it and try again."
MSG_UNRESOLVABLE = "That address could not be found. Check it and try again."
MSG_BLOCKED = (
    "That address points to a private or internal network, "
    "so it can't be reached from here."
)
MSG_TOO_LARGE = "The reply from that address was too large to read."
MSG_REDIRECTS = "That address redirects too many times."
MSG_REDIRECT_ELSEWHERE = (
    "That address redirected to a different site, which is not allowed for "
    "a connection that signs in. Enter the final address and try again."
)
#: Ports a self-hosted model server may listen on when
#: SELF_HOSTED_LLM_ALLOWED_PORTS is not set (https, vLLM, Ollama).
DEFAULT_SELF_HOSTED_PORTS: tuple[int, ...] = (443, 8000, 11434)


class EgressBlockedError(InvalidInputError):
    """An outbound address was refused. ``reason`` is a stable code:
    ``scheme``, ``invalid``, ``unresolvable``, ``blocked``, ``port``,
    ``too_large`` or ``redirects``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, details={"reason": reason})
        self.reason = reason


# ── Allow-list ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AllowList:
    """Admin-approved private destinations: exact host names and CIDRs.

    Host entries match the hostname being connected to; CIDR entries match
    each resolved address. Neither can unblock :data:`_ALWAYS_BLOCKED`.
    """

    hosts: frozenset[str] = field(default_factory=frozenset)
    networks: tuple[IPNetwork, ...] = ()

    @classmethod
    def from_entries(cls, entries: Iterable[str]) -> AllowList:
        hosts: set[str] = set()
        networks: list[IPNetwork] = []
        for raw in entries:
            item = (raw or "").strip()
            if not item:
                continue
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError:
                hosts.add(_normalise_host(item))
                continue
            if network.prefixlen == 0:
                # A catch-all range would switch the private-address guard off.
                raise ValueError("an egress allow-list must not contain 0.0.0.0/0 or ::/0")
            networks.append(network)
        return cls(hosts=frozenset(hosts), networks=tuple(networks))

    def __bool__(self) -> bool:
        return bool(self.hosts or self.networks)

    def allows_host(self, host: str | None) -> bool:
        return bool(host) and _normalise_host(host or "") in self.hosts

    def allows_ip(self, ip: IPAddress) -> bool:
        return any(ip.version == n.version and ip in n for n in self.networks)


NO_ALLOWLIST = AllowList()


def self_hosted_llm_allow_list() -> AllowList:
    """The allow-list for self-hosted model endpoints (SELF_HOSTED_LLM_ALLOWED_HOSTS)."""
    from app.core.config import settings

    return AllowList.from_entries(settings.self_hosted_llm_allowed_hosts)


def self_hosted_llm_allow_plain_http() -> bool:
    """SELF_HOSTED_LLM_ALLOW_PLAIN_HTTP: whether a self-hosted model server
    inside an allow-listed private CIDR may be reached over plain http.
    Off unless an administrator turns it on."""
    from app.core.config import settings

    return getattr(settings, "self_hosted_llm_allow_plain_http", False) is True


def self_hosted_llm_allowed_ports() -> frozenset[int]:
    """SELF_HOSTED_LLM_ALLOWED_PORTS: the ports a self-hosted model server may use."""
    from app.core.config import settings

    raw = getattr(settings, "self_hosted_llm_allowed_ports", None)
    if raw is None:
        raw = DEFAULT_SELF_HOSTED_PORTS
    ports: set[int] = set()
    for item in raw:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if 0 < port < 65536:
            ports.add(port)
    return frozenset(ports)


def _normalise_host(host: str) -> str:
    return host.strip().lower().rstrip(".").strip("[]")


# ── IP policy (the single source of truth) ────────────────────────────────
def parse_address(address: str) -> IPAddress:
    """Parse a resolved address: drop any ``%zone`` and unwrap IPv4-mapped IPv6."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _embedded_ipv4(ip: IPAddress) -> ipaddress.IPv4Address | None:
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _SIXTOFOUR:
        return ip.sixtofour
    return None


def _always_blocked(ip: IPAddress) -> bool:
    if any(ip.version == n.version and ip in n for n in _ALWAYS_BLOCKED):
        return True
    if ip.is_multicast:
        return True
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        # NAT64 sits inside ::/8, which Python reports as reserved; judge the
        # IPv4 address it carries instead, so an allow-list entry can apply.
        return _always_blocked(embedded)
    return ip.is_reserved


def ip_allowed(address: str, *, host: str | None = None, allow: AllowList = NO_ALLOWLIST) -> bool:
    """Whether an outbound connection to ``address`` is permitted.

    1. Strip ``%zone``; unwrap IPv4-mapped IPv6.
    2. Always refuse metadata, ``0.0.0.0/8``, ``::``, multicast and reserved,
       whatever the allow-list says.
    3. Allow when ``host`` or the address is on ``allow``.
    4. Refuse NAT64 and 6to4 (they embed an IPv4 target).
    5. Otherwise allow only globally routable addresses (this refuses private,
       loopback, link-local and CGNAT ``100.64.0.0/10``).

    Unparseable input is refused. Validate resolved addresses, never the raw
    host string: decimal and octal forms resolve to ordinary addresses.
    """
    try:
        ip = parse_address(address)
    except ValueError:
        return False
    if _always_blocked(ip):
        return False
    if allow.allows_host(host) or allow.allows_ip(ip):
        return True
    if _embedded_ipv4(ip) is not None:
        return False
    return ip.is_global


def resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to, de-duplicated, in resolver order."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise EgressBlockedError("unresolvable", MSG_UNRESOLVABLE) from None
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise EgressBlockedError("unresolvable", MSG_UNRESOLVABLE)
    return addresses


def private_cidr_allowed(address: str, *, allow: AllowList) -> bool:
    """Whether ``address`` is allowed, NOT globally routable, and inside a
    CIDR entry of ``allow``. This is the bar for plain http: a host-name
    entry never clears it, and neither does a public address."""
    if not ip_allowed(address, allow=allow):
        return False
    ip = parse_address(address)
    return not ip.is_global and allow.allows_ip(ip)


def resolve_allowed(
    host: str, port: int, *, allow: AllowList = NO_ALLOWLIST, private_cidr_only: bool = False
) -> list[str]:
    """Resolve ``host`` and return its addresses only if EVERY one is allowed.

    With ``private_cidr_only`` (plain-http connections) every address must
    also pass :func:`private_cidr_allowed`.
    """
    addresses = resolve(host, port)
    if private_cidr_only:
        ok = all(private_cidr_allowed(a, allow=allow) for a in addresses)
    else:
        ok = all(ip_allowed(a, host=host, allow=allow) for a in addresses)
    if not ok:
        _log_block("blocked", host)
        raise EgressBlockedError("blocked", MSG_BLOCKED)
    return addresses


def _log_block(reason: str, host: str) -> None:
    # The host may be customer-identifying; log a short hash, never the URL.
    digest = hashlib.sha256(host.encode("utf-8", "replace")).hexdigest()[:12]
    log.warning("egress refused: reason=%s host_sha=%s", reason, digest)


# ── URL validation ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Target:
    """A validated destination. ``addresses`` are informational: the
    connection re-resolves and re-validates at connect time."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


def validate_url(
    url: str | httpx.URL,
    *,
    allow: AllowList = NO_ALLOWLIST,
    require_https: bool = True,
    allowed_ports: Collection[int] | None = None,
    allow_plain_http: bool = False,
) -> Target:
    """Check scheme, host, port and every resolved address of ``url``.

    With ``require_https`` plain http is refused unless ``allow_plain_http``
    is set AND every resolved address passes :func:`private_cidr_allowed`
    (non-global and inside an allow-listed CIDR). A host-name entry on
    ``allow`` never permits plain http.
    """
    text = str(url).strip() if url is not None else ""
    if not text or len(text) > MAX_URL_LENGTH:
        raise EgressBlockedError("invalid", MSG_INVALID)
    try:
        parsed = httpx.URL(text)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (httpx.InvalidURL, ValueError, TypeError):
        raise EgressBlockedError("invalid", MSG_INVALID) from None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise EgressBlockedError("scheme", MSG_SCHEME)
    host = parsed.host
    if not host or parsed.userinfo:
        raise EgressBlockedError("invalid", MSG_INVALID)
    if allowed_ports is not None and port not in allowed_ports:
        _log_block("port", host)
        raise EgressBlockedError("port", MSG_INVALID)
    addresses = resolve_allowed(host, port, allow=allow)
    if scheme == "http" and require_https:
        private_only = allow_plain_http and all(
            private_cidr_allowed(a, allow=allow) for a in addresses
        )
        if not private_only:
            _log_block("scheme", host)
            raise EgressBlockedError("scheme", MSG_SCHEME)
    return Target(url=str(parsed), scheme=scheme, host=host, port=port, addresses=tuple(addresses))


def validate_base_url(
    url: str,
    *,
    allow: AllowList = NO_ALLOWLIST,
    allow_plain_http: bool = False,
    allowed_ports: Collection[int] | None = None,
) -> Target:
    """Validate a user-supplied API base URL: https, unless ``allow_plain_http``
    and every address is inside an allow-listed private CIDR."""
    return validate_url(
        url, allow=allow, require_https=True, allowed_ports=allowed_ports,
        allow_plain_http=allow_plain_http,
    )


# ── Guarded network backends ──────────────────────────────────────────────
class GuardedSyncBackend(httpcore.NetworkBackend):
    """Resolve, validate every address, then connect to a validated one.

    With ``private_cidr_only`` (a plain-http client) only addresses passing
    :func:`private_cidr_allowed` are accepted, so a name that re-resolves
    cannot move a plain-http connection onto a public address.
    """

    def __init__(
        self,
        allow: AllowList = NO_ALLOWLIST,
        inner: httpcore.NetworkBackend | None = None,
        *,
        private_cidr_only: bool = False,
    ) -> None:
        self._allow = allow
        self._inner = inner if inner is not None else SyncBackend()
        self._private_cidr_only = private_cidr_only

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        addresses = resolve_allowed(
            host, port, allow=self._allow, private_cidr_only=self._private_cidr_only
        )
        last_error: Exception | None = None
        for address in addresses[:MAX_CONNECT_ATTEMPTS]:
            try:
                return self._inner.connect_tcp(
                    address.split("%", 1)[0], port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError("no address to connect to")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise EgressBlockedError("blocked", MSG_BLOCKED)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class GuardedAsyncBackend(httpcore.AsyncNetworkBackend):
    """Async twin of :class:`GuardedSyncBackend` (resolution runs in a thread)."""

    def __init__(
        self,
        allow: AllowList = NO_ALLOWLIST,
        inner: httpcore.AsyncNetworkBackend | None = None,
        *,
        private_cidr_only: bool = False,
    ) -> None:
        self._allow = allow
        self._inner = inner if inner is not None else AnyIOBackend()
        self._private_cidr_only = private_cidr_only

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            with anyio.fail_after(timeout):
                addresses = await anyio.to_thread.run_sync(
                    lambda: resolve_allowed(
                        host, port, allow=self._allow, private_cidr_only=self._private_cidr_only
                    )
                )
        except TimeoutError:
            raise httpcore.ConnectTimeout("name resolution timed out") from None
        last_error: Exception | None = None
        for address in addresses[:MAX_CONNECT_ATTEMPTS]:
            try:
                return await self._inner.connect_tcp(
                    address.split("%", 1)[0], port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError("no address to connect to")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise EgressBlockedError("blocked", MSG_BLOCKED)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _install_backend(
    transport: httpx.HTTPTransport | httpx.AsyncHTTPTransport,
    backend: httpcore.NetworkBackend | httpcore.AsyncNetworkBackend,
) -> None:
    """Swap the pool's network backend. Fails closed if httpcore changed shape."""
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, (httpcore.ConnectionPool, httpcore.AsyncConnectionPool)) or not hasattr(
        pool, "_network_backend"
    ):
        raise RuntimeError("egress guard cannot attach to this httpx/httpcore version")
    pool._network_backend = backend


def _timeout(timeout: float | httpx.Timeout) -> httpx.Timeout:
    return timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)


def pinned_client(
    *,
    timeout: float | httpx.Timeout,
    allow: AllowList = NO_ALLOWLIST,
    headers: dict[str, str] | None = None,
    inner_backend: httpcore.NetworkBackend | None = None,
    private_cidr_only: bool = False,
) -> httpx.Client:
    """A sync client whose every connection goes through the egress guard.

    No redirects, no proxy env vars, no connection retries, TLS verified.
    ``inner_backend`` replaces the raw socket layer (tests only); it is still
    wrapped by the guard. ``private_cidr_only`` is for plain-http clients
    (see :class:`GuardedSyncBackend`).
    """
    transport = httpx.HTTPTransport(verify=True, retries=0, trust_env=False)
    _install_backend(
        transport, GuardedSyncBackend(allow, inner_backend, private_cidr_only=private_cidr_only)
    )
    return httpx.Client(
        transport=transport, timeout=_timeout(timeout), follow_redirects=False,
        trust_env=False, headers=headers,
    )


def pinned_async_client(
    *,
    timeout: float | httpx.Timeout,
    allow: AllowList = NO_ALLOWLIST,
    headers: dict[str, str] | None = None,
    inner_backend: httpcore.AsyncNetworkBackend | None = None,
    private_cidr_only: bool = False,
) -> httpx.AsyncClient:
    """Async twin of :func:`pinned_client`."""
    transport = httpx.AsyncHTTPTransport(verify=True, retries=0, trust_env=False)
    _install_backend(
        transport, GuardedAsyncBackend(allow, inner_backend, private_cidr_only=private_cidr_only)
    )
    return httpx.AsyncClient(
        transport=transport, timeout=_timeout(timeout), follow_redirects=False,
        trust_env=False, headers=headers,
    )


# ── Requests with manual redirects and a byte cap ─────────────────────────
def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _next_request(
    client: httpx.Client | httpx.AsyncClient,
    request: httpx.Request,
    response: httpx.Response,
    *,
    allow: AllowList,
    require_https: bool,
    allowed_ports: Collection[int] | None,
    cross_origin_redirects: bool = True,
) -> httpx.Request:
    location = response.headers.get("location", "")
    if not location:
        raise EgressBlockedError("invalid", MSG_INVALID)
    try:
        next_url = request.url.join(location)
    except (httpx.InvalidURL, ValueError):
        raise EgressBlockedError("invalid", MSG_INVALID) from None
    validate_url(next_url, allow=allow, require_https=require_https, allowed_ports=allowed_ports)
    if not cross_origin_redirects and _origin(next_url) != _origin(request.url):
        # A credentialed caller's custom key headers (X-Api-Key and the like)
        # and a 307/308 body would otherwise follow the hop to another site.
        _log_block("redirects", request.url.host)
        raise EgressBlockedError("redirects", MSG_REDIRECT_ELSEWHERE)
    method, content = request.method, request.content
    if response.status_code in (301, 302, 303) and method != "HEAD":
        method, content = "GET", None
    headers = httpx.Headers(request.headers)
    for name in ("host", "content-length", "transfer-encoding"):
        headers.pop(name, None)
    if content is None:
        headers.pop("content-type", None)
    next_request = client.build_request(method, next_url, headers=headers, content=content)
    if _origin(next_url) != _origin(request.url):
        # Removed after build_request, which re-merges the client's defaults.
        for name in _CREDENTIAL_HEADERS:
            next_request.headers.pop(name, None)
    return next_request


def _capped_response(
    response: httpx.Response, body: bytes, request: httpx.Request
) -> httpx.Response:
    # The body is already decoded, so the transfer headers no longer describe it.
    headers = [
        (k, v) for k, v in response.headers.multi_items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
    ]
    return httpx.Response(response.status_code, headers=headers, content=body, request=request)


def _declared_too_large(response: httpx.Response, max_bytes: int) -> bool:
    declared = response.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > max_bytes


def fetch(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    allow: AllowList = NO_ALLOWLIST,
    require_https: bool = True,
    allowed_ports: Collection[int] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    truncate: bool = False,
    cross_origin_redirects: bool = True,
    **request_kwargs: typing.Any,
) -> httpx.Response:
    """Send a request through ``client`` (from :func:`pinned_client`),
    following up to ``max_redirects`` redirects (capped at 5), each hop
    re-validated. The decoded body is read up to ``max_bytes``: beyond that
    it raises, or with ``truncate`` keeps the first ``max_bytes``.

    ``cross_origin_redirects=False`` (credentialed callers) refuses a hop to
    a different scheme, host or port with reason ``redirects``."""
    validate_url(url, allow=allow, require_https=require_https, allowed_ports=allowed_ports)
    request = client.build_request(method, url, **request_kwargs)
    for _ in range(min(max_redirects, MAX_REDIRECTS) + 1):
        response = client.send(request, stream=True)
        try:
            if response.status_code in _REDIRECT_STATUSES:
                request = _next_request(
                    client, request, response, allow=allow,
                    require_https=require_https, allowed_ports=allowed_ports,
                    cross_origin_redirects=cross_origin_redirects,
                )
                continue
            if not truncate and _declared_too_large(response, max_bytes):
                raise EgressBlockedError("too_large", MSG_TOO_LARGE)
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    if not truncate:
                        raise EgressBlockedError("too_large", MSG_TOO_LARGE)
                    del body[max_bytes:]
                    break
            return _capped_response(response, bytes(body), request)
        finally:
            response.close()
    raise EgressBlockedError("redirects", MSG_REDIRECTS)


async def afetch(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    allow: AllowList = NO_ALLOWLIST,
    require_https: bool = True,
    allowed_ports: Collection[int] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    truncate: bool = False,
    cross_origin_redirects: bool = True,
    **request_kwargs: typing.Any,
) -> httpx.Response:
    """Async twin of :func:`fetch`."""
    await anyio.to_thread.run_sync(
        lambda: validate_url(url, allow=allow, require_https=require_https, allowed_ports=allowed_ports)
    )
    request = client.build_request(method, url, **request_kwargs)
    for _ in range(min(max_redirects, MAX_REDIRECTS) + 1):
        response = await client.send(request, stream=True)
        try:
            if response.status_code in _REDIRECT_STATUSES:
                current = request
                request = await anyio.to_thread.run_sync(
                    lambda: _next_request(
                        client, current, response, allow=allow,
                        require_https=require_https, allowed_ports=allowed_ports,
                        cross_origin_redirects=cross_origin_redirects,
                    )
                )
                continue
            if not truncate and _declared_too_large(response, max_bytes):
                raise EgressBlockedError("too_large", MSG_TOO_LARGE)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    if not truncate:
                        raise EgressBlockedError("too_large", MSG_TOO_LARGE)
                    del body[max_bytes:]
                    break
            return _capped_response(response, bytes(body), request)
        finally:
            await response.aclose()
    raise EgressBlockedError("redirects", MSG_REDIRECTS)
