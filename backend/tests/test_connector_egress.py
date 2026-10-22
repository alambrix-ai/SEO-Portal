"""Customer-typed connector addresses and picture links go through the egress guard.

A connector whose base URL the customer types (custom API, Zapier, Magento,
WordPress, DSP, Salesforce, Site Crawler) must not be steerable at cloud
metadata, loopback or a private network, directly, by a redirect, or by a
DNS name that resolves (or later re-resolves) to one. Vendor-fixed
addresses keep the plain client.

No test touches the network. DNS is stubbed (``socket.getaddrinfo``) and the
raw socket layer is replaced through the test-only hooks
(``BaseConnector._egress_inner_backend``, ``images._fetch_inner_backend``),
which sit UNDER the guard, so the real httpx -> httpcore -> guarded backend
path runs.
"""
from __future__ import annotations

import json
import socket
import typing
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from app.connectors.base import registry
from app.connectors.base.connector import MAX_GUARDED_RESPONSE_BYTES, PLAIN_HTTP_CONNECTORS
from app.connectors.base.credentials import Credentials
from app.core import egress
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.services import images

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"
GUARDED = ("custom_api", "zapier_webhooks", "magento", "wordpress", "dsp_exchange", "salesforce", "site_crawler")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (64).to_bytes(4, "big") + (32).to_bytes(4, "big") + b"\x00" * 16


# ── Helpers ────────────────────────────────────────────────────────────────
def _dns(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[list[str]] | list[str]]) -> dict[str, int]:
    """Stub DNS. A host maps to addresses, or to successive answers (the last repeats)."""
    calls: dict[str, int] = {}

    def fake(host: str, port: int, *args: object, **kwargs: object) -> list[tuple]:
        key = str(host).lower()
        if key not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        answer = answers[key]
        n = calls.get(key, 0)
        calls[key] = n + 1
        addresses = answer[min(n, len(answer) - 1)] if answer and isinstance(answer[0], list) else answer
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


def _http(status: int = 200, body: bytes = b"{}", headers: dict[str, str] | None = None) -> bytes:
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

    def start_tls(self, ssl_context: typing.Any, server_hostname: str | None = None,  # noqa: ANN401
                  timeout: float | None = None) -> httpcore.NetworkStream:
        self._log["sni"] = server_hostname
        return self


class Raw(httpcore.NetworkBackend):
    """Raw socket stand-in under the guard: records where it was asked to connect."""

    def __init__(self, *replies: bytes) -> None:
        self.replies = list(replies)
        self.connects: list[dict[str, typing.Any]] = []

    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None,
                    socket_options: typing.Iterable[typing.Any] | None = None) -> httpcore.NetworkStream:
        log: dict[str, typing.Any] = {"host": host, "port": port, "sni": None, "sent": b""}
        self.connects.append(log)
        return _Stream(self.replies.pop(0) if self.replies else _http(), log)


def _connector(slug: str, raw: Raw, **values: str) -> typing.Any:  # noqa: ANN401
    connector = registry.get_class(slug)(Credentials(values=values), org_id="o")
    connector._egress_inner_backend = raw
    return connector


def _assert_friendly(exc: BaseException, *hidden: str) -> None:
    text = str(exc)
    assert text and "Traceback" not in text
    for value in hidden:
        assert value not in text


# ── Which connectors are guarded ───────────────────────────────────────────
@pytest.mark.parametrize("slug", GUARDED)
def test_every_customer_address_connector_is_guarded(slug: str) -> None:
    klass = registry.get_class(slug)
    assert klass is not None and klass.spec.base_url_field
    assert klass(Credentials(values={}), org_id="o").egress_guarded is True


@pytest.mark.parametrize("slug", ["openai", "slack", "hubspot", "search_console"])
def test_vendor_fixed_addresses_keep_the_plain_client(slug: str) -> None:
    connector = registry.get_class(slug)(Credentials(values={}), org_id="o")
    assert connector.egress_guarded is False
    client = connector._http()
    assert not isinstance(client._transport._pool._network_backend, egress.GuardedSyncBackend)
    assert connector._pinned_client is None


def test_a_guarded_connector_builds_a_pinned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector("magento", Raw(), baseUrl="https://store.example", accessToken="t" * 12)
    client = connector._http()
    assert isinstance(client._transport._pool._network_backend, egress.GuardedSyncBackend)
    assert client.follow_redirects is False and client._trust_env is False
    assert str(client.base_url).rstrip("/") == "https://store.example"


