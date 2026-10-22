"""Grok (xAI), and the self-hosted model servers (vLLM, Ollama).

The self-hosted servers have a customer-supplied address, so most of what is
tested here is what the code refuses: metadata, loopback and private
addresses unless an administrator allow-listed them (metadata never),
plain http to a public host, and redirects. No test touches the network:
DNS is stubbed and the raw socket layer is a recording stand-in that the
egress guard still wraps.
"""
from __future__ import annotations

import functools
import json
import socket
import typing

import httpcore
import httpx
import pytest

from app.connectors.base import registry
from app.connectors.base.connector import Capability
from app.connectors.base.credentials import Credentials
from app.connectors.base.llm_verify import tiny_completion_step
from app.core import egress
from app.core.config import settings
from app.core.egress import EgressBlockedError
from app.core.exceptions import ConnectorError, InvalidInputError
from app.llm import model_catalog
from app.llm.base import LLMError
from app.llm.from_connector import provider_from_connector, supports_agent_llm
from app.llm.providers import (
    SELF_HOSTED_REDIRECT_MESSAGE,
    GrokProvider,
    SelfHostedOpenAIProvider,
    UnreportedUsage,
    usage_reported,
)
from app.services.creative import model_choice as mc
from tests.support.verify_http import install, run_all

PUBLIC = "93.184.216.34"
SELF_HOSTED = ("vllm", "ollama")


# ── Helpers ────────────────────────────────────────────────────────────────
def _dns(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[str]]) -> None:
    """Stub DNS: each host resolves to the listed addresses."""

    def fake(host: str, port: int, *args: object, **kwargs: object) -> list[tuple]:
        key = str(host).lower()
        if key not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in answers[key]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a real socket was opened")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(settings, "self_hosted_llm_allowed_hosts", [])
    model_catalog.clear_cache()


def _raw(status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None) -> bytes:
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
        return self


class _Sockets(httpcore.NetworkBackend):
    """Raw socket stand-in: records what the guard let through."""

    def __init__(self, *replies: bytes) -> None:
        self.replies = list(replies)
        self.connects: list[dict[str, typing.Any]] = []

    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None,
                    socket_options: typing.Iterable[typing.Any] | None = None) -> httpcore.NetworkStream:
        log: dict[str, typing.Any] = {"host": host, "port": port, "sent": b""}
        self.connects.append(log)
        return _Stream(self.replies.pop(0) if self.replies else _raw(), log)


def _via(monkeypatch: pytest.MonkeyPatch, raw: _Sockets) -> None:
    """Every pinned client built from now on uses ``raw`` as its sockets."""
    monkeypatch.setattr(
        egress, "pinned_client", functools.partial(egress.pinned_client, inner_backend=raw)
    )


def _connector(slug: str, values: dict[str, str]):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values, oauth_completed=True))


