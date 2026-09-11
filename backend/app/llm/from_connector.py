"""Build an :class:`LLMProvider` from a connected AI-model connector.

Agents no longer use process-wide ``LLM_PROVIDER`` / ``LLM_MODEL`` keys.
The operator picks which connected model (OpenAI, Claude, Gemini, Perplexity)
each agent should write with; this module turns that connector's credentials
into the same provider stack the agents already call via ``ctx.ask``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.connectors.base.connector import Capability
from app.connectors.base.llm_fields import (
    ANTHROPIC_DEFAULT_EFFORT,
    ANTHROPIC_DEFAULT_MAX_TOKENS,
    GEMINI_DEFAULT_MAX_TOKENS,
    GEMINI_DEFAULT_TEMPERATURE,
    GEMINI_DEFAULT_THINKING_LEVEL,
    OPENAI_DEFAULT_MAX_TOKENS,
    OPENAI_DEFAULT_TEMPERATURE,
    PERPLEXITY_DEFAULT_MAX_TOKENS,
    PERPLEXITY_DEFAULT_TEMPERATURE,
    PERPLEXITY_DEFAULT_TOP_P,
    parse_choice,
    parse_float,
    parse_int,
)
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMProvider
from app.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.connectors.base.connector import BaseConnector

log = get_logger(__name__)


class PerplexityProvider(OpenAICompatibleProvider):
    """Perplexity Sonar — OpenAI-shaped chat, with ``max_tokens`` (not completion)."""

    name = "Perplexity"
    key_setting = "connector"

    def _endpoint(self) -> str:
        return "/chat/completions"

    def _payload(self, prompt: str, *, system: str, max_tokens: int) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.top_p is not None:
            body["top_p"] = self.top_p
        return body


# Connector slug → (provider class, API root). Roots match the vendor chat
# endpoints the HttpLLMProvider subclasses call — not always the connector's
# citation base_url (OpenAI's connector root ends in /v1).
_CONNECTOR_PROVIDERS: dict[str, tuple[type, str]] = {
    "anthropic_claude": (AnthropicProvider, "https://api.anthropic.com"),
    "openai": (OpenAIProvider, "https://api.openai.com"),
    "google_gemini": (GeminiProvider, "https://generativelanguage.googleapis.com"),
    "perplexity": (PerplexityProvider, "https://api.perplexity.ai"),
}


class UnusedLLMProvider(LLMProvider):
    """Stand-in for agents that never call the model.

    Kept so :class:`AgentContext` always has an ``llm`` attribute; any accidental
    call fails loudly rather than inventing content.
    """

    name = "unused"

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ):
        raise LLMError("This agent does not use an AI model.")


def supports_agent_llm(slug: str) -> bool:
    return slug in _CONNECTOR_PROVIDERS


def _options_for(slug: str, creds: Any) -> dict[str, Any]:
    """Vendor-specific generation knobs from the connector's stored values."""
    get = creds.get
    if slug == "anthropic_claude":
        return {
            "default_max_tokens": parse_int(
                get("maxTokens"), default=int(ANTHROPIC_DEFAULT_MAX_TOKENS)
            ),
            "effort": parse_choice(
                get("effort"),
                allowed={"low", "medium", "high", "xhigh", "max"},
                default=ANTHROPIC_DEFAULT_EFFORT,
            ),
        }
    if slug == "google_gemini":
        return {
            "default_max_tokens": parse_int(
                get("maxTokens"), default=int(GEMINI_DEFAULT_MAX_TOKENS)
            ),
            "temperature": parse_float(
                get("temperature"), default=float(GEMINI_DEFAULT_TEMPERATURE)
            ),
            "thinking_level": parse_choice(
                get("thinkingLevel"),
                allowed={"low", "medium", "high"},
                default=GEMINI_DEFAULT_THINKING_LEVEL,
            ),
        }
    if slug == "openai":
        return {
            "default_max_tokens": parse_int(
                get("maxTokens"), default=int(OPENAI_DEFAULT_MAX_TOKENS)
            ),
            "temperature": parse_float(
                get("temperature"), default=float(OPENAI_DEFAULT_TEMPERATURE)
            ),
        }
    if slug == "perplexity":
        return {
            "default_max_tokens": parse_int(
                get("maxTokens"), default=int(PERPLEXITY_DEFAULT_MAX_TOKENS)
            ),
            "temperature": parse_float(
                get("temperature"), default=float(PERPLEXITY_DEFAULT_TEMPERATURE)
            ),
            "top_p": parse_float(
                get("topP"),
                default=float(PERPLEXITY_DEFAULT_TOP_P),
                minimum=0.0,
                maximum=1.0,
            ),
        }
    return {}


def provider_from_connector(
    slug: str,
    connectors: dict[str, BaseConnector],
    *,
    names: dict[str, str] | None = None,
) -> LLMProvider:
    """Resolve a connected model into an :class:`LLMProvider`.

    Raises :class:`LLMError` with operator-facing wording when the choice is
    missing, disconnected, or incomplete — same skip class as a missing CMS.
    """
    choice = (slug or "").strip()
    label_map = names or {}
    if not choice:
        raise LLMError(
            "Choose which AI model this agent should use before it can run."
        )

    entry = _CONNECTOR_PROVIDERS.get(choice)
    if entry is None:
        raise LLMError(
            "That AI model connector cannot be used for writing. "
            "Pick OpenAI, Anthropic Claude, Google Gemini, or Perplexity."
        )

    instance = connectors.get(choice)
    pretty = label_map.get(choice) or (instance.name if instance else choice)
    if instance is None:
        raise LLMError(
            f"{pretty} is not connected. Open Connectors, connect it, then "
            "choose it again on this agent."
        )
    if not instance.supports(Capability.COMPLETE):
        raise LLMError(
            f"{pretty} is connected but cannot generate text for agents."
        )

    klass, base_url = entry
    try:
        api_key = instance.credentials.require("apiKey")
        model = instance.credentials.require("model")
    except Exception as exc:  # noqa: BLE001 - credential helpers raise domain errors
        raise LLMError(
            f"{pretty} is missing its API key or model name. "
            "Open Connectors and finish the connection."
        ) from exc

    options = _options_for(choice, instance.credentials)
    provider = klass(api_key=api_key, model=model, base_url=base_url, **options)
    log.info(
        "Agent LLM from connector %s (%s / %s) options=%s",
        choice,
        provider.name,
        model,
        {k: v for k, v in options.items()},
    )
    return provider
