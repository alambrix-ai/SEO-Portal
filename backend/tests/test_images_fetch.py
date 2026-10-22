"""Image service hardening: picture links, vendor keys, sizes and strict mode.

Pure unit tests. Vendor calls are stubbed (``images._vendor_post``) or go to an
``httpx.MockTransport``; DNS is stubbed so no test touches the network.
"""
from __future__ import annotations

import base64
import logging
import socket
from types import SimpleNamespace

import httpx
import pytest

from app.connectors.base import registry
from app.connectors.base.credentials import Credentials
from app.core import netguard
from app.core.config import settings
from app.core.exceptions import ConflictError, ConnectorError, InvalidInputError
from app.services import images

KEY = "sk-test-SECRET-key-0123456789abcdef"
HOST = "images.vendor.example"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (64).to_bytes(4, "big") + (32).to_bytes(4, "big") + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


#: The real vendor call; only tests that route it to a MockTransport use it.
_REAL_VENDOR_POST = images._vendor_post


@pytest.fixture(autouse=True)
def _no_real_vendor_calls(monkeypatch):  # noqa: ANN001, ANN202
    """Safety net: a test that forgets to stub the vendor fails, never reaches the network."""
    monkeypatch.setattr(images, "_vendor_post", lambda *a, **k: pytest.fail("unstubbed vendor call"))


# ── Helpers ────────────────────────────────────────────────────────────────
def _bundle(monkeypatch, instances: dict[str, dict]):  # noqa: ANN001, ANN202
    from app.services import connectors as connector_service

    bundle = SimpleNamespace(
        instances={
            slug: registry.get_class(slug)(Credentials(values=values), org_id="o")
            for slug, values in instances.items()
        }
    )
    monkeypatch.setattr(connector_service, "build_connector_bundle", lambda db, org: bundle)
    return SimpleNamespace(add=lambda row: None, flush=lambda: None), SimpleNamespace(id="t")


def _resolve_to(monkeypatch, *addresses: str) -> None:  # noqa: ANN001
    def fake(host, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in addresses
        ]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)


def _transport(monkeypatch, handler) -> list[dict]:  # noqa: ANN001
    """Route images' egress download client to ``handler``; return the kwargs seen.

    The real pinned path (DNS, pinning, rebinding) is covered with a raw
    socket stand-in in test_connector_egress.py; here the handler stands in
    for the whole client so redirect, status and size rules can be driven."""
    seen: list[dict] = []

    def client(**kwargs):  # noqa: ANN003, ANN202
        seen.append(kwargs)
        return httpx.Client(
            transport=httpx.MockTransport(handler), timeout=kwargs["timeout"],
            headers=kwargs.get("headers"), follow_redirects=False, trust_env=False,
        )

    monkeypatch.setattr(images.egress, "pinned_client", client)
    return seen


def _openai_answers_with_link(monkeypatch, url: str) -> None:  # noqa: ANN001
    monkeypatch.setattr(images, "_vendor_post", lambda *a, **k: httpx.Response(200, json={"data": [{"url": url}]}))


def _allow(monkeypatch, *hosts: str) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "image_fetch_allowed_hosts", list(hosts))


def _no_download(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(images.egress, "pinned_client", lambda **k: pytest.fail("no download may be attempted"))


OPENAI = {"openai": {"apiKey": KEY, "model": "m", "imageModel": "img-o"}}


# ── Picture links: off by default ──────────────────────────────────────────
def test_links_are_not_downloaded_when_no_host_is_allowed(monkeypatch, caplog):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    _allow(monkeypatch)
    _openai_answers_with_link(monkeypatch, f"https://{HOST}/a.png")
    _no_download(monkeypatch)
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert "could not be downloaded safely" in str(caught.value)
    assert "IMAGE_FETCH_ALLOWED_HOSTS" in caplog.text


def test_the_setting_is_empty_by_default():
    assert type(settings).model_fields["image_fetch_allowed_hosts"].default_factory() == []


# ── Picture links: every check when switched on ────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/a.png",  # not https
        "https://evil.example/a.png",  # host not allowed
        f"https://{HOST}.evil.example/a.png",  # look-alike suffix
        f"https://sub.{HOST}/a.png",  # subdomains are not implied
        f"https://{HOST}:8443/a.png",  # non-default port
        f"https://user:pw@{HOST}/a.png",  # credentials in the link
        f"ftp://{HOST}/a.png",
        "https:///a.png",
        "https://" + HOST + "/" + "a" * 3000,  # too long
    ],
)
def test_links_that_fail_a_check_are_never_fetched(monkeypatch, url):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, "93.184.216.34")
    _openai_answers_with_link(monkeypatch, url)
    _no_download(monkeypatch)
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert url not in str(caught.value)


