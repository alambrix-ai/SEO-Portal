"""The four model providers, one class each.

Anthropic, OpenAI, Gemini and Grok. Which one runs, and which model, comes
from ``LLM_PROVIDER`` and ``LLM_MODEL`` — nothing in the codebase names a
vendor except this file.

OpenAI and Grok share a class because Grok's API is deliberately
OpenAI-compatible; that is shared implementation, not a fallback. Anthropic
and Gemini each have their own request and response shape.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMUsage
from app.llm.http_provider import HttpLLMProvider, price

log = get_logger(__name__)


class AnthropicProvider(HttpLLMProvider):
    """Claude, over the Messages API.

    Claude 5-generation models use adaptive thinking with ``effort`` as the
    cost dial. Older models (e.g. Sonnet 4.5) do not support adaptive
    thinking — those requests omit the thinking block. Refusal fallbacks are
    **not** used: they re-run a declined request on a different model
    server-side, which means the copy on a customer's page could come from a
    model nobody configured, and the reply would not say so.
    """

    name = "Anthropic"
    key_setting = "ANTHROPIC_API_KEY"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": settings.anthropic_api_version,
            "Content-Type": "application/json",
        }

    def _endpoint(self) -> str:
        return "/v1/messages"

    @staticmethod
    def _adaptive_thinking_supported(model: str) -> bool:
        """True for Claude 5 / Fable 5 IDs that accept thinking.type=adaptive."""
        m = (model or "").strip().lower()
        # claude-sonnet-5, claude-opus-5, claude-fable-5, claude-fable-5-1, …
        # Must not match claude-sonnet-4-5 (Adaptive is unsupported there).
        return bool(
            re.search(r"claude-(?:sonnet|opus|fable)-5(?:-|\b|$)", m)
            or re.search(r"claude-(?:sonnet|opus)-4-[6-9]", m)
        )

    def _payload(self, prompt: str, *, system: str, max_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._adaptive_thinking_supported(self.model):
            effort = (self.effort or settings.llm_effort or "high").lower()
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": effort}
        if system:
            body["system"] = system
        return body

    def _parse(self, data: dict[str, Any]) -> tuple[str, LLMUsage, str]:
        if data.get("stop_reason") == "refusal":
            details = data.get("stop_details") or {}
            raise LLMError(
                f"Anthropic declined the request (category: "
                f"{details.get('category') or 'unspecified'}). Rephrase the "
                "agent prompt or narrow its scope."
            )
        text = "".join(
            block.get("text", "")
            for block in data.get("content") or []
            if block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        return (
            text,
            LLMUsage(tokens_in, tokens_out, price(tokens_in, tokens_out)),
            str(data.get("model") or ""),
        )


class OpenAICompatibleProvider(HttpLLMProvider):
    """The chat-completions shape, used by OpenAI and by Grok."""

    def _endpoint(self) -> str:
        return "/v1/chat/completions"

    def _payload(self, prompt: str, *, system: str, max_tokens: int) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            # The current parameter name. The older `max_tokens` is rejected by
            # recent models, and sending both to see which sticks is exactly
            # the kind of guess this codebase does not make.
            "max_completion_tokens": max_tokens,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.top_p is not None:
            body["top_p"] = self.top_p
        return body

    def _parse(self, data: dict[str, Any]) -> tuple[str, LLMUsage, str]:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name} returned no choices")
        first = choices[0]
        if first.get("finish_reason") == "content_filter":
            raise LLMError(
                f"{self.name} filtered the request. Rephrase the agent prompt "
                "or narrow its scope."
            )
        text = ((first.get("message") or {}).get("content")) or ""
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        return (
            str(text),
            LLMUsage(tokens_in, tokens_out, price(tokens_in, tokens_out)),
            str(data.get("model") or ""),
        )


class OpenAIProvider(OpenAICompatibleProvider):
    name = "OpenAI"
    key_setting = "OPENAI_API_KEY"


class GrokProvider(OpenAICompatibleProvider):
    name = "Grok"
    key_setting = "GROK_API_KEY"


class GeminiProvider(HttpLLMProvider):
    """Gemini, over the generateContent API.

    The key goes in a query parameter rather than a header, the system prompt
    is its own field, and the reply is a list of parts — three small
    differences that are why this is not the OpenAI-compatible class.
    """

    name = "Gemini"
    key_setting = "GEMINI_API_KEY"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _params(self) -> dict[str, str]:
        return {"key": self.api_key}

    def _endpoint(self) -> str:
        return f"/v1beta/models/{self.model}:generateContent"

# Fix Gemini thinkingConfig nesting — put thinkingConfig inside generationConfig cleanly
    def _payload(self, prompt: str, *, system: str, max_tokens: int) -> dict[str, Any]:
        generation: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if self.temperature is not None:
            generation["temperature"] = self.temperature
        level = (self.thinking_level or "").strip().lower()
        if level in {"low", "medium", "high"}:
            generation["thinkingConfig"] = {"thinkingLevel": level}
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    def _parse(self, data: dict[str, Any]) -> tuple[str, LLMUsage, str]:
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(
                f"Gemini returned no candidates"
                + (f" (blocked: {blocked})" if blocked else "")
            )
        first = candidates[0]
        if first.get("finishReason") == "SAFETY":
            raise LLMError(
                "Gemini blocked the response on safety grounds. Rephrase the "
                "agent prompt or narrow its scope."
            )
        text = "".join(
            part.get("text", "") for part in ((first.get("content") or {}).get("parts") or [])
        )
        usage = data.get("usageMetadata") or {}
        tokens_in = int(usage.get("promptTokenCount") or 0)
        tokens_out = int(usage.get("candidatesTokenCount") or 0)
        return (
            text,
            LLMUsage(tokens_in, tokens_out, price(tokens_in, tokens_out)),
            str(data.get("modelVersion") or ""),
        )


#: Everything needed to build a provider from configuration alone.
PROVIDERS: dict[str, tuple[type[HttpLLMProvider], str, str]] = {
    "anthropic": (AnthropicProvider, "anthropic_api_key", "anthropic_base_url"),
    "openai": (OpenAIProvider, "openai_api_key", "openai_base_url"),
    "gemini": (GeminiProvider, "gemini_api_key", "gemini_base_url"),
    "grok": (GrokProvider, "grok_api_key", "grok_base_url"),
}