def test_only_the_site_crawler_may_read_plain_http() -> None:
    assert PLAIN_HTTP_CONNECTORS == frozenset({"site_crawler"})


# ── Refusals ───────────────────────────────────────────────────────────────
def test_cloud_metadata_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"169.254.169.254": ["169.254.169.254"]})
    raw = Raw()
    connector = _connector("custom_api", raw, webhookUrl="http://169.254.169.254", secret="s" * 16)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/latest/meta-data/")
    assert caught.value.details["reason"] == "blocked"
    assert "private or internal network" in caught.value.message
    _assert_friendly(caught.value, "169.254", "s" * 16)
    assert raw.connects == []


def test_cloud_metadata_is_refused_even_for_the_crawler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain http is allowed for the crawler; private addresses never are."""
    _dns(monkeypatch, {"169.254.169.254": ["169.254.169.254"]})
    raw = Raw()
    connector = _connector("site_crawler", raw, siteUrl="http://169.254.169.254", discovery="crawl")
    with pytest.raises(ConnectorConfigError):
        connector.request("GET", "/", expect_json=False)
    assert raw.connects == []


@pytest.mark.parametrize("slug, field", [("wordpress", "siteUrl"), ("site_crawler", "siteUrl"), ("zapier_webhooks", "webhookUrl")])
def test_localhost_is_refused(monkeypatch: pytest.MonkeyPatch, slug: str, field: str) -> None:
    _dns(monkeypatch, {"localhost": ["127.0.0.1", "::1"]})
    raw = Raw()
    connector = _connector(slug, raw, **{field: "http://localhost"}, username="u", applicationPassword="p" * 12)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/", expect_json=False)
    assert caught.value.details["reason"] == "blocked"
    _assert_friendly(caught.value, "127.0.0.1", "localhost")
    assert raw.connects == []


@pytest.mark.parametrize(
    "slug, values",
    [
        ("magento", {"baseUrl": "https://shop.internal.example", "accessToken": "t" * 12}),
        ("dsp_exchange", {"apiHost": "https://shop.internal.example", "apiKey": "k" * 12, "seatId": "1"}),
        ("salesforce", {"instanceUrl": "https://shop.internal.example", "accessToken": "t" * 12}),
        ("custom_api", {"webhookUrl": "https://shop.internal.example", "secret": "s" * 16}),
    ],
)
def test_a_name_resolving_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, slug: str, values: dict[str, str]
) -> None:
    _dns(monkeypatch, {"shop.internal.example": ["10.1.2.3"]})
    raw = Raw()
    connector = _connector(slug, raw, **values)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/anything")
    assert caught.value.details["reason"] == "blocked"
    _assert_friendly(caught.value, "10.1.2.3", "shop.internal.example")
    assert raw.connects == []


def test_one_private_answer_among_public_ones_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"mixed.example": [PUBLIC, "192.168.0.10"]})
    raw = Raw()
    connector = _connector("wordpress", raw, siteUrl="https://mixed.example", username="u", applicationPassword="p" * 12)
    with pytest.raises(ConnectorConfigError):
        connector.request("GET", "/wp-json")
    assert raw.connects == []


def test_a_redirect_to_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"store.example": [PUBLIC], "10.0.0.5": ["10.0.0.5"]})
    raw = Raw(_http(302, b"", {"Location": "https://10.0.0.5/rest/V1/store/storeViews"}))
    connector = _connector("magento", raw, baseUrl="https://store.example", accessToken="t" * 12)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/rest/V1/store/storeViews")
    assert caught.value.details["reason"] == "blocked"
    _assert_friendly(caught.value, "10.0.0.5")
    assert len(raw.connects) == 1 and raw.connects[0]["host"] == PUBLIC


def test_a_redirect_to_a_name_that_resolves_privately_is_refused_for_the_crawler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dns(monkeypatch, {"site.example": [PUBLIC], "intranet.example": ["172.16.4.4"]})
    raw = Raw(_http(301, b"", {"Location": "http://intranet.example/"}))
    connector = _connector("site_crawler", raw, siteUrl="https://site.example", discovery="crawl")
    with pytest.raises(ConnectorConfigError):
        connector.request("GET", "/", expect_json=False)
    assert len(raw.connects) == 1


def test_dns_rebinding_between_check_and_connect_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _dns(monkeypatch, {"rebind.example": [[PUBLIC], ["10.0.0.9"]]})
    raw = Raw()
    connector = _connector("custom_api", raw, webhookUrl="https://rebind.example", secret="s" * 16)
    with pytest.raises(ConnectorConfigError):
        connector.request("GET", "/health")
    assert calls["rebind.example"] == 2 and raw.connects == []


def test_plain_http_is_refused_for_credentialed_connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"store.example": [PUBLIC]})
    raw = Raw()
    connector = _connector("magento", raw, baseUrl="http://store.example", accessToken="t" * 12)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/rest/V1/store/storeViews")
    assert caught.value.details["reason"] == "scheme"
    assert "https://" in caught.value.message
    assert raw.connects == []


def test_a_redirect_from_https_to_plain_http_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"blog.example": [PUBLIC]})
    raw = Raw(_http(302, b"", {"Location": "http://blog.example/wp-json"}))
    connector = _connector("wordpress", raw, siteUrl="https://blog.example", username="u", applicationPassword="p" * 12)
    with pytest.raises(ConnectorConfigError):
        connector.request("GET", "/wp-json")
    assert len(raw.connects) == 1


def test_an_oversized_reply_is_refused_as_one_item_not_a_misconfiguration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dns(monkeypatch, {"site.example": [PUBLIC]})
    big = b"x" * (MAX_GUARDED_RESPONSE_BYTES + 1)
    raw = Raw(_http(200, big))
    connector = _connector("site_crawler", raw, siteUrl="https://site.example", discovery="crawl")
    with pytest.raises(ConnectorError) as caught:
        connector.request("GET", "/huge", expect_json=False)
    assert not isinstance(caught.value, ConnectorConfigError)
    assert caught.value.details["reason"] == "too_large"


def test_a_swapped_in_plain_client_cannot_bypass_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the pinned client (or a socket-less MockTransport stub) is used."""
    connector = _connector("magento", Raw(), baseUrl="https://store.example", accessToken="t" * 12)
    connector._client = httpx.Client(base_url="https://store.example")
    with pytest.raises(RuntimeError):
        connector.request("GET", "/rest/V1/store/storeViews")