def test_a_link_that_is_not_text_is_refused(monkeypatch):  # noqa: ANN001
    _allow(monkeypatch, HOST)
    _no_download(monkeypatch)
    with pytest.raises(ConnectorError):
        images._fetch_image("OpenAI", {"href": f"https://{HOST}/a.png"})


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "fe80::1"],
)
def test_an_allowed_host_that_resolves_to_a_private_address_is_refused(monkeypatch, address):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, address)
    _openai_answers_with_link(monkeypatch, f"https://{HOST}/a.png")
    _no_download(monkeypatch)
    with pytest.raises(ConnectorError):
        images.generate_bytes(db, org, "p")


def test_one_private_address_among_public_ones_is_refused(monkeypatch):  # noqa: ANN001
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, "93.184.216.34", "10.1.2.3")
    _no_download(monkeypatch)
    with pytest.raises(ConnectorError):
        images._fetch_image("OpenAI", f"https://{HOST}/a.png")


def test_an_allowed_public_link_is_downloaded_without_redirects_or_proxies(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, "93.184.216.34")
    _openai_answers_with_link(monkeypatch, f"https://{HOST}/a.png")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=iter([PNG]))

    seen = _transport(monkeypatch, handler)
    made = images.generate_bytes(db, org, "p")
    assert made.data == PNG and made.mime == "image/png" and (made.width, made.height) == (64, 32)
    assert len(requests) == 1 and requests[0].url.host == HOST
    # The egress client: pinned, no redirects, no proxy env, TLS verified; and
    # an empty egress allow-list, so the address must be public.
    assert seen[0]["allow"] is images.egress.NO_ALLOWLIST


@pytest.mark.parametrize("target", ["https://elsewhere.example/a.png", f"https://{HOST}/b.png", "http://127.0.0.1/"])
def test_a_redirect_is_refused_even_to_the_same_host(monkeypatch, target):  # noqa: ANN001
    _allow(monkeypatch, HOST, "elsewhere.example")
    _resolve_to(monkeypatch, "93.184.216.34")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": target})

    _transport(monkeypatch, handler)
    with pytest.raises(ConnectorError):
        images._fetch_image("OpenAI", f"https://{HOST}/a.png")
    assert len(requests) == 1


def test_an_error_status_is_refused(monkeypatch):  # noqa: ANN001
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, "93.184.216.34")
    _transport(monkeypatch, lambda request: httpx.Response(403, content=b"denied"))
    with pytest.raises(ConnectorError):
        images._fetch_image("OpenAI", f"https://{HOST}/a.png")


def test_an_oversized_body_is_cut_off(monkeypatch):  # noqa: ANN001
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(settings, "image_fetch_max_bytes", 1000)
    streamed: list[int] = []

    def chunks():  # noqa: ANN202
        for _ in range(50):
            streamed.append(1)
            yield b"\x00" * 100

    _transport(monkeypatch, lambda request: httpx.Response(200, content=chunks()))
    with pytest.raises(ConnectorError) as caught:
        images._fetch_image("OpenAI", f"https://{HOST}/a.png")
    assert "too large" in str(caught.value)
    assert len(streamed) < 50  # stopped early, not read to the end


def test_a_declared_oversized_body_is_refused_before_reading(monkeypatch):  # noqa: ANN001
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(settings, "image_fetch_max_bytes", 1000)
    _transport(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"content-length": "999999"}, content=b"\x00" * 10),
    )
    with pytest.raises(ConnectorError) as caught:
        images._fetch_image("OpenAI", f"https://{HOST}/a.png")
    assert "too large" in str(caught.value)