def _models(*ids: str) -> bytes:
    return json.dumps({"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}).encode()


def _allow(monkeypatch: pytest.MonkeyPatch, *entries: str) -> None:
    monkeypatch.setattr(settings, "self_hosted_llm_allowed_hosts", list(entries))


# ── Catalogue ──────────────────────────────────────────────────────────────
def test_the_three_connectors_write_text_only_and_reach_the_pickers():
    for slug in ("xai_grok", *SELF_HOSTED):
        spec = registry.get_spec(slug)
        assert spec is not None and spec.category == "AI models"
        assert spec.capabilities == frozenset({Capability.COMPLETE})
        assert supports_agent_llm(slug)
        assert slug in mc.TEXT_LABELS and slug not in mc.IMAGE_LABELS
    assert mc.TEXT_LABELS["xai_grok"] == "Grok (xAI)"
    assert mc.TEXT_LABELS["vllm"] == "Your own model (vLLM)"
    assert mc.TEXT_LABELS["ollama"] == "Your own model (Ollama)"
    assert "xai" not in mc.TEXT_LABELS


def test_grok_states_retention_and_uses_xais_fixed_address():
    spec = registry.get_spec("xai_grok")
    assert spec.base_url == "https://api.x.ai/v1" and not spec.base_url_field
    assert "30 days" in spec.description and "does not train" in spec.description
    assert "Zero data retention" in spec.description and "enterprise" in spec.description
    grok = _connector("xai_grok", {"apiKey": "xai-k", "baseUrl": "https://evil.example"})
    assert grok.base_url == "https://api.x.ai/v1"


@pytest.mark.parametrize("slug", SELF_HOSTED)
def test_self_hosted_connect_screen_says_where_data_goes(slug: str):
    spec = registry.get_spec(slug)
    assert "your own" in spec.description and "your server" in spec.description
    assert "HTTPS reverse proxy" in spec.description
    fields = {f.key: f for f in spec.fields}
    assert fields["baseUrl"].required
    assert fields["apiKey"].is_secret and not fields["apiKey"].required
    assert fields["model"].model_kind == "text"
    assert "imageModel" not in fields


def test_reddit_is_hidden_from_the_catalogue_while_switched_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "reddit_enabled", False)
    assert "reddit" not in registry.connector_slugs()
    assert "reddit" not in {s.slug for s in registry.all_specs()}
    # Still registered, so a connection made earlier can be read and removed.
    assert registry.get_spec("reddit") is not None
    monkeypatch.setattr(settings, "reddit_enabled", True)
    assert "reddit" in registry.connector_slugs()
    assert "reddit" in {s.slug for s in registry.all_specs()}


# ── Grok ───────────────────────────────────────────────────────────────────
def test_grok_lists_models_then_sends_max_completion_tokens(monkeypatch: pytest.MonkeyPatch):
    key = "xai-test-secret-0123456789abcdef"
    grok = _connector("xai_grok", {"apiKey": key, "model": "grok-x"})
    rec = install(grok, [
        ("GET", r"^https://api\.x\.ai/v1/models$", {"data": [{"id": "grok-x", "created": 5}, {"id": "grok-y"}]}),
        ("POST", r"^https://api\.x\.ai/v1/chat/completions$", {
            "model": "grok-x", "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1},
        }),
    ])

    def _http(self):  # noqa: ANN001, ANN202
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, transport=httpx.MockTransport(rec))
        return self._client

    monkeypatch.setattr(GrokProvider, "_http", _http)
    results = run_all(grok)
    assert results["list_models"][1] == {"models": 2, "model": "grok-x"}
    body = rec.body("POST", r"/chat/completions$")
    assert body["max_completion_tokens"] == 16 and "max_tokens" not in body
    assert rec.find("POST", r".")[0].headers["authorization"] == f"Bearer {key}"
    assert results["tiny_completion"][1]["input_tokens"] == 7


def test_grok_never_puts_an_unsafe_model_id_in_the_path():
    grok = _connector("xai_grok", {"apiKey": "xai-k", "model": "../admin"})
    rec = install(grok, [])
    with pytest.raises(ConnectorError) as caught:
        grok.retrieve_model("../admin")
    assert getattr(caught.value, "http_status", None) == 404
    assert rec.calls == []


def test_grok_provider_root_is_xais_own():
    grok = _connector("xai_grok", {"apiKey": "xai-k", "model": "grok-x"})
    provider = provider_from_connector("xai_grok", {"xai_grok": grok})
    assert isinstance(provider, GrokProvider)
    assert provider.base_url == "https://api.x.ai"
    assert provider._payload("hi", system="", max_tokens=50)["max_completion_tokens"] == 50


# ── Model list parsing ─────────────────────────────────────────────────────
def test_self_hosted_model_list_is_parsed():
    models = model_catalog.parse_self_hosted({
        "object": "list",
        "data": [
            {"id": "llama3:8b", "object": "model", "created": 1_700_000_000, "owned_by": "library"},
            {"id": "org/served-model", "object": "model", "max_model_len": 8192},
        ],
    })
    assert [m.id for m in models] == ["llama3:8b", "org/served-model"]
    assert all(m.kinds == (model_catalog.TEXT,) for m in models)
    assert models[0].created == 1_700_000_000 and models[1].created is None


@pytest.mark.parametrize(
    "payload",
    [None, [], "x", {"data": "x"}, {"data": None}, {"models": [{"id": "a"}]}],
)
def test_self_hosted_model_list_shapes_that_are_not_a_list_give_nothing(payload: object):
    assert model_catalog.parse_self_hosted(payload) == []


def test_self_hosted_model_list_drops_junk_and_is_capped():
    data = [
        {"id": ""}, {"id": "  "}, {"id": 5}, "x", None, {"id": "a" * 500}, {"id": "bad\nid"},
        {"id": "dup"}, {"id": "dup"}, {"id": "ok", "created": True},
    ]
    models = model_catalog.parse_self_hosted({"data": data})
    assert [m.id for m in models] == ["dup", "ok"]
    assert models[1].created is None
    many = model_catalog.parse_self_hosted({"data": [{"id": f"m{i}"} for i in range(5000)]})
    assert len(many) == model_catalog.SELF_HOSTED_MAX_MODELS


