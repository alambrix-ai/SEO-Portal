"""LLM provider selection.

Agent runs resolve their model from the connector the operator picked on that
agent (see :mod:`app.llm.from_connector`). :func:`get_provider` remains for
tests and rare tooling that still want a process-wide provider from env —
it is not used by the agent runner.
"""
from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMProvider, LLMResponse, LLMUsage, extract_json

log = get_logger(__name__)

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Return a process-wide provider from env, built on first use.

    Prefer connector-backed providers for agent work. This path is for tests
    and tooling only; missing env config raises :class:`LLMError`.
    """
    global _provider
    if _provider is not None:
        return _provider

    from app.llm.providers import PROVIDERS

    choice = (settings.llm_provider or "").strip().lower()
    if choice not in PROVIDERS:
        raise LLMError(
            "No process-wide LLM is configured. Agents use the AI model "
            "connector chosen on each agent instead."
        )

    klass, key_field, base_url_field = PROVIDERS[choice]
    _provider = klass(
        api_key=getattr(settings, key_field),
        model=settings.llm_model,
        base_url=getattr(settings, base_url_field),
    )
    log.info(
        "LLM provider (env tooling): %s (%s)",
        _provider.name,
        settings.llm_model,
    )
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    """Install a provider explicitly. Used by tests to inject a double."""
    global _provider
    _provider = provider


def reset_provider() -> None:
    """Drop the cached provider, so the next call rebuilds it."""
    set_provider(None)


__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "LLMUsage",
    "extract_json",
    "get_provider",
    "reset_provider",
    "set_provider",
]
