"""Client address, forwarded scheme, rate limits, and the request id.

No database needed: the walk is a pure function, and the middleware is tested
on a bare app with a fixed peer address.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from ipaddress import ip_network

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.core import client_ip
from app.core.config import settings
from app.core.security import create_access_token

PROXIES = (ip_network("10.0.0.0/8"), ip_network("fd00::/8"))


# ── The walk ────────────────────────────────────────────────────────────────


def test_untrusted_peer_ignores_forwarded_for() -> None:
    assert client_ip.resolve_from("203.0.113.9", ["1.2.3.4"], PROXIES) == "203.0.113.9"


def test_no_trusted_proxies_means_the_peer_is_the_client() -> None:
    assert client_ip.resolve_from("10.0.0.1", ["1.2.3.4"], ()) == "10.0.0.1"


def test_spoofed_leftmost_entry_is_ignored() -> None:
    # The client wrote "6.6.6.6"; our proxy appended the address it saw.
    chain = ["6.6.6.6, 198.51.100.7, 10.0.0.5"]
    assert client_ip.resolve_from("10.0.0.2", chain, PROXIES) == "198.51.100.7"


def test_trusted_peer_is_honoured() -> None:
    assert client_ip.resolve_from("10.0.0.2", ["198.51.100.7"], PROXIES) == "198.51.100.7"


def test_repeated_headers_are_one_list_in_order() -> None:
    chain = ["6.6.6.6", "198.51.100.7", "10.0.0.5"]
    assert client_ip.resolve_from("10.0.0.2", chain, PROXIES) == "198.51.100.7"


def test_all_trusted_chain_returns_the_leftmost() -> None:
    chain = ["10.1.1.1, 10.2.2.2", "10.3.3.3"]
    assert client_ip.resolve_from("10.0.0.2", chain, PROXIES) == "10.1.1.1"


def test_unparseable_entry_stops_at_the_last_good_hop() -> None:
    assert (
        client_ip.resolve_from("10.0.0.2", ["1.2.3.4, unknown, 10.0.0.5"], PROXIES)
        == "10.0.0.5"
    )
    assert client_ip.resolve_from("10.0.0.2", ["garbage"], PROXIES) == "10.0.0.2"


def test_empty_header_falls_back_to_the_peer() -> None:
    assert client_ip.resolve_from("10.0.0.2", [" , "], PROXIES) == "10.0.0.2"


def test_bracketed_ipv6_with_port() -> None:
    chain = ["[2001:db8::1]:4711, [fd00::5]:443"]
    assert client_ip.resolve_from("10.0.0.2", chain, PROXIES) == "2001:db8::1"


def test_ipv4_with_port_and_zone_suffix() -> None:
    assert client_ip.parse_ip("198.51.100.7:8080") == client_ip.parse_ip("198.51.100.7")
    assert str(client_ip.parse_ip("fe80::1%eth0")) == "fe80::1"
    assert str(client_ip.parse_ip("[fe80::1%25eth0]:80")) == "fe80::1"


def test_ipv4_mapped_addresses_are_normalised() -> None:
    # A mapped peer is the same trusted proxy as its IPv4 form...
    assert (
        client_ip.resolve_from("::ffff:10.0.0.2", ["::ffff:198.51.100.7"], PROXIES)
        == "198.51.100.7"
    )
    # ...and a mapped untrusted peer cannot dodge the check by its spelling.
    assert client_ip.resolve_from("::ffff:203.0.113.9", ["1.2.3.4"], PROXIES) == "203.0.113.9"


@pytest.mark.parametrize(
    "raw",
    ["", "1.2.3.4:", "1.2.3.4:99999x", "[2001:db8::1", "[2001:db8::1]x", "1.2.3.4:٣", "x" * 200],
)
def test_malformed_entries_are_rejected(raw: str) -> None:
    assert client_ip.parse_ip(raw) is None


def test_non_ip_peer_is_returned_as_is_and_headers_ignored() -> None:
    assert client_ip.resolve_from("testclient", ["1.2.3.4"], PROXIES) == "testclient"
    assert client_ip.resolve_from(None, ["1.2.3.4"], PROXIES) == ""


def test_ipv6_clients_share_a_slash_64() -> None:
    a = client_ip.rate_limit_key("2001:db8:1:2::1")
    b = client_ip.rate_limit_key("2001:db8:1:2:ffff::9")
    c = client_ip.rate_limit_key("2001:db8:1:3::1")
    assert a == b == "2001:db8:1:2::/64"
    assert a != c
    assert client_ip.rate_limit_key("198.51.100.7") == "198.51.100.7"


# ── X-Forwarded-Proto ───────────────────────────────────────────────────────


def _request(peer: str, headers: list[tuple[str, str]], scheme: str = "http") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": scheme,
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "server": ("api.example.com", 80),
            "client": (peer, 1234),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        }
    )


@pytest.fixture
def trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.0/8", "fd00::/8"])


def test_forwarded_proto_from_untrusted_peer_is_ignored(trusted: None) -> None:
    request = _request("203.0.113.9", [("X-Forwarded-Proto", "https")])
    assert client_ip.forwarded_proto(request) == "http"


def test_forwarded_proto_uses_the_rightmost_value(trusted: None) -> None:
    assert client_ip.forwarded_proto(
        _request("10.0.0.2", [("X-Forwarded-Proto", "http, https")])
    ) == "https"
    assert client_ip.forwarded_proto(
        _request("10.0.0.2", [("X-Forwarded-Proto", "https"), ("X-Forwarded-Proto", "http")])
    ) == "http"


@pytest.mark.parametrize("value", ["javascript", "HTTPS2", "ftp"])
def test_forwarded_proto_fails_closed(trusted: None, value: str) -> None:
    assert client_ip.forwarded_proto(_request("10.0.0.2", [("X-Forwarded-Proto", value)])) == "http"


def test_resolve_reads_the_request(trusted: None) -> None:
    request = _request(
        "10.0.0.2",
        [("X-Forwarded-For", "6.6.6.6, 198.51.100.7"), ("X-Forwarded-For", "10.0.0.9")],
    )
    assert client_ip.resolve(request) == "198.51.100.7"


# ── The middleware ──────────────────────────────────────────────────────────


class _Peer:
    """Pretend every request arrives from ``host`` (TestClient can't)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.host = "198.51.100.7"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, 1234)}
        await self.app(scope, receive, send)


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1000)
    monkeypatch.setattr(settings, "auth_rate_limit_per_minute", 1000)
    monkeypatch.setattr(settings, "user_rate_limit_per_minute", 3)
    monkeypatch.setattr(settings, "llm_rate_limit_per_tenant_per_minute", 2)
    monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.0/8"])


