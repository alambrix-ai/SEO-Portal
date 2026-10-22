"""Egress guard: address policy, pinned connections, redirects and size caps.

No test touches the network. DNS is stubbed (``socket.getaddrinfo``) and the
raw socket layer is replaced by a recording backend that is still wrapped by
the guard, so the real httpx -> httpcore -> guarded backend path runs.
"""
from __future__ import annotations

import asyncio
import socket
import typing

import httpcore
import httpx
import pytest

from app.core import egress, netguard
from app.core.egress import AllowList, EgressBlockedError
from app.core.exceptions import InvalidInputError

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"


# ── Helpers ────────────────────────────────────────────────────────────────
def _dns(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[list[str]] | list[str]]) -> dict[str, int]:
    """Stub DNS. A host maps to a list of addresses, or to a list of answers
    returned on successive lookups (the last one repeats). Returns call counts."""
    calls: dict[str, int] = {}

    def fake(host: str, port: int, *args: object, **kwargs: object) -> list[tuple]:
        key = str(host).lower()
        if key not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        answer = answers[key]
        n = calls.get(key, 0)
        calls[key] = n + 1
        if answer and isinstance(answer[0], list):
            addresses = answer[min(n, len(answer) - 1)]
        else:
            addresses = answer
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in addresses  # type: ignore[union-attr]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a real socket was opened")

    monkeypatch.setattr(socket, "create_connection", refuse)


def _http(status: int = 200, body: bytes = b"ok", headers: dict[str, str] | None = None) -> bytes:
    lines = [f"HTTP/1.1 {status} X", f"Content-Length: {len(body)}", "Connection: close"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class _Stream(httpcore.NetworkStream):
    def __init__(self, reply: bytes, log: dict[str, typing.Any]) -> None:
        self._chunks = [reply]
        self._log = log

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._log["sent"] += buffer

    def close(self) -> None:
        pass

    def start_tls(self, ssl_context: typing.Any, server_hostname: str | None = None,
                  timeout: float | None = None) -> httpcore.NetworkStream:
        self._log["sni"] = server_hostname
        return self


class Recorder(httpcore.NetworkBackend):
    """Raw socket stand-in: records what the guard asked it to connect to."""

    def __init__(self, *replies: bytes) -> None:
        self.replies = list(replies)
        self.connects: list[dict[str, typing.Any]] = []

    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None,
                    socket_options: typing.Iterable[typing.Any] | None = None) -> httpcore.NetworkStream:
        log: dict[str, typing.Any] = {"host": host, "port": port, "sni": None, "sent": b""}
        self.connects.append(log)
        return _Stream(self.replies.pop(0) if self.replies else _http(), log)


class _AsyncStream(httpcore.AsyncNetworkStream):
    def __init__(self, inner: _Stream) -> None:
        self._inner = inner

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer)

    async def aclose(self) -> None:
        pass

    async def start_tls(self, ssl_context: typing.Any, server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        self._inner.start_tls(ssl_context, server_hostname)
        return self


class AsyncRecorder(httpcore.AsyncNetworkBackend):
    def __init__(self, *replies: bytes) -> None:
        self._sync = Recorder(*replies)
        self.connects = self._sync.connects

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options: typing.Iterable[typing.Any] | None = None) -> httpcore.AsyncNetworkStream:
        return _AsyncStream(self._sync.connect_tcp(host, port))  # type: ignore[arg-type]

    async def sleep(self, seconds: float) -> None:
        pass


# ── IP policy ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254", "::ffff:169.254.169.254", "fd00:ec2::254", "100.100.100.200",
        "0.0.0.0", "0.1.2.3", "::", "127.0.0.1", "::1", "10.0.0.1", "172.16.0.1",
        "192.168.1.1", "100.64.0.1", "100.127.255.254", "224.0.0.1", "ff02::1",
        "240.0.0.1", "fe80::1%eth0", "64:ff9b::a9fe:a9fe", "64:ff9b::808:808",
        "2002:a9fe:a9fe::1", "not-an-ip",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert egress.ip_allowed(address) is False


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert egress.ip_allowed(address) is True