def test_a_downloaded_body_that_is_not_a_picture_is_refused(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, "93.184.216.34")
    _openai_answers_with_link(monkeypatch, f"https://{HOST}/a.png")
    _transport(monkeypatch, lambda request: httpx.Response(200, content=iter([b"<html><script>x</script></html>"])))
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert "not a picture" in str(caught.value)


def test_a_network_failure_is_a_plain_message(monkeypatch):  # noqa: ANN001
    _allow(monkeypatch, HOST)
    _resolve_to(monkeypatch, "93.184.216.34")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom 10.0.0.9 internal detail", request=request)

    _transport(monkeypatch, handler)
    with pytest.raises(ConnectorError) as caught:
        images._fetch_image("OpenAI", f"https://{HOST}/a.png")
    assert "boom" not in str(caught.value) and "10.0.0.9" not in str(caught.value)


# ── Base64 answers ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("data, mime", [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_base64_pictures_are_typed_from_their_bytes(monkeypatch, data, mime):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    encoded = base64.b64encode(data).decode()
    monkeypatch.setattr(images, "_vendor_post", lambda *a, **k: httpx.Response(200, json={"data": [{"b64_json": encoded}]}))
    _no_download(monkeypatch)
    assert images.generate_bytes(db, org, "p").mime == mime


@pytest.mark.parametrize(
    "encoded",
    [
        base64.b64encode(b"GIF89a" + b"\x00" * 20).decode(),  # a GIF is not accepted
        base64.b64encode(b"<svg onload=alert(1)>").decode(),
        "!!!not base64!!!",
    ],
)
def test_base64_that_is_not_an_accepted_picture_is_refused(monkeypatch, encoded):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    monkeypatch.setattr(images, "_vendor_post", lambda *a, **k: httpx.Response(200, json={"data": [{"b64_json": encoded}]}))
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert "not a picture" in str(caught.value)


def test_oversized_base64_is_refused_before_decoding(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    monkeypatch.setattr(settings, "image_fetch_max_bytes", 100)
    encoded = base64.b64encode(PNG + b"\x00" * 1000).decode()
    monkeypatch.setattr(images, "_vendor_post", lambda *a, **k: httpx.Response(200, json={"data": [{"b64_json": encoded}]}))
    monkeypatch.setattr(images.base64, "b64decode", lambda *a, **k: pytest.fail("decoded an oversized body"))
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert "too large" in str(caught.value)


def test_a_reply_that_is_not_json_is_a_plain_message(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    monkeypatch.setattr(images, "_vendor_post", lambda *a, **k: httpx.Response(200, content=b"<html>oops</html>"))
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert "oops" not in str(caught.value)


def test_a_vendor_network_failure_is_a_plain_message_and_not_retried(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    calls: list[int] = []

    def post(*a, **k):  # noqa: ANN002, ANN003, ANN202
        calls.append(1)
        raise httpx.ReadTimeout(f"timed out talking to vendor with {KEY}")

    monkeypatch.setattr(images, "_vendor_post", post)
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert KEY not in str(caught.value) and "timed out" not in str(caught.value)
    assert calls == [1]


# ── Gemini: key in the header; shapes ──────────────────────────────────────
def _gemini_capture(monkeypatch) -> list[dict]:  # noqa: ANN001
    sent: list[dict] = []

    def post(url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        sent.append({"url": url, **kwargs})
        encoded = base64.b64encode(PNG).decode()
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "text/html", "data": encoded}}]}}]}
        )

    monkeypatch.setattr(images, "_vendor_post", post)
    return sent


def test_gemini_sends_its_key_in_the_header_never_the_url(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, {"google_gemini": {"apiKey": KEY, "model": "m", "imageModel": "img-g"}})
    sent = _gemini_capture(monkeypatch)
    made = images.generate_bytes(db, org, "p", provider="google_gemini")
    assert sent[0]["headers"]["x-goog-api-key"] == KEY
    assert KEY not in sent[0]["url"] and "key=" not in sent[0]["url"]
    assert "params" not in sent[0]
    # The vendor's label is ignored; the bytes decide the type.
    assert made.mime == "image/png"


