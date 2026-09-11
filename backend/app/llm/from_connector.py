"""Build an :class:`LLMProvider` from a connected AI-model connector.

Agents no longer use process-wide ``LLM_PROVIDER`` / ``LLM_MODEL`` keys.
The operator picks which connected model (OpenAI, Claude, Gemini, Perplexity)
each agent should write with; this module turns that connector's credentials
into the same provider stack the agents already call via ``ctx.ask``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.connectors.base.connector import Capability
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
    """Perplexity's chat API is OpenAI-shaped but not under ``/v1``."""

    name = "Perplexity"
    key_setting = "connector"

    def _endpoint(self) -> str:
        return "/chat/completions"


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

    provider = klass(api_key=api_key, model=model, base_url=base_url)
    log.info(
        "Agent LLM from connector %s (%s / %s)",
        choice,
        provider.name,
        model,
    )
    return provider
