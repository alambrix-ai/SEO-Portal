"""The four model providers, and the absence of fallbacks.

What is tested here is mostly what the code *refuses* to do. The provider
layer is where a silent fallback would be most damaging: an agent that quietly
used a different model, or a different vendor, or treated an empty completion
as content, would publish copy nobody chose to a customer's live website and
nothing would look wrong.
"""
from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.llm import get_provider, reset_provider
from app.llm.base import LLMError, LLMUsage
from app.llm.providers import (
    PROVIDERS,
    AnthropicProvider,
    GeminiProvider,
    GrokProvider,
    OpenAIProvider,
)


@pytest.fixture(autouse=True)
def _clean_provider():
    reset_provider()
    yield
    reset_provider()


def _configure(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


# ── Selection comes from configuration alone ───────────────────────────────
@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("anthropic", AnthropicProvider),
        ("openai", OpenAIProvider),
        ("gemini", GeminiProvider),
        ("grok", GrokProvider),
    ],
)
def test_the_provider_is_whichever_one_the_env_names(monkeypatch, choice, expected):
    _configure(
        monkeypatch,
        llm_provider=choice,
        llm_model="some-model",
        anthropic_api_key="k",
        openai_api_key="k",
        gemini_api_key="k",
        grok_api_key="k",
    )
    assert isinstance(get_provider(), expected)


def test_an_unknown_provider_is_refused_and_lists_the_real_ones(monkeypatch):
    _configure(monkeypatch, llm_provider="llama", llm_model="m")
    with pytest.raises(LLMError) as caught:
        get_provider()
    for name in PROVIDERS:
        assert name in str(caught.value)


def test_there_is_no_default_model(monkeypatch):
    """The model decides what gets written and what it costs.

    A default would mean a deployment that never set it still runs — writing
    to a customer's site with a model nobody chose.
    """
    _configure(monkeypatch, llm_provider="anthropic", llm_model="", anthropic_api_key="k")
    with pytest.raises(LLMError, match="LLM_MODEL"):
        get_provider()


def test_a_missing_key_names_the_setting_and_the_provider(monkeypatch):
    _configure(monkeypatch, llm_provider="gemini", llm_model="m", gemini_api_key="")
    with pytest.raises(LLMError) as caught:
        get_provider()
    assert "GEMINI_API_KEY" in str(caught.value)
    # And it does not fall back to a provider that does have a key.
    assert "Anthropic" not in str(caught.value)


def test_selecting_one_provider_does_not_require_the_others_keys(monkeypatch):
    """Switching LLM_PROVIDER must not mean re-entering credentials."""
    _configure(
        monkeypatch,
        llm_provider="grok",
        llm_model="grok-4",
        grok_api_key="k",
        anthropic_api_key="",
        openai_api_key="",
        gemini_api_key="",
    )
    assert isinstance(get_provider(), GrokProvider)


# ── Request and response shapes ────────────────────────────────────────────
def _mock(provider, handler):
    """Point a provider's client at a transport instead of the network."""
    provider._client = httpx.Client(
        base_url=provider.base_url, transport=httpx.MockTransport(handler)
    )
    return provider


def test_anthropic_sends_the_messages_shape_and_reads_its_usage(monkeypatch):
    _configure(monkeypatch, llm_price_input_per_mtok=3.0, llm_price_output_per_mtok=15.0)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "the answer"}],
                "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
                "stop_reason": "end_turn",
            },
        )

    provider = _mock(
        AnthropicProvider(api_key="k", model="claude-opus-5", base_url="https://api.test"),
        handler,
    )
    response = provider.complete("hello", system="be terse")

    assert response.text == "the answer"
    assert seen["model"] == "claude-opus-5"
    assert seen["system"] == "be terse"
    assert seen["messages"] == [{"role": "user", "content": "hello"}]
    assert seen["headers"]["x-api-key"] == "k"
    # Priced from the configured rates, not a built-in table.
    assert response.usage == LLMUsage(1_000_000, 1_000_000, 18.0)


def test_openai_and_grok_share_the_chat_completions_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        # The current parameter name, not the deprecated max_tokens.
        assert "max_completion_tokens" in body
        assert "max_tokens" not in body
        assert body["messages"][0] == {"role": "system", "content": "sys"}
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    for klass in (OpenAIProvider, GrokProvider):
        provider = _mock(klass(api_key="k", model="m", base_url="https://api.test"), handler)
        response = provider.complete("hi", system="sys")
        assert response.text == "ok"
        assert response.usage.input_tokens == 10


def test_gemini_puts_the_key_in_the_query_and_the_system_in_its_own_field():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "generateContent" in request.url.path
        assert request.url.params["key"] == "k"
        assert body["systemInstruction"]["parts"][0]["text"] == "sys"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "part one "}, {"text": "part two"}]}}
                ],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
                "modelVersion": "gemini-2.5-pro",
            },
        )

    provider = _mock(
        GeminiProvider(api_key="k", model="gemini-2.5-pro", base_url="https://api.test"),
        handler,
    )
    response = provider.complete("hi", system="sys")
    assert response.text == "part one part two"
    assert response.model == "gemini-2.5-pro"


# ── What it refuses to do ──────────────────────────────────────────────────
def test_an_empty_completion_is_an_error_not_an_answer():
    """Returning "" would let an agent treat nothing as generated content."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    provider = _mock(OpenAIProvider(api_key="k", model="m", base_url="https://api.test"), handler)
    with pytest.raises(LLMError, match="no text"):
        provider.complete("hi")


def test_an_unknown_model_says_so_instead_of_trying_another():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no such model"})

    provider = _mock(
        OpenAIProvider(api_key="k", model="gpt-nonexistent", base_url="https://api.test"),
        handler,
    )
    with pytest.raises(LLMError) as caught:
        provider.complete("hi")
    assert "gpt-nonexistent" in str(caught.value)
    assert "LLM_MODEL" in str(caught.value)


def test_a_rejected_key_names_the_setting_to_fix():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    provider = _mock(OpenAIProvider(api_key="k", model="m", base_url="https://api.test"), handler)
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        provider.complete("hi")


def test_a_refusal_is_reported_rather_than_retried_elsewhere():
    """Anthropic's server-side refusal fallbacks are deliberately not used.

    They re-run a declined request on a different model, which means copy on a
    customer's page could come from a model nobody configured — and the reply
    would not say so.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert "fallbacks" not in json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "stop_reason": "refusal",
                "stop_details": {"category": "policy"},
                "content": [],
                "usage": {},
            },
        )

    provider = _mock(
        AnthropicProvider(api_key="k", model="m", base_url="https://api.test"), handler
    )
    with pytest.raises(LLMError, match="declined"):
        provider.complete("hi")


def test_a_content_filter_is_an_error_not_an_empty_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}],
                "usage": {},
            },
        )

    provider = _mock(OpenAIProvider(api_key="k", model="m", base_url="https://api.test"), handler)
    with pytest.raises(LLMError, match="filtered"):
        provider.complete("hi")


def test_cost_is_zero_when_no_price_is_configured(monkeypatch):
    """Rather than a plausible figure from a table that goes stale.

    The number lands in agent_runs.cost and gets reported as spend, so a
    wrong one is worse than an obviously absent one — and the startup warning
    says it is absent.
    """
    _configure(monkeypatch, llm_price_input_per_mtok=0.0, llm_price_output_per_mtok=0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5_000_000, "completion_tokens": 5_000_000},
            },
        )

    provider = _mock(OpenAIProvider(api_key="k", model="m", base_url="https://api.test"), handler)
    assert provider.complete("hi").usage.cost == 0.0