@pytest.mark.parametrize("shape, ratio", [("portrait", "2:3"), ("landscape", "3:2")])
def test_gemini_gets_the_aspect_ratio_for_the_shape(monkeypatch, shape, ratio):  # noqa: ANN001
    db, org = _bundle(monkeypatch, {"google_gemini": {"apiKey": KEY, "model": "m", "imageModel": "img-g"}})
    sent = _gemini_capture(monkeypatch)
    images.generate_bytes(db, org, "p", provider="google_gemini", shape=shape)
    assert sent[0]["json"]["generationConfig"] == {"imageConfig": {"aspectRatio": ratio}}


def test_gemini_square_sends_no_image_config(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, {"google_gemini": {"apiKey": KEY, "model": "m", "imageModel": "img-g"}})
    sent = _gemini_capture(monkeypatch)
    images.generate_bytes(db, org, "p", provider="google_gemini", shape="square")
    assert "generationConfig" not in sent[0]["json"]


@pytest.mark.parametrize(
    "shape, size", [("square", "1024x1024"), ("portrait", "1024x1536"), ("landscape", "1536x1024")]
)
def test_openai_gets_the_size_for_the_shape(monkeypatch, shape, size):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    sent: list[dict] = []

    def post(url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        sent.append(kwargs["json"])
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    monkeypatch.setattr(images, "_vendor_post", post)
    images.generate_bytes(db, org, "p", shape=shape)
    assert sent[0]["size"] == size


def test_gemini_refusal_is_logged_redacted_and_not_shown(monkeypatch, caplog):  # noqa: ANN001
    db, org = _bundle(monkeypatch, {"google_gemini": {"apiKey": KEY, "model": "m", "imageModel": "img-g"}})
    monkeypatch.setattr(
        images,
        "_vendor_post",
        lambda *a, **k: httpx.Response(403, json={"error": {"message": f"API key {KEY} owned by a@b.com denied"}}),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p", provider="google_gemini")
    assert "denied" not in str(caught.value) and KEY not in str(caught.value)
    assert "denied" in caplog.text
    assert KEY not in caplog.text and "a@b.com" not in caplog.text


# ── Strict mode: exactly what was chosen ───────────────────────────────────
BOTH = {
    "openai": {"apiKey": KEY, "model": "m", "imageModel": "img-o"},
    "google_gemini": {"apiKey": "g-key-0123456789", "model": "m", "imageModel": "img-g"},
}


def _record_calls(monkeypatch) -> list[tuple[str, str]]:  # noqa: ANN001
    used: list[tuple[str, str]] = []
    monkeypatch.setattr(
        images, "_openai", lambda key, model, prompt, size: used.append(("openai", model)) or (PNG, "image/png", 1, 1)
    )
    monkeypatch.setattr(
        images,
        "_gemini",
        lambda key, model, prompt, ratio="": used.append(("google_gemini", model)) or (PNG, "image/png", 1, 1),
    )
    return used


def test_strict_never_falls_back_to_another_connected_service(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, {"openai": BOTH["openai"]})
    used = _record_calls(monkeypatch)
    with pytest.raises(ConflictError):
        images.generate_bytes(db, org, "p", provider="google_gemini", strict=True)
    assert used == []
    # Outside strict mode the same call quietly uses the connected one.
    images.generate_bytes(db, org, "p", provider="google_gemini")
    assert used == [("openai", "img-o")]


@pytest.mark.parametrize("provider", ["", "anthropic", "disconnected"])
def test_strict_needs_a_named_image_service(monkeypatch, provider):  # noqa: ANN001
    db, org = _bundle(monkeypatch, BOTH)
    used = _record_calls(monkeypatch)
    with pytest.raises(InvalidInputError):
        images.generate_bytes(db, org, "p", provider=provider, strict=True)
    assert used == []


def test_strict_refuses_an_unknown_shape_instead_of_drawing_square(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, BOTH)
    used = _record_calls(monkeypatch)
    with pytest.raises(InvalidInputError):
        images.generate_bytes(db, org, "p", provider="openai", shape="banner", strict=True)
    assert used == []


def test_strict_uses_the_override_model_exactly(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, BOTH)
    used = _record_calls(monkeypatch)
    made = images.generate_bytes(
        db, org, "p", provider="google_gemini", model_override="  img-picked  ", strict=True, shape="portrait"
    )
    assert used == [("google_gemini", "img-picked")]
    assert made.provider == "google_gemini" and made.model == "img-picked"


def test_strict_without_an_image_model_is_a_plain_reason_not_a_fallback(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, {"openai": {"apiKey": KEY, "model": "m"}, "google_gemini": BOTH["google_gemini"]})
    used = _record_calls(monkeypatch)
    with pytest.raises(images.ImageModelMissing):
        images.generate_bytes(db, org, "p", provider="openai", strict=True)
    assert used == []


@pytest.mark.parametrize("override", ["", "   ", "../secrets", "a/b", "m?key=x", "m#x", "m b", "x" * 200, "..", "m..n"])
def test_an_unsafe_override_is_refused_before_any_call(monkeypatch, override):  # noqa: ANN001
    db, org = _bundle(monkeypatch, BOTH)
    used = _record_calls(monkeypatch)
    with pytest.raises(InvalidInputError) as caught:
        images.generate_bytes(db, org, "p", provider="google_gemini", model_override=override, strict=True)
    assert used == []
    assert override.strip() == "" or override not in str(caught.value)


def test_a_saved_model_with_path_characters_is_refused_too(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, {"google_gemini": {"apiKey": KEY, "model": "m", "imageModel": "../../v1/x"}})
    monkeypatch.setattr(images, "_vendor_post", lambda *a, **k: pytest.fail("no call with an unsafe model"))
    with pytest.raises(InvalidInputError):
        images.generate_bytes(db, org, "p", provider="google_gemini")


def test_generate_stores_what_generate_bytes_made(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, BOTH)
    _record_calls(monkeypatch)
    added: list[object] = []
    db.add = added.append
    image = images.generate(db, org, "p", provider="openai", model_override="img-x", strict=True)
    assert added == [image]
    assert image.provider == "openai" and image.data == PNG and image.tenant_id == "t"


# ── Vendor replies are streamed and capped ─────────────────────────────────
def _vendor_transport(monkeypatch, handler) -> None:  # noqa: ANN001
    """Send the real ``_vendor_post`` (its streaming and cap) to ``handler``."""
    real = httpx.Client
    monkeypatch.setattr(images.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(images, "_vendor_post", _REAL_VENDOR_POST)


def test_a_vendor_reply_within_the_cap_is_read_in_full(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    _vendor_transport(monkeypatch, handler)
    made = images.generate_bytes(db, org, "p")
    assert made.data == PNG and made.mime == "image/png"
    assert seen[0].headers["accept-encoding"] == "identity"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


def test_an_oversized_streamed_reply_stops_reading_at_the_cap(monkeypatch, caplog):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    monkeypatch.setattr(settings, "image_fetch_max_bytes", 300)
    monkeypatch.setattr(images, "REPLY_OVERHEAD_BYTES", 100)  # cap: 300 * 4 // 3 + 100 = 500 bytes
    sent: list[int] = []

    def body():  # noqa: ANN202
        for _ in range(1000):  # 1000 x 100 bytes, no declared length
            sent.append(1)
            yield b"x" * 100

    _vendor_transport(monkeypatch, lambda request: httpx.Response(200, content=body()))
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert "too large" in str(caught.value)
    assert len(sent) <= 7  # stopped just past 500 bytes, never read the whole 100 kB
    assert KEY not in caplog.text


def test_a_declared_oversized_reply_is_refused_before_reading(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    monkeypatch.setattr(settings, "image_fetch_max_bytes", 300)
    monkeypatch.setattr(images, "REPLY_OVERHEAD_BYTES", 100)
    big = b"{" + b" " * 5000 + b"}"
    _vendor_transport(monkeypatch, lambda request: httpx.Response(200, content=big))
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert "too large" in str(caught.value)


def test_a_vendor_refusal_still_reaches_the_plain_message_through_the_stream(monkeypatch):  # noqa: ANN001
    db, org = _bundle(monkeypatch, OPENAI)
    _vendor_transport(monkeypatch, lambda request: httpx.Response(429, json={"error": {"message": "SLOW-DOWN"}}))
    with pytest.raises(ConnectorError) as caught:
        images.generate_bytes(db, org, "p")
    assert "busy" in str(caught.value) and "SLOW-DOWN" not in str(caught.value)