def test_no_registered_connector_overrides_the_guarded_send_path() -> None:
    """A customer-address connector inherits the guarded path. The one
    exception is the self-hosted model family, which brings its own egress
    client (pinned, its own allow-list, redirects refused) — checked here."""
    from app.connectors.base.connector import BaseConnector
    from app.connectors.base.llm_models import SelfHostedModelConnector

    for slug in registry.connector_slugs():
        klass = registry.get_class(slug)
        if klass is None or not klass.spec.base_url_field:
            continue
        if issubclass(klass, SelfHostedModelConnector):
            assert klass._http is SelfHostedModelConnector._http, f"{slug} overrides _http"
            assert klass._send is SelfHostedModelConnector._send, f"{slug} overrides _send"
            continue
        for name in ("_http", "_send", "_guarded_send", "request", "egress_guarded"):
            assert getattr(klass, name) is getattr(BaseConnector, name), f"{slug} overrides {name}"


def test_a_cassette_recording_does_not_exempt_a_plain_client() -> None:
    """The recorder calls the real send, so a plain client stays refused."""
    from app.connectors.base.cassettes import Recorder

    connector = _connector("magento", Raw(), baseUrl="https://store.example", accessToken="t" * 12)
    connector._client = httpx.Client(base_url="https://store.example")
    with Recorder().active(), pytest.raises(RuntimeError):
        connector.request("GET", "/rest/V1/store/storeViews")


def test_a_cassette_replay_answers_without_the_network() -> None:
    from app.connectors.base import cassettes
    from app.connectors.base.connector import _cassette_replay_active

    assert _cassette_replay_active() is False
    player = cassettes.Player(cassettes.Cassette.__new__(cassettes.Cassette))
    with player.active():
        assert _cassette_replay_active() is True
    assert _cassette_replay_active() is False