@pytest.fixture
def limited() -> Iterator[tuple[TestClient, _Peer]]:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/api/v1/things")
    def things() -> dict:
        return {}

    @app.post("/api/v1/assistant/chat")
    def chat() -> dict:
        return {}

    @app.post("/api/v1/approvals/{approval_id}/request-changes")
    def request_changes(approval_id: str) -> dict:
        return {}

    peer = _Peer(app)
    with TestClient(peer) as client:
        yield client, peer


def _bearer(user_id: str, tenant_id: str = "org-1") -> dict[str, str]:
    token, _ = create_access_token(
        user_id=user_id, tenant_id=tenant_id, role="admin", session_id="s"
    )
    return {"Authorization": f"Bearer {token}"}


def test_per_user_limit_returns_a_friendly_429(limits: None, limited) -> None:  # noqa: ANN001
    client, peer = limited
    headers = _bearer("user-1")
    for host in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        peer.host = host  # a new address each time cannot dodge the user budget
        assert client.get("/api/v1/things", headers=headers).status_code == 200
    peer.host = "198.51.100.4"
    refused = client.get("/api/v1/things", headers=headers)
    assert refused.status_code == 429
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    body = refused.json()
    assert body["code"] == "rate_limited"
    assert "Please wait" in body["detail"]
    assert "user-1" not in refused.text

    # Someone else is unaffected.
    assert client.get("/api/v1/things", headers=_bearer("user-2")).status_code == 200


def test_per_tenant_llm_limit_is_shared_across_users(limits: None, limited) -> None:  # noqa: ANN001
    client, _ = limited
    assert client.post("/api/v1/assistant/chat", headers=_bearer("a")).status_code == 200
    assert (
        client.post("/api/v1/approvals/x/request-changes", headers=_bearer("b")).status_code
        == 200
    )
    refused = client.post("/api/v1/assistant/chat", headers=_bearer("c"))
    assert refused.status_code == 429
    assert "AI tasks" in refused.json()["detail"]
    assert refused.headers["Retry-After"]

    # Another workspace has its own budget, and non-model calls are not counted.
    assert client.post("/api/v1/assistant/chat", headers=_bearer("d", "org-2")).status_code == 200
    assert client.get("/api/v1/things", headers=_bearer("c")).status_code == 200