def test_allow_list_opens_private_ranges_but_never_metadata() -> None:
    allow = AllowList.from_entries(
        ["10.0.0.0/8", "169.254.0.0/16", "fd00::/8", "100.64.0.0/10", "64:ff9b::/96", "llm.lan"]
    )
    assert egress.ip_allowed("10.1.2.3", allow=allow)
    assert egress.ip_allowed("127.0.0.1", host="llm.lan", allow=allow)
    assert egress.ip_allowed("64:ff9b::808:808", allow=allow)
    # Metadata, unspecified, multicast and reserved stay blocked whatever is listed.
    for blocked in ("169.254.169.254", "::ffff:169.254.169.254", "fd00:ec2::254",
                    "100.100.100.200", "64:ff9b::a9fe:a9fe", "0.0.0.0", "224.0.0.1"):
        assert not egress.ip_allowed(blocked, host="llm.lan", allow=allow), blocked
    assert not egress.ip_allowed("192.168.1.1", allow=allow)


def test_allow_list_refuses_catch_all_ranges() -> None:
    for entry in ("0.0.0.0/0", "::/0"):
        with pytest.raises(ValueError):
            AllowList.from_entries([entry])


def test_self_hosted_allow_list_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "self_hosted_llm_allowed_hosts", ["10.0.0.0/8", "gpu.lan"])
    allow = egress.self_hosted_llm_allow_list()
    assert allow.allows_host("GPU.lan.") and egress.ip_allowed("10.9.9.9", allow=allow)


# ── URL validation ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("url", "answers", "reason"),
    [
        ("https://169.254.169.254/latest/meta-data", {"169.254.169.254": ["169.254.169.254"]}, "blocked"),
        ("https://[::ffff:169.254.169.254]/", {"::ffff:169.254.169.254": ["::ffff:169.254.169.254"]}, "blocked"),
        ("https://[fd00:ec2::254]/", {"fd00:ec2::254": ["fd00:ec2::254"]}, "blocked"),
        ("https://localhost/", {"localhost": ["127.0.0.1", "::1"]}, "blocked"),
        # Resolvers that follow inet_aton turn the decimal form into loopback.
        ("https://2130706433/", {"2130706433": ["127.0.0.1"]}, "blocked"),
        ("https://0177.0.0.1/", {"0177.0.0.1": ["127.0.0.1"]}, "invalid"),
        ("https://internal.example/", {"internal.example": ["10.0.0.5"]}, "blocked"),
        ("https://cgnat.example/", {"cgnat.example": ["100.64.3.4"]}, "blocked"),
        ("https://mixed.example/", {"mixed.example": [PUBLIC, "10.0.0.5"]}, "blocked"),
        ("https://nowhere.example/", {}, "unresolvable"),
        ("http://public.example/", {"public.example": [PUBLIC]}, "scheme"),
        ("ftp://public.example/", {"public.example": [PUBLIC]}, "scheme"),
        ("https://user:pw@public.example/", {"public.example": [PUBLIC]}, "invalid"),
        ("not a url", {}, "scheme"),
    ],
)
def test_validate_base_url_refuses(monkeypatch: pytest.MonkeyPatch, url: str,
                                   answers: dict[str, list[str]], reason: str) -> None:
    _dns(monkeypatch, answers)  # type: ignore[arg-type]
    with pytest.raises(EgressBlockedError) as caught:
        egress.validate_base_url(url)
    assert caught.value.reason == reason
    assert isinstance(caught.value, InvalidInputError)  # friendly 422 for callers
    assert "10.0.0.5" not in caught.value.message and ".example" not in caught.value.message


def test_validate_base_url_accepts_public_https_and_allow_listed_http(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC], "gpu.lan": ["192.168.5.5"], "model.lan": ["10.0.0.9"]})
    target = egress.validate_base_url("https://api.example.com/v1")
    assert (target.scheme, target.host, target.port, target.addresses) == ("https", "api.example.com", 443, (PUBLIC,))
    # A host on the allow-list may be reached over https.
    assert egress.validate_base_url("https://gpu.lan:8000/v1", allow=AllowList.from_entries(["gpu.lan"])).port == 8000
    # Plain http needs the opt-in AND a private CIDR entry.
    cidr = AllowList.from_entries(["10.0.0.0/8"])
    assert egress.validate_base_url("http://model.lan:11434", allow=cidr, allow_plain_http=True).addresses == ("10.0.0.9",)
    with pytest.raises(EgressBlockedError) as caught:
        egress.validate_base_url("http://model.lan:11434", allow=cidr)
    assert caught.value.reason == "scheme"
    # A host-name entry alone never permits plain http.
    with pytest.raises(EgressBlockedError) as caught:
        egress.validate_base_url("http://gpu.lan:8000/v1", allow=AllowList.from_entries(["gpu.lan"]), allow_plain_http=True)
    assert caught.value.reason == "scheme"
    # A private address not on the allow-list stays refused.
    with pytest.raises(EgressBlockedError):
        egress.validate_base_url("http://gpu.lan/", allow=AllowList.from_entries(["model.lan"]))