# ── Public addresses still work ────────────────────────────────────────────
def test_public_https_works_pinned_with_the_real_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"store.example": [PUBLIC]})
    views = [{"id": 1, "code": "default"}]
    raw = Raw(_http(200, json.dumps(views).encode(), {"Content-Type": "application/json"}))
    connector = _connector("magento", raw, baseUrl="https://store.example", accessToken="t" * 12)
    assert connector.request("GET", "/rest/V1/store/storeViews") == views
    assert raw.connects[0]["host"] == PUBLIC and raw.connects[0]["port"] == 443
    assert raw.connects[0]["sni"] == "store.example"
    sent = raw.connects[0]["sent"]
    assert b"GET /rest/V1/store/storeViews HTTP/1.1" in sent and b"Host: store.example\r\n" in sent
    assert b"authorization: bearer " + b"t" * 12 in sent.lower()


def test_a_signed_post_body_reaches_the_wire_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"api.example": [PUBLIC]})
    raw = Raw(_http(200, b'{"ok": true}'))
    connector = _connector("custom_api", raw, webhookUrl="https://api.example/hook", secret="s" * 16)
    body = b'{"a":1}'
    connector.request("POST", "/hook/pages/1", content=body, headers={"Content-Type": "application/json"})
    sent = raw.connects[0]["sent"]
    assert sent.endswith(b"\r\n\r\n" + body) and b"Content-Length: 7\r\n" in sent


def test_a_same_site_redirect_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"blog.example": [PUBLIC]})
    raw = Raw(
        _http(301, b"", {"Location": "https://blog.example/wp-json/"}),
        _http(200, b'{"name": "Blog"}'),
    )
    connector = _connector("wordpress", raw, siteUrl="https://blog.example", username="u", applicationPassword="p" * 12)
    assert connector.request("GET", "/wp-json") == {"name": "Blog"}
    assert [c["host"] for c in raw.connects] == [PUBLIC, PUBLIC]


def test_a_credentialed_connector_refuses_a_redirect_to_another_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a public www hop: the connection's keys would go with it."""
    _dns(monkeypatch, {"blog.example": [PUBLIC], "www.blog.example": [PUBLIC_2]})
    raw = Raw(
        _http(301, b"", {"Location": "https://www.blog.example/wp-json"}),
        _http(200, b'{"name": "Blog"}'),
    )
    connector = _connector("wordpress", raw, siteUrl="https://blog.example", username="u", applicationPassword="p" * 12)
    with pytest.raises(ConnectorError) as caught:
        connector.request("GET", "/wp-json")
    assert caught.value.details["reason"] == "redirects"
    assert [c["host"] for c in raw.connects] == [PUBLIC]


def test_the_crawler_still_reads_a_public_plain_http_site(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"old-site.example": [PUBLIC]})
    raw = Raw(_http(200, b"<html><title>Hi</title></html>"))
    connector = _connector("site_crawler", raw, siteUrl="http://old-site.example", discovery="crawl")
    assert "Hi" in connector.request("GET", "/", expect_json=False)
    assert raw.connects[0]["port"] == 80 and raw.connects[0]["sni"] is None


def test_a_mock_transport_stub_still_works_for_tests() -> None:
    """Existing tests inject an httpx.MockTransport client; it opens no socket."""
    connector = registry.get_class("magento")(
        Credentials(values={"baseUrl": "https://store.example", "accessToken": "t" * 12}), org_id="o"
    )
    connector._client = httpx.Client(
        base_url="https://store.example", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1]))
    )
    assert connector.request("GET", "/rest/V1/store/storeViews") == [1]


# ── Picture links (images.py) ──────────────────────────────────────────────
IMAGE_HOST = "images.vendor.example"


@pytest.fixture
def image_raw(monkeypatch: pytest.MonkeyPatch) -> typing.Callable[..., Raw]:
    monkeypatch.setattr(settings, "image_fetch_allowed_hosts", [IMAGE_HOST])

    def install(*replies: bytes) -> Raw:
        raw = Raw(*replies)
        monkeypatch.setattr(images, "_fetch_inner_backend", raw)
        return raw

    return install


def test_an_allowed_public_picture_is_downloaded_pinned(monkeypatch: pytest.MonkeyPatch, image_raw) -> None:  # noqa: ANN001
    _dns(monkeypatch, {IMAGE_HOST: [PUBLIC]})
    raw = image_raw(_http(200, PNG))
    assert images._fetch_image("OpenAI", f"https://{IMAGE_HOST}/a.png") == PNG
    assert raw.connects[0]["host"] == PUBLIC and raw.connects[0]["sni"] == IMAGE_HOST