# ── SSRF: refused at connect time ──────────────────────────────────────────
@pytest.mark.parametrize("slug", SELF_HOSTED)
@pytest.mark.parametrize(
    ("url", "answers"),
    [
        ("http://169.254.169.254", {"169.254.169.254": ["169.254.169.254"]}),
        ("https://metadata.example", {"metadata.example": ["169.254.169.254"]}),
        ("https://localhost:11434", {"localhost": ["127.0.0.1"]}),
        ("https://models.internal", {"models.internal": ["10.0.0.5"]}),
        ("https://models.internal", {"models.internal": ["192.168.1.20"]}),
        ("https://models.internal", {"models.internal": ["172.16.0.9"]}),
        ("https://cgnat.example", {"cgnat.example": ["100.64.0.1"]}),
        ("http://models.example.com", {"models.example.com": [PUBLIC]}),
        ("ftp://models.example.com", {"models.example.com": [PUBLIC]}),
        ("https://user:pw@models.example.com", {"models.example.com": [PUBLIC]}),
        ("https://models.example.com/v1?x=1", {"models.example.com": [PUBLIC]}),
    ],
)
def test_unsafe_server_addresses_are_refused_at_connect_time(
    monkeypatch: pytest.MonkeyPatch, slug: str, url: str, answers: dict[str, list[str]]
):
    from app.services.connectors import verify_credentials

    _dns(monkeypatch, answers)
    raw = _Sockets()
    _via(monkeypatch, raw)
    with pytest.raises(EgressBlockedError) as caught:
        verify_credentials(
            slug=slug, values={"baseUrl": url, "model": "m"}, oauth_completed=False, org_id="o1"
        )
    assert isinstance(caught.value, InvalidInputError)
    assert url not in caught.value.message
    assert raw.connects == []


@pytest.mark.parametrize("slug", SELF_HOSTED)
def test_metadata_is_refused_even_when_allow_listed(monkeypatch: pytest.MonkeyPatch, slug: str):
    _dns(monkeypatch, {"metadata.lan": ["169.254.169.254"]})
    _allow(monkeypatch, "169.254.0.0/16", "metadata.lan")
    raw = _Sockets()
    _via(monkeypatch, raw)
    connector = _connector(slug, {"baseUrl": "http://metadata.lan", "model": "m"})
    with pytest.raises(EgressBlockedError):
        connector.check_health()
    with pytest.raises(EgressBlockedError):
        connector.list_models()
    assert raw.connects == []


def test_a_private_range_works_once_allow_listed(monkeypatch: pytest.MonkeyPatch):
    _dns(monkeypatch, {"model.lan": ["10.0.0.9"]})
    raw = _Sockets(_raw(200, _models("llama3:8b")))
    _via(monkeypatch, raw)
    connector = _connector("ollama", {"baseUrl": "http://model.lan:11434/v1/", "model": "llama3:8b"})
    with pytest.raises(EgressBlockedError):
        connector.check_health()
    _allow(monkeypatch, "10.0.0.0/8")
    # Plain http also needs the administrator's opt-in.
    connector = _connector("ollama", {"baseUrl": "http://model.lan:11434/v1/", "model": "llama3:8b"})
    with pytest.raises(EgressBlockedError):
        connector.check_health()
    monkeypatch.setattr(settings, "self_hosted_llm_allow_plain_http", True, raising=False)
    connector = _connector("ollama", {"baseUrl": "http://model.lan:11434/v1/", "model": "llama3:8b"})
    report = connector.check_health()
    assert report.ok
    assert (raw.connects[0]["host"], raw.connects[0]["port"]) == ("10.0.0.9", 11434)
    sent = raw.connects[0]["sent"]
    assert sent.startswith(b"GET /v1/models ")
    # No key, no Authorization header.
    assert b"authorization" not in sent.lower()


def test_the_key_is_sent_as_a_bearer_when_set(monkeypatch: pytest.MonkeyPatch):
    _dns(monkeypatch, {"models.example.com": [PUBLIC]})
    raw = _Sockets(_raw(200, _models("served")))
    _via(monkeypatch, raw)
    connector = _connector(
        "vllm", {"baseUrl": "https://models.example.com", "apiKey": "vllm-secret-key", "model": "served"}
    )
    assert [m.id for m in connector.list_models()] == ["served"]
    assert b"Bearer vllm-secret-key" in raw.connects[0]["sent"]