def test_port_allow_list_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC]})
    with pytest.raises(EgressBlockedError) as caught:
        egress.validate_url("https://api.example.com:6379/", allowed_ports={443})
    assert caught.value.reason == "port"


# ── The guarded backend is really in the connection path ─────────────────
def test_client_settings_and_backend_override() -> None:
    client = egress.pinned_client(timeout=5)
    pool = client._transport._pool  # type: ignore[attr-defined]
    assert isinstance(pool._network_backend, egress.GuardedSyncBackend)
    assert client.follow_redirects is False and client.trust_env is False
    assert pool._retries == 0 and client.timeout.connect == 5
    aclient = egress.pinned_async_client(timeout=5)
    assert isinstance(aclient._transport._pool._network_backend, egress.GuardedAsyncBackend)  # type: ignore[attr-defined]


def test_override_is_not_ignored_blocked_host_raises_although_dns_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """If httpcore stopped honouring the swapped backend, its default backend
    would open a socket (AssertionError from the fixture), not refuse."""
    _dns(monkeypatch, {"internal.example": ["10.0.0.5"]})
    with egress.pinned_client(timeout=1) as client:
        with pytest.raises(EgressBlockedError):
            client.get("https://internal.example/")


def test_install_fails_closed_when_pool_shape_changes() -> None:
    class Odd:
        _pool = object()

    with pytest.raises(RuntimeError):
        egress._install_backend(Odd(), egress.GuardedSyncBackend())  # type: ignore[arg-type]


def test_connection_is_pinned_to_validated_ip_with_real_hostname_for_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC]})
    raw = Recorder(_http(200, b"hello"))
    with egress.pinned_client(timeout=5, inner_backend=raw) as client:
        response = egress.fetch(client, "GET", "https://api.example.com/v1/models")
    assert response.status_code == 200 and response.content == b"hello"
    assert raw.connects[0]["host"] == PUBLIC and raw.connects[0]["port"] == 443
    assert raw.connects[0]["sni"] == "api.example.com"
    assert b"Host: api.example.com\r\n" in raw.connects[0]["sent"]


def test_rebinding_second_lookup_private_is_refused_at_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _dns(monkeypatch, {"rebind.example": [[PUBLIC], ["10.0.0.5"]]})
    raw = Recorder(_http())
    with egress.pinned_client(timeout=5, inner_backend=raw) as client:
        with pytest.raises(EgressBlockedError):
            egress.fetch(client, "GET", "https://rebind.example/")
    assert calls["rebind.example"] == 2 and raw.connects == []


def test_unix_sockets_are_refused() -> None:
    with pytest.raises(EgressBlockedError):
        egress.GuardedSyncBackend().connect_unix_socket("/var/run/docker.sock")
    with pytest.raises(EgressBlockedError):
        asyncio.run(egress.GuardedAsyncBackend().connect_unix_socket("/var/run/docker.sock"))


def test_octal_host_reaching_backend_is_validated_after_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"0177.0.0.1": ["127.0.0.1"]})
    with pytest.raises(EgressBlockedError):
        egress.GuardedSyncBackend(inner=Recorder()).connect_tcp("0177.0.0.1", 80)


# ── Redirects and size caps ────────────────────────────────────────────────
def test_plain_client_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC]})
    raw = Recorder(_http(302, b"", {"Location": "http://10.0.0.7/"}))
    with egress.pinned_client(timeout=5, inner_backend=raw) as client:
        assert client.get("https://api.example.com/").status_code == 302
    assert len(raw.connects) == 1


def test_redirect_to_private_ip_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC], "10.0.0.7": ["10.0.0.7"]})
    raw = Recorder(_http(302, b"", {"Location": "https://10.0.0.7/admin"}))
    with egress.pinned_client(timeout=5, inner_backend=raw) as client:
        with pytest.raises(EgressBlockedError) as caught:
            egress.fetch(client, "GET", "https://api.example.com/")
    assert caught.value.reason == "blocked" and len(raw.connects) == 1


