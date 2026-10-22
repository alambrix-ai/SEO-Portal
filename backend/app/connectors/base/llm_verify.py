"""Live checks shared by the AI-model connectors.

OpenAI, Anthropic Claude, Google Gemini, Perplexity, Grok and the
self-hosted servers (vLLM, Ollama) are checked the same way, so the steps are written once here and each connector's
``verify_steps`` picks the ones its vendor supports. (This would sit best in
``base/llm_models.py`` next to ``ModelChoice``; it lives here only because
the base package is shared with work running in parallel.)

* **List models** — the vendor's own models endpoint, through the
  connector's ``list_models`` (the list the Connect dialog shows), and the
  saved model must be on it. Perplexity has no such list for its Sonar API.
* **Tiny completion** — one trivial prompt with a very small output cap,
  through ``provider_from_connector``: the exact provider stack agents write
  with. It costs a fraction of a cent. No citation (web search) check is run:
  that is a paid search per query, and the completion already proves the
  key, the model and the endpoint.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.connectors.base import verify_helpers as vh
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger, log_failure

if TYPE_CHECKING:  # pragma: no cover
    from app.connectors.base.verification import VerifyStep

log = get_logger(__name__)

#: The whole prompt. Short, harmless, and answerable in one token.
PROMPT = "Reply with OK"
#: Output cap for the completion: enough for "OK", small enough to cost nothing.
MAX_OUTPUT_TOKENS = 16


def _no_answer_message(name: str, exc: Exception) -> str:
    """Shown when the test prompt fails. Fixed wording: the provider's own
    message can carry vendor text, and the detail is in the log. A prompt the
    vendor blocked or filtered gets its own sentence, since a new key or model
    will not fix it."""
    raw = str(exc).lower()
    if "blocked" in raw or "filtered" in raw:
        return (
            f"{name} blocked the one-line test prompt with its safety filter. "
            "Try again, or check the safety settings on your account."
        )
    return (
        f"{name} did not answer a one-line test prompt. Check the key and the "
        "chosen model, then try again."
    )


def _chosen_model(connector) -> str:  # noqa: ANN001
    model = connector.chosen_model()
    if not model:
        raise ConnectorConfigError(f"Choose a model for {connector.name} first.")
    return model


def model_list_step(connector) -> VerifyStep:  # noqa: ANN001
    """Read the vendor's model list and find the saved model(s) on it."""
    from app.llm import model_catalog

    def run() -> tuple[str, dict]:
        model = _chosen_model(connector)
        # list_models, not the cached available_models: a live check reads live.
        models = connector.list_models()
        if model_catalog.find(models, model) is None:
            raise ConnectorError(model_catalog.stale_model_message(model, connector.name))
        evidence: dict = {"models": len(models), "model": model}
        image = connector.chosen_image_model()
        if image:
            if model_catalog.find(models, image) is None:
                raise ConnectorError(
                    model_catalog.stale_image_model_message(image, connector.name)
                )
            evidence["image_model"] = image
        return (
            f"Read {vh.plural(len(models), 'model')} this key can use; the chosen "
            f"model {model} is among them.",
            evidence,
        )

    return vh.read_step(
        "list_models", "List the models this key can use and find the chosen one", run
    )


def _spent_on_thinking(message: str) -> bool:
    """A 200 reply with no words in it — a reasoning model used the whole
    tiny cap on thinking. The key, the model and the endpoint all worked.

    Matched on the wording of ``app.llm.http_provider`` ("returned no text")
    and ``GeminiProvider._parse`` ("returned no candidates", without a
    block reason).
    """
    return "returned no text" in message or (
        "returned no candidates" in message and "blocked" not in message
    )


def tiny_completion_step(connector, *, cost: str = "a fraction of a cent") -> VerifyStep:  # noqa: ANN001
    """One trivial prompt through the provider agents write with."""

    def run() -> tuple[str, dict]:
        from app.llm.base import LLMError
        from app.llm.from_connector import provider_from_connector

        model = _chosen_model(connector)
        try:
            provider = provider_from_connector(connector.slug, {connector.slug: connector})
        except LLMError as exc:
            raise ConnectorConfigError(str(exc)) from exc
        try:
            reply = provider.complete(PROMPT, max_tokens=MAX_OUTPUT_TOKENS)
        except LLMError as exc:
            if _spent_on_thinking(str(exc)):
                return (
                    f"The model {model} answered, but used its {MAX_OUTPUT_TOKENS}-token "
                    "allowance on thinking, so no words came back. The key and the model work.",
                    {"model": model, "text_returned": False},
                )
            log_failure(log, "AI connector test prompt failed", exc, connector=connector.slug)
            raise ConnectorError(_no_answer_message(connector.name, exc)) from exc
        finally:
            provider.close()
        from app.llm.providers import usage_reported

        answered = (reply.model or model)[:200]
        if not usage_reported(reply.usage):
            # Self-hosted servers may leave token counts out: unknown, not 0.
            return (
                f"The model {answered} answered a one-line prompt (the server "
                "did not report token counts).",
                {
                    "model": answered,
                    "input_tokens": None,
                    "output_tokens": None,
                    "text_returned": True,
                },
            )
        return (
            f"The model {answered} answered a one-line prompt "
            f"({reply.usage.input_tokens} tokens in, {reply.usage.output_tokens} out).",
            {
                "model": answered,
                "input_tokens": reply.usage.input_tokens,
                "output_tokens": reply.usage.output_tokens,
                "text_returned": True,
            },
        )

    return vh.read_step(
        "tiny_completion",
        f"Ask the chosen model for a one-word reply (costs {cost})",
        run,
    )
