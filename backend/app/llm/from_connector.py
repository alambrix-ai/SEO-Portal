"""Build an :class:`LLMProvider` from a connected AI-model connector.

Agents no longer use process-wide ``LLM_PROVIDER`` / ``LLM_MODEL`` keys.
The operator picks which connected model (OpenAI, Claude, Gemini, Perplexity,
Grok, or the customer's own vLLM or Ollama server) each agent should write with; this module turns that connector's credentials
into the same provider stack the agents already call via ``ctx.ask``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from typing import TYPE_CHECKING, Any

from app.connectors.base.connector import Capability
from app.connectors.base.llm_fields import (
    parse_optional_choice,
    parse_optional_float,
    parse_optional_int,
    without_legacy_autofill,
)
from app.core.egress import EgressBlockedError
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMProvider
from app.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    GrokProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    SelfHostedOpenAIProvider,
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

    def _payload(self, prompt: str, *, system: str, max_tokens: int | None) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        # Every one of these is optional on Perplexity's chat completions;
        # each is sent only when set.
        if max_tokens:
            body["max_tokens"] = max_tokens
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
    # xAI's own address, fixed: GrokProvider adds /v1/chat/completions.
    "xai_grok": (GrokProvider, "https://api.x.ai"),
    # The customer's own server: the root comes from the connection and is
    # validated by the egress guard (SelfHostedOpenAIProvider).
    "vllm": (SelfHostedOpenAIProvider, ""),
    "ollama": (SelfHostedOpenAIProvider, ""),
}

#: Connectors whose address the customer supplies, and whose key is optional.
SELF_HOSTED: frozenset[str] = frozenset({"vllm", "ollama"})


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
        temperature: float | None = None,
    ):
        raise LLMError("This agent does not use an AI model.")


def supports_agent_llm(slug: str) -> bool:
    return slug in _CONNECTOR_PROVIDERS


def effective_llm(
    choice: str, connected: Iterable[str], *, allow_fallback: bool = True
) -> tuple[str, bool]:
    """``(model connector a run uses, whether the saved choice is out of date)``.

    The agent's chosen model when it is connected; otherwise the first
    connected model in catalogue order, so disconnecting OpenAI does not stop
    every agent that was set to it while Claude is connected. ``""`` when no
    model is connected at all. The setup screen, the agent page and the run
    all call this, so what the console says is what the run does.

    With ``allow_fallback=False`` (strict, an explicit model choice) only the
    chosen connector is ever returned: when it is not connected the result is
    ``""`` and the caller must stop, never write with a different model.
    """
    from app.connectors.base import registry

    choice = (choice or "").strip()
    have = set(connected)
    if choice and choice in have and choice in _CONNECTOR_PROVIDERS:
        return choice, False
    if not allow_fallback:
        return "", bool(choice)
    for slug in registry.connector_slugs():
        if slug in have and slug in _CONNECTOR_PROVIDERS:
            return slug, bool(choice)
    return "", bool(choice)


def _options_for(slug: str, creds: Any) -> dict[str, Any]:  # noqa: ANN401
    """Vendor-specific settings from the connection — only those the person set.

    A blank setting is ``None`` and is left out of the request, so the
    vendor's own default applies. Values the old form filled in by itself are
    read as blank (see ``LEGACY_AUTOFILLED``).
    """
    values = without_legacy_autofill(slug, dict(getattr(creds, "values", {}) or {}))
    get = values.get
    max_tokens = parse_optional_int(get("maxTokens") or "")
    if slug == "anthropic_claude":
        return {
            "default_max_tokens": max_tokens,
            "effort": parse_optional_choice(
                get("effort") or "", allowed={"low", "medium", "high", "xhigh", "max"}
            ),
        }
    if slug == "google_gemini":
        return {
            "default_max_tokens": max_tokens,
            "temperature": parse_optional_float(get("temperature") or ""),
            "thinking_level": parse_optional_choice(
                get("thinkingLevel") or "", allowed={"low", "medium", "high"}
            ),
        }
    if slug == "openai":
        return {
            "default_max_tokens": max_tokens,
            "temperature": parse_optional_float(get("temperature") or ""),
        }
    if slug == "perplexity":
        return {
            "default_max_tokens": max_tokens,
            "temperature": parse_optional_float(get("temperature") or ""),
            "top_p": parse_optional_float(get("topP") or "", maximum=1.0),
        }
    if slug == "xai_grok":
        return {
            "default_max_tokens": max_tokens,
            "temperature": parse_optional_float(get("temperature") or ""),
        }
    if slug in SELF_HOSTED:
        return {"default_max_tokens": max_tokens}
    return {}


#: An explicitly chosen model id. Some vendors put the model in the URL path
#: (Gemini, Anthropic's model lookup), so separators, traversal and query or
#: fragment characters are refused rather than escaped.
_MODEL_OVERRIDE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
#: A self-hosted model id (vLLM serves Hugging Face ids such as
#: ``org/model``). Segments of the strict shape joined by single ``/``: no
#: leading, trailing or doubled slash, no traversal, no query or fragment.
#: Safe only because these providers send the model in the request body,
#: never in a URL path (``SelfHostedOpenAIProvider._payload``).
_SELF_HOSTED_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*(?:/[A-Za-z0-9][A-Za-z0-9._:-]*)*$")
#: Longest model id accepted from anyone.
MAX_MODEL_ID_CHARS = 128


def valid_model_id(slug: str, model: object) -> bool:
    """Whether ``model`` is an acceptable explicit model id for connector ``slug``.

    Provider-aware: ``/`` is allowed only for the self-hosted connectors
    (:data:`SELF_HOSTED`), whose id goes in the request body only. Every
    other connector keeps the strict shape, because some put the model in a
    URL path.
    """
    if not isinstance(model, str) or not model or len(model) > MAX_MODEL_ID_CHARS:
        return False
    if ".." in model:
        return False
    pattern = _SELF_HOSTED_MODEL if (slug or "").strip() in SELF_HOSTED else _MODEL_OVERRIDE
    return pattern.fullmatch(model) is not None


def _checked_model_override(model: str, pretty: str, slug: str = "") -> str:
    """``model`` stripped and validated for ``slug``, or :class:`LLMError`."""
    value = (model or "").strip() if isinstance(model, str) else ""
    if not valid_model_id(slug, value):
        raise LLMError(
            f"That model name cannot be used with {pretty}. Choose a model "
            "from the list and try again."
        )
    return value


def provider_from_connector(
    slug: str,
    connectors: dict[str, BaseConnector],
    *,
    names: dict[str, str] | None = None,
    model_override: str | None = None,
) -> LLMProvider:
    """Resolve a connected model into an :class:`LLMProvider`.

    Raises :class:`LLMError` with operator-facing wording when the choice is
    missing, disconnected, or incomplete — same skip class as a missing CMS.

    ``model_override`` is an explicit model chosen for this one call (for
    example in Creative Studio). It replaces the connection's saved model and
    is strict: it is validated, used exactly, and never swapped for the saved
    model or another connector when it is unusable. The caller is still
    responsible for checking it against the model allow-list.
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
            "Pick OpenAI, Anthropic Claude, Google Gemini, Perplexity, Grok, "
            "or your own model server."
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
    if choice in SELF_HOSTED:
        # Optional key; the address is the connection's own, validated by
        # the provider through the egress guard.
        api_key = instance.credentials.get("apiKey") or ""
        base_url = instance.base_url
        if not base_url:
            raise LLMError(
                f"{pretty} has no server address. Open Connectors and finish the connection."
            )
    else:
        try:
            api_key = instance.credentials.require("apiKey")
        except Exception as exc:  # noqa: BLE001 - credential helpers raise domain errors
            raise LLMError(
                f"{pretty} is missing its API key. Open Connectors and finish the connection."
            ) from exc
    # The model is the one picked from the vendor's list. There is no default
    # to fall back to: a connection without one is asked to choose.
    if model_override is not None:
        model = _checked_model_override(model_override, pretty, choice)
    else:
        model = (instance.credentials.get("model") or "").strip()
    if not model:
        raise LLMError(
            f"Choose a model for {pretty}. Open Connectors, open {pretty} and "
            "pick one from the list."
        )
    if model_override is None and not valid_model_id(choice, model):
        # The saved model is checked like an explicit one: some vendors put
        # it in a URL path, so a malformed value must never reach a request.
        log.warning("Agent LLM from connector %s: saved model id refused", choice)
        raise LLMError(
            f"The model saved for {pretty} cannot be used. Open Connectors, "
            f"open {pretty} and pick one from the list."
        )

    options = _options_for(choice, instance.credentials)
    try:
        provider = klass(
            api_key=api_key, model=model, base_url=base_url, vendor_label=pretty, **options
        )
    except EgressBlockedError as exc:
        # The guard's own sentence ("That address points to a private or
        # internal network…") is written for people; the address is not logged.
        log.warning("Agent LLM from connector %s refused by the egress guard: %s", choice, exc.reason)
        raise LLMError(f"{pretty}: {exc.message}") from exc
    log.info(
        "Agent LLM from connector %s (%s / %s) options=%s",
        choice,
        provider.name,
        model,
        {k: v for k, v in options.items()},
    )
    return provider