def test_redirect_hops_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC]})
    raw = Recorder(*[_http(302, b"", {"Location": f"/hop{i}"}) for i in range(10)])
    with egress.pinned_client(timeout=5, inner_backend=raw) as client:
        with pytest.raises(EgressBlockedError) as caught:
            egress.fetch(client, "GET", "https://api.example.com/", max_redirects=50)
    assert caught.value.reason == "redirects" and len(raw.connects) == egress.MAX_REDIRECTS + 1


def test_cross_origin_redirect_drops_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC], "cdn.example.net": [PUBLIC_2]})
    raw = Recorder(_http(307, b"", {"Location": "https://cdn.example.net/x"}), _http(200, b"done"))
    with egress.pinned_client(timeout=5, inner_backend=raw, headers={"Authorization": "Bearer t0k"}) as client:
        response = egress.fetch(client, "GET", "https://api.example.com/")
    assert response.content == b"done"
    assert b"Bearer t0k" in raw.connects[0]["sent"]
    assert b"authorization" not in raw.connects[1]["sent"].lower()
    assert raw.connects[1]["host"] == PUBLIC_2 and raw.connects[1]["sni"] == "cdn.example.net"


def test_response_size_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC]})
    big = b"x" * 5000
    with egress.pinned_client(timeout=5, inner_backend=Recorder(_http(200, big))) as client:
        with pytest.raises(EgressBlockedError) as caught:
            egress.fetch(client, "GET", "https://api.example.com/", max_bytes=1000)
    assert caught.value.reason == "too_large"
    with egress.pinned_client(timeout=5, inner_backend=Recorder(_http(200, big))) as client:
        assert len(egress.fetch(client, "GET", "https://api.example.com/", max_bytes=1000, truncate=True).content) == 1000


def test_allow_listed_private_cidr_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"model.lan": ["10.0.0.9"]})
    allow = AllowList.from_entries(["10.0.0.0/8"])
    raw = Recorder(_http(200, b'{"data":[]}'))
    with egress.pinned_client(timeout=5, allow=allow, inner_backend=raw) as client:
        response = egress.fetch(client, "GET", "https://model.lan:11434/v1/models", allow=allow)
    assert response.json() == {"data": []}
    assert (raw.connects[0]["host"], raw.connects[0]["port"]) == ("10.0.0.9", 11434)


def test_metadata_stays_blocked_when_allow_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"metadata.lan": ["169.254.169.254"]})
    allow = AllowList.from_entries(["169.254.0.0/16", "metadata.lan"])
    raw = Recorder(_http())
    with egress.pinned_client(timeout=5, allow=allow, inner_backend=raw) as client:
        with pytest.raises(EgressBlockedError):
            client.get("http://metadata.lan/latest/meta-data")
    assert raw.connects == []


# ── Async twin ─────────────────────────────────────────────────────────────
def test_async_client_pins_and_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example.com": [PUBLIC], "internal.example": ["10.0.0.5"]})
    raw = AsyncRecorder(_http(302, b"", {"Location": "https://internal.example/"}), _http(200, b"ok"))

    async def run() -> tuple[httpx.Response, Exception | None]:
        async with egress.pinned_async_client(timeout=5, inner_backend=raw) as client:
            blocked: Exception | None = None
            try:
                await egress.afetch(client, "GET", "https://api.example.com/")
            except EgressBlockedError as exc:
                blocked = exc
            return await egress.afetch(client, "GET", "https://api.example.com/ok"), blocked

    response, blocked = asyncio.run(run())
    assert isinstance(blocked, EgressBlockedError) and blocked.reason == "blocked"
    assert response.content == b"ok"
    assert all(c["host"] == PUBLIC and c["sni"] == "api.example.com" for c in raw.connects)


def test_async_override_is_not_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"internal.example": ["10.0.0.5"]})

    async def run() -> None:
        async with egress.pinned_async_client(timeout=1) as client:
            await client.get("https://internal.example/")

    with pytest.raises(EgressBlockedError):
        asyncio.run(run())


# ── netguard now shares the policy and the pinned connection ──────────────
def test_netguard_refuses_cgnat(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"cgnat.example": ["100.64.10.10"]})
    with pytest.raises(InvalidInputError, match="not a public website"):
        netguard.check_public_url("https://cgnat.example/")


def test_netguard_safe_get_closes_the_rebinding_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"rebind.example": [[PUBLIC], [PUBLIC], ["127.0.0.1"]]})
    with pytest.raises(InvalidInputError, match="not a public website"):
        netguard.safe_get("https://rebind.example/")