@pytest.mark.parametrize("slug", SELF_HOSTED)
def test_a_redirect_from_the_model_list_is_not_followed(monkeypatch: pytest.MonkeyPatch, slug: str):
    _dns(monkeypatch, {"models.example.com": [PUBLIC], "10.0.0.7": ["10.0.0.7"]})
    raw = _Sockets(_raw(302, b"", {"Location": "http://10.0.0.7/v1/models"}))
    _via(monkeypatch, raw)
    connector = _connector(slug, {"baseUrl": "https://models.example.com", "model": "m"})
    with pytest.raises(ConnectorError) as caught:
        connector.list_models()
    assert str(caught.value) == SELF_HOSTED_REDIRECT_MESSAGE
    assert len(raw.connects) == 1


def test_an_oversized_model_list_is_refused(monkeypatch: pytest.MonkeyPatch):
    _dns(monkeypatch, {"models.example.com": [PUBLIC]})
    big = b"x" * (egress.DEFAULT_MAX_BYTES + 10)
    _via(monkeypatch, _Sockets(_raw(200, big)))
    connector = _connector("vllm", {"baseUrl": "https://models.example.com", "model": "m"})
    with pytest.raises(ConnectorError) as caught:
        connector.list_models()
    assert str(caught.value) == egress.MSG_TOO_LARGE


def test_the_model_cache_works_without_a_key(monkeypatch: pytest.MonkeyPatch):
    _dns(monkeypatch, {"models.example.com": [PUBLIC]})
    raw = _Sockets(_raw(200, _models("a", "b")))
    _via(monkeypatch, raw)
    connector = _connector("vllm", {"baseUrl": "https://models.example.com", "model": "a"})
    assert [m.id for m in connector.available_models()] == ["a", "b"]
    assert [m.id for m in connector.available_models()] == ["a", "b"]
    assert len(raw.connects) == 1


def test_a_saved_model_missing_from_the_server_fails(monkeypatch: pytest.MonkeyPatch):
    _dns(monkeypatch, {"models.example.com": [PUBLIC]})
    _via(monkeypatch, _Sockets(_raw(200, _models("other"))))
    connector = _connector("vllm", {"baseUrl": "https://models.example.com", "model": "gone"})
    report = connector.check_health()
    assert not report.ok and "no longer offered" in report.detail


# ── The provider agents write with ─────────────────────────────────────────
def _completion(usage: object = None, content: str = '{"ok": true}') -> bytes:
    body: dict[str, object] = {
        "model": "served", "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    }
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body).encode()


def _provider(monkeypatch: pytest.MonkeyPatch, raw: _Sockets, *, key: str = "") -> SelfHostedOpenAIProvider:
    _dns(monkeypatch, {"models.example.com": [PUBLIC]})
    _via(monkeypatch, raw)
    values = {"baseUrl": "https://models.example.com/v1", "model": "served", "maxTokens": "300"}
    if key:
        values["apiKey"] = key
    connector = _connector("vllm", values)
    provider = provider_from_connector("vllm", {"vllm": connector})
    assert isinstance(provider, SelfHostedOpenAIProvider)
    return provider


def _sent_json(raw: _Sockets, n: int = 0) -> dict:
    sent = raw.connects[n]["sent"]
    return json.loads(sent.split(b"\r\n\r\n", 1)[1])


def test_a_missing_usage_does_not_crash_and_is_unknown_not_zero(monkeypatch: pytest.MonkeyPatch):
    raw = _Sockets(_raw(200, _completion()))
    provider = _provider(monkeypatch, raw)
    reply = provider.complete("hello")
    assert reply.text == '{"ok": true}'
    assert isinstance(reply.usage, UnreportedUsage) and not usage_reported(reply.usage)
    sent = raw.connects[0]["sent"]
    assert sent.startswith(b"POST /v1/chat/completions ")
    assert b"authorization" not in sent.lower()
    body = _sent_json(raw)
    assert body["max_tokens"] == 300 and "max_completion_tokens" not in body


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": None}, "junk", {"prompt_tokens": -1}])
def test_any_unreadable_usage_is_unknown(monkeypatch: pytest.MonkeyPatch, usage: object):
    body = json.loads(_completion())
    body["usage"] = usage
    provider = _provider(monkeypatch, _Sockets(_raw(200, json.dumps(body).encode())))
    assert not usage_reported(provider.complete("hello").usage)