def test_a_refused_request_does_not_charge_other_budgets(limits: None, limited) -> None:  # noqa: ANN001
    client, _ = limited
    headers = _bearer("a")
    client.post("/api/v1/assistant/chat", headers=headers)
    client.post("/api/v1/assistant/chat", headers=headers)
    assert client.post("/api/v1/assistant/chat", headers=headers).status_code == 429
    # Two charged to the user budget, not three: one more call still fits.
    assert client.get("/api/v1/things", headers=headers).status_code == 200


def test_forged_token_gets_no_user_bucket(limits: None, limited) -> None:  # noqa: ANN001
    client, _ = limited
    forged = {"Authorization": "Bearer not.a.token"}
    for _ in range(5):
        assert client.get("/api/v1/things", headers=forged).status_code == 200


def test_spoofed_forwarded_for_cannot_dodge_the_ip_limit(
    limits: None, limited, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 2)
    client, _ = limited
    for n in range(2):
        response = client.get("/api/v1/things", headers={"X-Forwarded-For": f"1.1.1.{n}"})
        assert response.status_code == 200
    refused = client.get("/api/v1/things", headers={"X-Forwarded-For": "1.1.1.9"})
    assert refused.status_code == 429


def test_trusted_proxy_forwarded_for_is_honoured(
    limits: None, limited, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    client, peer = limited
    peer.host = "10.0.0.2"
    assert client.get("/api/v1/things", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    # A different real client behind the same proxy has its own budget.
    assert client.get("/api/v1/things", headers={"X-Forwarded-For": "1.1.1.2"}).status_code == 200
    assert client.get("/api/v1/things", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429


def test_ipv6_neighbours_share_one_budget(
    limits: None, limited, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    client, peer = limited
    peer.host = "2001:db8:1:2::1"
    assert client.get("/api/v1/things").status_code == 200
    peer.host = "2001:db8:1:2::abcd"
    assert client.get("/api/v1/things").status_code == 429


def test_llm_path_matching() -> None:
    llm = RateLimitMiddleware._is_llm_call
    assert llm("POST", "/api/v1/creative/jobs")
    assert llm("POST", "/api/v1/approvals/abc/request-changes")
    assert llm("POST", "/api/v1/assistant/chat/stream")
    assert llm("POST", "/api/v1/brand-profile/generate")
    assert llm("POST", "/api/v1/analyses")
    assert llm("POST", "/api/v1/social/schedule")
    assert not llm("GET", "/api/v1/analyses")
    assert not llm("POST", "/api/v1/analyses/abc/cancel")
    assert not llm("POST", "/api/v1/approvals/abc/approve")
    assert not llm("POST", "/other/assistant/chat")


# ── Request id and the app shell ────────────────────────────────────────────


@asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001, ANN202
    yield


def _app() -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    return app


def test_request_id_is_bound_in_sync_endpoints() -> None:
    from app.core.logging import current_request_id

    app = _app()
    seen: list[str] = []

    def probe() -> dict:
        seen.append(current_request_id())
        return {}

    app.add_api_route("/__test__/rid-sync", probe, methods=["GET"])
    with TestClient(app) as client:
        response = client.get("/__test__/rid-sync", headers={"X-Request-ID": "req-sync.1"})
    assert seen == ["req-sync.1"]
    assert response.headers["X-Request-ID"] == "req-sync.1"
    assert current_request_id() == "-"


@pytest.mark.parametrize("supplied", ["bad id with spaces", "x" * 65, "<script>", "a;b=c"])
def test_unsafe_request_ids_are_replaced(supplied: str) -> None:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/x")
    def x() -> dict:
        return {}

    with TestClient(app) as client:
        response = client.get("/x", headers={"X-Request-ID": supplied})
    echoed = response.headers["X-Request-ID"]
    assert echoed != supplied
    assert len(echoed) == 16 and echoed.isalnum()


def test_root_does_not_reveal_the_version() -> None:
    from app.main import VERSION

    with TestClient(_app()) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "version" not in response.json()
    assert VERSION not in response.text


def test_cors_exposes_the_download_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cors_origins", ["https://app.example.com"])
    with TestClient(_app()) as client:
        response = client.get("/", headers={"Origin": "https://app.example.com"})
    exposed = {h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")}
    assert {"x-next-cursor", "content-disposition", "x-request-id"} <= exposed