def test_picture_dns_rebinding_is_refused_at_connect(monkeypatch: pytest.MonkeyPatch, image_raw) -> None:  # noqa: ANN001
    """The gap 2.2 left open: checked public, then resolved privately on connect."""
    calls = _dns(monkeypatch, {IMAGE_HOST: [[PUBLIC], ["10.0.0.5"]]})
    raw = image_raw(_http(200, PNG))
    with pytest.raises(ConnectorError) as caught:
        images._fetch_image("OpenAI", f"https://{IMAGE_HOST}/a.png")
    assert "could not be downloaded safely" in str(caught.value)
    assert calls[IMAGE_HOST] == 2 and raw.connects == []


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254"])
def test_an_allowed_picture_host_on_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, image_raw, address: str  # noqa: ANN001
) -> None:
    _dns(monkeypatch, {IMAGE_HOST: [address]})
    raw = image_raw()
    with pytest.raises(ConnectorError):
        images._fetch_image("OpenAI", f"https://{IMAGE_HOST}/a.png")
    assert raw.connects == []


def test_a_picture_redirect_is_never_followed(monkeypatch: pytest.MonkeyPatch, image_raw) -> None:  # noqa: ANN001
    _dns(monkeypatch, {IMAGE_HOST: [PUBLIC]})
    raw = image_raw(_http(302, b"", {"Location": f"https://{IMAGE_HOST}/b.png"}), _http(200, PNG))
    with pytest.raises(ConnectorError) as caught:
        images._fetch_image("OpenAI", f"https://{IMAGE_HOST}/a.png")
    assert "could not be downloaded safely" in str(caught.value)
    assert len(raw.connects) == 1


def test_an_oversized_picture_is_refused(monkeypatch: pytest.MonkeyPatch, image_raw) -> None:  # noqa: ANN001
    _dns(monkeypatch, {IMAGE_HOST: [PUBLIC]})
    monkeypatch.setattr(settings, "image_fetch_max_bytes", 1000)
    image_raw(_http(200, PNG + b"\x00" * 2000))
    with pytest.raises(ConnectorError) as caught:
        images._fetch_image("OpenAI", f"https://{IMAGE_HOST}/a.png")
    assert "too large" in str(caught.value)


def test_a_picture_error_status_is_refused(monkeypatch: pytest.MonkeyPatch, image_raw) -> None:  # noqa: ANN001
    _dns(monkeypatch, {IMAGE_HOST: [PUBLIC]})
    image_raw(_http(403, b"denied"))
    with pytest.raises(ConnectorError) as caught:
        images._fetch_image("OpenAI", f"https://{IMAGE_HOST}/a.png")
    assert "denied" not in str(caught.value)


def test_a_downloaded_non_picture_is_refused_by_magic_bytes(monkeypatch: pytest.MonkeyPatch, image_raw) -> None:  # noqa: ANN001
    from app.services import connectors as connector_service

    _dns(monkeypatch, {IMAGE_HOST: [PUBLIC]})
    image_raw(_http(200, b"<html><script>x</script></html>"))
    bundle = SimpleNamespace(
        instances={"openai": registry.get_class("openai")(
            Credentials(values={"apiKey": "sk-test-0123456789abcdef", "model": "m", "imageModel": "img-o"}),
            org_id="o",
        )}
    )
    monkeypatch.setattr(connector_service, "build_connector_bundle", lambda db, org: bundle)
    monkeypatch.setattr(
        images, "_vendor_post",
        lambda *a, **k: httpx.Response(200, json={"data": [{"url": f"https://{IMAGE_HOST}/a.png"}]}),
    )
    db = SimpleNamespace(add=lambda row: None, flush=lambda: None)
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, SimpleNamespace(id="t"), "p")
    assert "not a picture" in str(caught.value)


# ── Token refresh at a customer-typed sign-in address (oauth.py) ───────────
def _salesforce(raw: Raw, login_url: str) -> typing.Any:  # noqa: ANN401
    return _connector(
        "salesforce", raw, loginUrl=login_url, refreshToken="5Aep-refresh-0123",
        clientId="sf-client-id", clientSecret="sf-client-SECRET-0123",
        instanceUrl="https://acme.my.salesforce.com",
    )