def test_a_reported_usage_is_kept(monkeypatch: pytest.MonkeyPatch):
    raw = _Sockets(_raw(200, _completion({"prompt_tokens": 12, "completion_tokens": 3})))
    reply = _provider(monkeypatch, raw, key="sh-key-123456").complete("hello")
    assert usage_reported(reply.usage)
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (12, 3)
    assert b"Bearer sh-key-123456" in raw.connects[0]["sent"]


def test_json_calls_ask_for_a_json_object(monkeypatch: pytest.MonkeyPatch):
    raw = _Sockets(_raw(200, _completion()))
    data, usage = _provider(monkeypatch, raw).complete_json("give json", schema_hint='{"ok": bool}')
    assert data == {"ok": True}
    body = _sent_json(raw)
    assert body["response_format"] == {"type": "json_object"}
    assert "max_tokens" in body and "max_completion_tokens" not in body


def test_the_provider_refuses_a_redirect(monkeypatch: pytest.MonkeyPatch):
    raw = _Sockets(_raw(307, b"", {"Location": "https://other.example/v1/chat/completions"}))
    with pytest.raises(LLMError) as caught:
        _provider(monkeypatch, raw).complete("hello")
    assert str(caught.value) == SELF_HOSTED_REDIRECT_MESSAGE
    assert len(raw.connects) == 1


@pytest.mark.parametrize(
    ("url", "answers"),
    [
        ("https://metadata.example", {"metadata.example": ["169.254.169.254"]}),
        ("https://models.internal", {"models.internal": ["10.1.2.3"]}),
    ],
)
def test_the_provider_refuses_an_unsafe_address(
    monkeypatch: pytest.MonkeyPatch, url: str, answers: dict[str, list[str]]
):
    _dns(monkeypatch, answers)
    connector = _connector("ollama", {"baseUrl": url, "model": "m"})
    with pytest.raises(LLMError) as caught:
        provider_from_connector("ollama", {"ollama": connector})
    assert url not in str(caught.value) and "private or internal" in str(caught.value)


def test_the_provider_needs_a_server_address():
    connector = _connector("vllm", {"model": "m"})
    with pytest.raises(LLMError, match="no server address"):
        provider_from_connector("vllm", {"vllm": connector})


# ── The shared live check ──────────────────────────────────────────────────
def test_the_tiny_check_records_unknown_tokens_as_none(monkeypatch: pytest.MonkeyPatch):
    raw = _Sockets(_raw(200, _completion(content="OK")))
    _dns(monkeypatch, {"models.example.com": [PUBLIC]})
    _via(monkeypatch, raw)
    connector = _connector("vllm", {"baseUrl": "https://models.example.com", "model": "served"})
    detail, evidence = tiny_completion_step(connector).run()
    assert evidence["input_tokens"] is None and evidence["output_tokens"] is None
    assert "did not report token counts" in detail
    assert _sent_json(raw)["max_tokens"] == 16


def test_a_failed_tiny_check_shows_a_fixed_message_not_the_providers(monkeypatch: pytest.MonkeyPatch):
    grok = _connector("xai_grok", {"apiKey": "xai-k", "model": "grok-x"})

    def boom(self, prompt: str, **kwargs: object):  # noqa: ANN001, ANN202
        raise LLMError("vendor said: key xai-LEAKEDSECRET0123456789 is bad for jane@example.com")

    monkeypatch.setattr(GrokProvider, "complete", boom)
    with pytest.raises(ConnectorError) as caught:
        tiny_completion_step(grok).run()
    message = str(caught.value)
    assert "LEAKED" not in message and "example.com" not in message
    assert message == "Grok (xAI) did not answer a one-line test prompt. Check the key and the chosen model, then try again."


def test_a_blocked_tiny_check_names_the_filter_without_vendor_text(monkeypatch: pytest.MonkeyPatch):
    grok = _connector("xai_grok", {"apiKey": "xai-k", "model": "grok-x"})

    def boom(self, prompt: str, **kwargs: object):  # noqa: ANN001, ANN202
        raise LLMError("Grok filtered the request; vendor ref xai-LEAKEDSECRET0123456789")

    monkeypatch.setattr(GrokProvider, "complete", boom)
    with pytest.raises(ConnectorError) as caught:
        tiny_completion_step(grok).run()
    assert "safety filter" in str(caught.value) and "LEAKED" not in str(caught.value)
