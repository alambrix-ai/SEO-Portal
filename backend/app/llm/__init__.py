"""LLM provider selection.

Agents call :func:`get_provider`; nothing else in the codebase names a vendor.
Which vendor and which model come from ``LLM_PROVIDER`` and ``LLM_MODEL``, and
there is no default for either — see :func:`get_provider`.

There is no mock provider and no offline mode. An earlier version shipped a
deterministic one that synthesised plausible marketing copy: convenient for
demos, and a genuine hazard in a platform whose agents publish to a customer's
live website and ad accounts, because a misconfigured deployment would write
invented content and nothing would look wrong. The deterministic provider
lives in the test suite, where it is a test double and cannot be selected by
configuration.

There is also no chain of providers. If the configured one is unreachable the
call fails and the agent reports that it is waiting on a model — it does not
quietly ask a different vendor. Copy on a customer's page should only ever
come from the model somebody chose.
"""
from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMProvider, LLMResponse, LLMUsage, extract_json

log = get_logger(__name__)

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Return the process-wide provider, built on first use.

    Raises :class:`LLMError` when the configuration does not name a provider,
    a model and a key for that provider. Deliberately loud: the alternative is
    a deployment that looks healthy and writes nothing, or writes with a model
    nobody chose.
    """
    global _provider
    if _provider is not None:
        return _provider

    from app.llm.providers import PROVIDERS

    choice = (settings.llm_provider or "").strip().lower()
    if choice not in PROVIDERS:
        raise LLMError(
            f"LLM_PROVIDER is {settings.llm_provider!r}. Set it to one of: "
            + ", ".join(sorted(PROVIDERS))
        )

    klass, key_field, base_url_field = PROVIDERS[choice]
    _provider = klass(
        api_key=getattr(settings, key_field),
        model=settings.llm_model,
        base_url=getattr(settings, base_url_field),
    )
    log.info(
        "LLM provider: %s (%s, effort=%s, max_tokens=%d)",
        _provider.name,
        settings.llm_model,
        settings.llm_effort,
        settings.llm_max_tokens,
    )
    if not settings.llm_price_input_per_mtok and not settings.llm_price_output_per_mtok:
        # Said once, at the point the provider is built, because the number it
        # affects is written to every agent run and reported as spend.
        log.warning(
            "LLM_PRICE_INPUT_PER_MTOK and LLM_PRICE_OUTPUT_PER_MTOK are unset, "
            "so agent run costs will read zero. Set them to your contracted "
            "rates if you report on model spend."
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