@pytest.mark.parametrize(
    "login_url, answers",
    [
        ("http://169.254.169.254", {"169.254.169.254": ["169.254.169.254"]}),
        ("https://169.254.169.254", {"169.254.169.254": ["169.254.169.254"]}),
        ("https://localhost", {"localhost": ["127.0.0.1", "::1"]}),
        ("https://login.corp.example", {"login.corp.example": ["10.20.30.40"]}),
    ],
)
def test_a_private_salesforce_login_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, login_url: str, answers: dict[str, list[str]]
) -> None:
    _dns(monkeypatch, answers)
    raw = Raw()
    connector = _salesforce(raw, login_url)
    with pytest.raises(ConnectorConfigError) as caught:
        _ = connector.access_token
    assert caught.value.details["reason"] == "blocked"
    assert "login address" in caught.value.message
    _assert_friendly(caught.value, "169.254", "127.0.0.1", "10.20.30.40", "sf-client-SECRET-0123", "5Aep-refresh-0123")
    assert raw.connects == []


def test_salesforce_token_rebinding_is_refused_at_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _dns(monkeypatch, {"login.rebind.example": [[PUBLIC], ["10.0.0.9"]]})
    raw = Raw()
    with pytest.raises(ConnectorConfigError):
        _ = _salesforce(raw, "https://login.rebind.example").access_token
    assert calls["login.rebind.example"] == 2 and raw.connects == []


def test_a_plain_http_salesforce_login_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"login.example": [PUBLIC]})
    raw = Raw()
    with pytest.raises(ConnectorConfigError) as caught:
        _ = _salesforce(raw, "http://login.example").access_token
    assert caught.value.details["reason"] == "scheme" and raw.connects == []


def test_a_salesforce_token_endpoint_may_not_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even to a public host: the client secret must not follow a redirect."""
    _dns(monkeypatch, {"login.example": [PUBLIC], "elsewhere.example": [PUBLIC_2]})
    raw = Raw(_http(307, b"", {"Location": "https://elsewhere.example/services/oauth2/token"}))
    with pytest.raises(ConnectorConfigError) as caught:
        _ = _salesforce(raw, "https://login.example").access_token
    assert caught.value.details["reason"] == "redirects"
    assert "redirect" in caught.value.message
    assert len(raw.connects) == 1


def test_an_oversized_token_reply_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"login.example": [PUBLIC]})
    raw = Raw(_http(200, b"{" + b" " * 200_000 + b"}"))
    with pytest.raises(ConnectorConfigError) as caught:
        _ = _salesforce(raw, "https://login.example").access_token
    assert caught.value.details["reason"] == "too_large"


def test_an_unreachable_salesforce_login_address_is_a_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"login.example": [PUBLIC]})

    class Down(Raw):
        def connect_tcp(self, host: str, port: int, *args: typing.Any, **kwargs: typing.Any) -> httpcore.NetworkStream:  # noqa: ANN401
            raise httpcore.ConnectError(f"refused by {host}")

    with pytest.raises(ConnectorError) as caught:
        _ = _salesforce(Down(), "https://login.example").access_token
    _assert_friendly(caught.value, PUBLIC, "refused by")
    assert "login address" in caught.value.message


def test_a_public_https_salesforce_token_endpoint_works(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"login.example": [PUBLIC]})
    reply = json.dumps({"access_token": "sf-access", "instance_url": "https://acme.my.salesforce.com"}).encode()
    raw = Raw(_http(200, reply, {"Content-Type": "application/json"}))
    connector = _salesforce(raw, "login.example")  # no scheme: https:// is assumed
    assert connector.access_token == "sf-access"
    sent = raw.connects[0]["sent"]
    assert raw.connects[0]["host"] == PUBLIC and raw.connects[0]["sni"] == "login.example"
    assert sent.startswith(b"POST /services/oauth2/token HTTP/1.1")
    assert b"grant_type=refresh_token" in sent and b"refresh_token=5Aep-refresh-0123" in sent


def test_vendor_fixed_token_endpoints_keep_the_plain_client() -> None:
    for slug in ("search_console", "hubspot", "shopify", "x_twitter"):
        connector = registry.get_class(slug)(Credentials(values={}), org_id="o")
        assert connector._token_url_guarded() is False, slug
    assert _salesforce(Raw(), "https://login.salesforce.com")._token_url_guarded() is True
