"""AI models come from the person's own connection, picked from the vendor's live list.

Pure unit tests: every vendor call goes to an httpx.MockTransport. Model ids
used here are made up on purpose ("m-old", "m-new"): nothing in the code may
depend on a real model's name.
"""
from __future__ import annotations

import io
import json
import logging
import pathlib
import re
import tokenize
from types import SimpleNamespace

import httpx
import pytest

from app.connectors.base import registry
from app.connectors.base.credentials import Credentials
from app.core.config import settings
from app.llm import model_catalog
from app.llm.base import LLMError
from app.llm.providers import AnthropicProvider, GeminiProvider, OpenAIProvider

KEY = "sk-test-SECRET-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _fresh_cache():
    model_catalog.clear_cache()
    yield
    model_catalog.clear_cache()


def _connector(slug: str, values: dict, handler):  # noqa: ANN001, ANN202
    klass = registry.get_class(slug)
    instance = klass(Credentials(values=values), org_id="o")
    instance._client = httpx.Client(base_url=instance.base_url, transport=httpx.MockTransport(handler))
    return instance


# ── Parsing each vendor's list ─────────────────────────────────────────────
def test_openai_list_is_parsed_newest_first_and_nothing_is_preselected():
    """OpenAI's list has ids and timestamps but no capability data, so the
    newest id could be a speech or image model: nothing is pre-selected."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "m-old", "object": "model", "created": 100, "owned_by": "x"},
                    {"id": "m-new", "object": "model", "created": 300, "owned_by": "x"},
                    {"id": "m-mid", "object": "model", "created": 200, "owned_by": "x"},
                ],
            },
        )

    openai = _connector("openai", {"apiKey": KEY}, handler)
    models = openai.available_models()
    assert [m.id for m in models] == ["m-new", "m-mid", "m-old"]
    assert all(set(m.kinds) == {"text", "image"} and not m.kinds_known for m in models)
    assert model_catalog.suggested_text_model(models) == ""
    # Cached: a second read does not call OpenAI again.
    openai.available_models()
    assert len(seen) == 1


def test_anthropic_list_follows_pagination_and_preselects_the_newest_by_created_at():
    pages = {
        "": {
            "data": [
                {"id": "m-new", "display_name": "New", "created_at": "2026-09-01T00:00:00Z", "lifecycle": "active"},
                {"id": "m-gone", "display_name": "Gone", "created_at": "2026-10-01T00:00:00Z", "lifecycle": "retired"},
            ],
            "has_more": True,
            "last_id": "m-gone",
        },
        "m-gone": {
            "data": [
                {"id": "m-old", "display_name": "Old", "created_at": "2025-01-01T00:00:00Z", "lifecycle": "deprecated"},
                {"id": "m-undated", "display_name": "Undated", "created_at": "1970-01-01T00:00:00Z"},
            ],
            "has_more": False,
            "last_id": "m-undated",
        },
    }
    cursors: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["x-api-key"] == KEY
        assert request.headers["anthropic-version"]
        cursor = request.url.params.get("after_id", "")
        cursors.append(cursor)
        return httpx.Response(200, json=pages[cursor])

    claude = _connector("anthropic_claude", {"apiKey": KEY}, handler)
    models = claude.available_models()
    assert cursors == ["", "m-gone"]
    # Retired models are left out; an epoch date means "unknown".
    assert [m.id for m in models] == ["m-new", "m-old", "m-undated"]
    assert next(m for m in models if m.id == "m-undated").created is None
    assert models[0].label == "New"
    assert model_catalog.suggested_text_model(models) == "m-new"


def test_gemini_list_follows_page_tokens_keeps_generate_content_models_and_preselects_nothing():
    pages = {
        "": {
            "models": [
                {"name": "models/m-a", "displayName": "A", "supportedGenerationMethods": ["generateContent", "countTokens"], "thinking": True},
                {"name": "models/m-embed", "displayName": "Embed", "supportedGenerationMethods": ["embedContent"]},
            ],
            "nextPageToken": "page-2",
        },
        "page-2": {
            "models": [
                {"name": "models/m-b", "displayName": "B", "supportedGenerationMethods": ["generateContent"]},
            ],
        },
    }
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models"
        assert request.headers["x-goog-api-key"] == KEY
        token = request.url.params.get("pageToken", "")
        tokens.append(token)
        return httpx.Response(200, json=pages[token])

    gemini = _connector("google_gemini", {"apiKey": KEY}, handler)
    models = gemini.available_models()
    assert tokens == ["", "page-2"]
    assert [m.id for m in models] == ["m-a", "m-b"]
    assert models[0].thinking is True
    # Gemini publishes no creation time, so nothing is pre-selected.
    assert model_catalog.suggested_text_model(models) == ""


def test_perplexity_has_no_list_and_says_so():
    perplexity = _connector("perplexity", {"apiKey": KEY}, lambda r: pytest.fail("no list call"))
    with pytest.raises(model_catalog.ModelListUnavailable):
        perplexity.available_models()


# ── Health: the saved model must still be offered ─────────────────────────
def _openai_list(*ids: str):  # noqa: ANN202
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": i, "created": 1} for i in ids]})

    return handler


def test_a_saved_model_no_longer_offered_fails_health_with_a_plain_message():
    openai = _connector("openai", {"apiKey": KEY, "model": "m-retired"}, _openai_list("m-new"))
    report = openai.check_health()
    assert not report.ok
    assert report.detail == "Choose a model — your saved model m-retired is no longer offered by OpenAI."


def test_a_saved_image_model_no_longer_offered_fails_health_too():
    openai = _connector("openai", {"apiKey": KEY, "model": "m-new", "imageModel": "img-gone"}, _openai_list("m-new"))
    report = openai.check_health()
    assert not report.ok and "image model img-gone is no longer offered by OpenAI" in report.detail


def test_when_listing_is_not_permitted_the_model_is_checked_on_its_own_and_only_warned():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(403, json={"error": {"message": "listing not allowed"}})
        assert request.url.path == "/v1/models/m-new"
        return httpx.Response(200, json={"id": "m-new"})

    report = _connector("openai", {"apiKey": KEY, "model": "m-new"}, handler).check_health()
    assert report.ok
    assert "could not be read" in report.detail


def test_runs_with_a_vanished_model_fail_with_the_same_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    provider = OpenAIProvider(api_key=KEY, model="m-retired", base_url="https://api.test", vendor_label="OpenAI")
    provider._client = httpx.Client(base_url=provider.base_url, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError) as caught:
        provider.complete("hi")
    assert str(caught.value) == "Choose a model — your saved model m-retired is no longer offered by OpenAI."


# ── Listing before saving never echoes the key ─────────────────────────────
def _listing_with(monkeypatch, handler, values):  # noqa: ANN001, ANN202
    """Run the service behind POST /connectors/{slug}/models with a mock vendor."""
    from app.api.v1 import connectors as routes
    from app.connectors.base import connector as base
    from app.services import connectors as service

    real_client = httpx.Client

    def client(**kwargs):  # noqa: ANN003, ANN202
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(base.httpx, "Client", client)
    record = SimpleNamespace(connected=False, credential_hints={}, credentials_encrypted="", oauth_completed=False)
    monkeypatch.setattr(service, "get_record", lambda db, tenant_id, slug: record)
    monkeypatch.setattr(service.OrgCipher, "for_org", classmethod(lambda cls, org: None))
    org = SimpleNamespace(id="o")
    listing = service.model_listing(None, org=org, slug="openai", values=values)
    return routes._model_list_out(listing).model_dump_json()


def test_listing_before_saving_returns_models_and_never_the_key(monkeypatch, caplog):  # noqa: ANN001
    body = _listing_with(monkeypatch, _openai_list("m-a", "m-b"), {"apiKey": KEY})
    assert KEY not in body
    data = json.loads(body)
    assert [m["id"] for m in data["models"]] == ["m-a", "m-b"]
    assert data["source"] == "The list comes from your OpenAI account."


def test_a_refused_key_is_a_friendly_message_and_is_neither_returned_nor_logged(monkeypatch, caplog):  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        # A vendor that echoes the key it was sent.
        return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY}"}})

    with caplog.at_level(logging.DEBUG):
        body = _listing_with(monkeypatch, handler, {"apiKey": KEY})
    data = json.loads(body)
    assert data["models"] == [] and "could not load the model list from OpenAI" in data["error"]
    assert "type a model name" in data["error"]
    assert KEY not in body
    assert KEY not in caplog.text
    assert "[redacted]" in caplog.text  # the echoed key was logged, blanked out


# ── Sampling settings: only what the person set ────────────────────────────
def _capture(provider):  # noqa: ANN001, ANN202
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={})
        body = json.loads(request.content)
        sent.append(body)
        if "contents" in body:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        if provider.name == "Anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    provider._client = httpx.Client(base_url=provider.base_url, transport=httpx.MockTransport(handler))
    return sent


def test_openai_sends_no_temperature_or_top_p_when_blank_but_always_a_token_cap(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "llm_max_tokens", 1234)
    provider = OpenAIProvider(api_key=KEY, model="m", base_url="https://api.test")
    sent = _capture(provider)
    provider.complete("hi", temperature=0.9)  # a per-call value is never sent
    assert set(sent[0]) == {"model", "messages", "max_completion_tokens"}
    assert sent[0]["max_completion_tokens"] == 1234


def test_openai_sends_what_the_person_set():
    provider = OpenAIProvider(api_key=KEY, model="m", base_url="https://api.test", temperature=0.7, default_max_tokens=500)
    sent = _capture(provider)
    provider.complete("hi")
    assert sent[0]["temperature"] == 0.7
    assert sent[0]["max_completion_tokens"] == 500
    assert "max_tokens" not in sent[0]


def test_gemini_sends_only_the_token_cap_in_generation_config_when_blank(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "llm_max_tokens", 1234)
    provider = GeminiProvider(api_key=KEY, model="m", base_url="https://api.test")
    sent = _capture(provider)
    provider.complete("hi")
    assert sent[0]["generationConfig"] == {"maxOutputTokens": 1234}


def test_perplexity_sends_no_sampling_when_blank_but_always_a_token_cap(monkeypatch):  # noqa: ANN001
    from app.llm.from_connector import PerplexityProvider

    monkeypatch.setattr(settings, "llm_max_tokens", 1234)
    provider = PerplexityProvider(api_key=KEY, model="m", base_url="https://api.test")
    sent = _capture(provider)
    provider.complete("hi")
    assert set(sent[0]) == {"model", "messages", "max_tokens"}
    assert sent[0]["max_tokens"] == 1234


def test_the_token_cap_prefers_the_call_then_the_connection_then_the_platform(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "llm_max_tokens", 1234)
    provider = OpenAIProvider(api_key=KEY, model="m", base_url="https://api.test", default_max_tokens=500)
    assert provider.resolve_max_tokens(77) == 77
    assert provider.resolve_max_tokens(None) == 500
    provider.default_max_tokens = None
    assert provider.resolve_max_tokens(None) == 1234
    # A zero or negative cap is never sent; the next one down applies.
    assert provider.resolve_max_tokens(0) == 1234
    assert provider.resolve_max_tokens(-5) == 1234


def test_no_cap_anywhere_refuses_to_call_rather_than_send_an_unbounded_request(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "llm_max_tokens", 0)
    provider = OpenAIProvider(api_key=KEY, model="m", base_url="https://api.test")
    sent = _capture(provider)
    with pytest.raises(LLMError, match="LLM_MAX_TOKENS"):
        provider.complete("hi")
    assert sent == []


def test_anthropic_always_sends_max_tokens_and_thinking_only_from_capabilities(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_tokens", 1234)
    plain = AnthropicProvider(api_key=KEY, model="m", base_url="https://api.test", effort="high")
    sent = _capture(plain)  # the capability lookup 404s -> nothing optional is sent
    plain.complete("hi")
    assert sent[0]["max_tokens"] == 1234
    assert "thinking" not in sent[0] and "output_config" not in sent[0]

    capable = AnthropicProvider(api_key=KEY, model="m", base_url="https://api.test", effort="high")
    capable.set_capabilities(
        {
            "thinking": {"supported": True, "types": {"adaptive": {"supported": True}}},
            "effort": {"supported": True, "high": {"supported": True}, "max": {"supported": False}},
        }
    )
    sent = _capture(capable)
    capable.complete("hi")
    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert sent[0]["output_config"] == {"effort": "high"}

    unsupported_level = AnthropicProvider(api_key=KEY, model="m", base_url="https://api.test", effort="max")
    unsupported_level.set_capabilities(capable.model_capabilities())
    sent = _capture(unsupported_level)
    unsupported_level.complete("hi")
    assert "output_config" not in sent[0]


def test_anthropic_capabilities_are_read_from_the_models_api():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/v1/models/m"
            return httpx.Response(
                200, json={"id": "m", "capabilities": {"thinking": {"types": {"adaptive": {"supported": True}}}}}
            )
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "adaptive"}
        assert "output_config" not in body  # no effort set: Anthropic's default
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    provider = AnthropicProvider(api_key="another-key-for-cache", model="m", base_url="https://api.test")
    provider._client = httpx.Client(base_url=provider.base_url, transport=httpx.MockTransport(handler))
    assert provider.complete("hi").text == "ok"


def test_old_automatic_fill_ins_are_read_as_blank_but_real_choices_are_kept():
    from app.llm.from_connector import provider_from_connector

    old = registry.get_class("openai")(
        Credentials(values={"apiKey": KEY, "model": "m", "temperature": "0.2", "maxTokens": "4096"}), org_id="o"
    )
    provider = provider_from_connector("openai", {"openai": old})
    assert provider.temperature is None and provider.default_max_tokens is None

    chosen = registry.get_class("perplexity")(
        Credentials(values={"apiKey": KEY, "model": "m", "temperature": "0.5", "topP": "0.3"}), org_id="o"
    )
    provider = provider_from_connector("perplexity", {"perplexity": chosen})
    assert provider.temperature == 0.5 and provider.top_p == 0.3


# ── Images: the chosen image model, or none ────────────────────────────────
def _image_bundle(monkeypatch, values: dict):  # noqa: ANN001, ANN202
    from app.services import connectors as connector_service

    instance = registry.get_class("openai")(Credentials(values=values), org_id="o")
    bundle = SimpleNamespace(instances={"openai": instance})
    monkeypatch.setattr(connector_service, "build_connector_bundle", lambda db, org: bundle)
    return SimpleNamespace(add=lambda row: None, flush=lambda: None), SimpleNamespace(id="t")


def test_images_use_the_chosen_image_model(monkeypatch):  # noqa: ANN001
    import base64

    from app.services import images

    db, org = _image_bundle(monkeypatch, {"apiKey": KEY, "model": "m", "imageModel": "img-chosen"})
    sent: list[dict] = []

    def post(url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        sent.append(kwargs["json"])
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}]})

    monkeypatch.setattr(images, "_vendor_post", post)
    images.generate(db, org, "a picture")
    assert sent[0]["model"] == "img-chosen"


def test_no_image_model_means_no_attempt_and_a_plain_reason(monkeypatch):  # noqa: ANN001
    from app.services import images

    db, org = _image_bundle(monkeypatch, {"apiKey": KEY, "model": "m"})
    monkeypatch.setattr(images, "_vendor_post", lambda *a, **k: pytest.fail("no image call without a model"))
    with pytest.raises(images.ImageModelMissing) as caught:
        images.generate(db, org, "a picture")
    assert str(caught.value) == "Choose an image model in your OpenAI connection to create images."

    connector = SimpleNamespace(name="OpenAI", credentials=Credentials(values={}))
    ctx = SimpleNamespace(with_capability=lambda capability: connector)
    assert images.image_setup_problem(ctx) == "Choose an image model in your OpenAI connection to create images."
    connector.credentials = Credentials(values={"imageModel": "img"})
    assert images.image_setup_problem(ctx) == ""


def test_an_image_model_that_cannot_draw_says_which_setting_to_change(monkeypatch, caplog):  # noqa: ANN001
    from app.core.exceptions import ConnectorError
    from app.services import images

    db, org = _image_bundle(monkeypatch, {"apiKey": KEY, "model": "m", "imageModel": "m-text-only"})
    vendor_text = f"not an image model for key {KEY}"
    monkeypatch.setattr(
        images, "_vendor_post", lambda *a, **k: httpx.Response(400, json={"error": {"message": vendor_text}})
    )
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectorError) as caught:
        images.generate(db, org, "a picture")
    assert "could not create an image with m-text-only" in str(caught.value)
    assert "Choose a different image model in your OpenAI connection" in str(caught.value)
    # The vendor's own words go to the log, redacted, and never to the person.
    assert "not an image model" not in str(caught.value) and "said" not in str(caught.value)
    assert "not an image model" in caplog.text
    assert KEY not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_other_image_refusals_never_echo_the_vendor(monkeypatch, status):  # noqa: ANN001
    from app.core.exceptions import ConnectorError
    from app.services import images

    db, org = _image_bundle(monkeypatch, {"apiKey": KEY, "model": "m", "imageModel": "img"})
    monkeypatch.setattr(
        images, "_vendor_post", lambda *a, **k: httpx.Response(status, json={"error": {"message": "INTERNAL-DETAIL"}})
    )
    with pytest.raises(ConnectorError) as caught:
        images.generate(db, org, "a picture")
    assert "INTERNAL-DETAIL" not in str(caught.value)
    assert "HTTP" not in str(caught.value) and str(status) not in str(caught.value)


# ── Nothing in app/ names a model ──────────────────────────────────────────
_MODEL_ID = re.compile(
    r"(?<![\w-])(?:gpt-[\w.]|o[134](?![\w])|o[134]-[a-z]|claude-[a-z0-9]|gemini-[0-9a-z]"
    r"|sonar(?![\w])|sonar-[a-z]|dall-e|imagen-[0-9])"
)
_URL = re.compile(r"https?://\S+")


def test_no_model_id_is_written_anywhere_in_the_app():
    """Strings and comments alike: the model always comes from a connection
    (or, for the platform's own assistant, from .env). URLs to vendor docs are
    allowed; their paths mention product names."""
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    hits: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type not in (tokenize.STRING, tokenize.COMMENT):
                continue
            if _MODEL_ID.search(_URL.sub("", token.string)):
                hits.append(f"{path.relative_to(app_dir.parent)}:{token.start[0]}: {token.string[:80]}")
    assert hits == []


def test_the_scan_would_catch_a_model_id():
    for literal in ('"gpt-4o"', '"o3"', '"claude-sonnet-4-5"', '"gemini-2.0-flash"', '"sonar"', '"sonar-pro"', '"dall-e-3"'):
        assert _MODEL_ID.search(literal), literal
    assert not _MODEL_ID.search(_URL.sub("", '"https://ai.google.dev/gemini-api/docs"'))
