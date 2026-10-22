"""Shared connect-form fields for AI model connectors.

The model is never written down here: it is picked in the Connect dialog from
the vendor's own live list (``GET /api/v1/connectors/{slug}/models``), and is
required. Every other setting is optional and has **no default**: blank means
the vendor's own default, and the setting is left out of the request entirely
(see :mod:`app.llm.from_connector`). The one exception is Anthropic's
``max_tokens``, which that API requires; blank there uses the platform's
``LLM_MAX_TOKENS`` setting.
"""
from __future__ import annotations

from app.connectors.base.credentials import CredentialField, model, number, text

#: The writing model's help text, per vendor. Plain words for non-technical users.
_MODEL_HELP = "The list comes from your {vendor} account. Pick the model your agents should write with."
_IMAGE_HELP = (
    "Optional. The list comes from your {vendor} account; {vendor} does not say which "
    "models can draw, so pick one that makes images. If it cannot, the first picture "
    "attempt will say so. Leave blank to make content without pictures."
)


def _model_field(vendor: str) -> CredentialField:
    return model("model", "Model", help_text=_MODEL_HELP.format(vendor=vendor))


def _image_model_field(vendor: str) -> CredentialField:
    return model(
        "imageModel",
        "Image model",
        model_kind="image",
        required=False,
        help_text=_IMAGE_HELP.format(vendor=vendor),
    )


def _max_tokens(label: str, help_text: str) -> CredentialField:
    return number("maxTokens", label, "", required=False, help_text=help_text)


def _temperature(help_text: str) -> CredentialField:
    return number("temperature", "Temperature", "", required=False, help_text=help_text)


def anthropic_llm_fields() -> tuple[CredentialField, ...]:
    return (
        _model_field("Anthropic"),
        _max_tokens(
            "Max tokens",
            "Optional. Longest reply allowed, in tokens. Blank uses this platform's standard limit.",
        ),
        text(
            "effort",
            "Effort",
            "",
            required=False,
            help_text=(
                "Optional: low, medium, high, xhigh or max — how hard the model "
                "thinks. Blank uses Anthropic's default."
            ),
        ),
    )


def gemini_llm_fields() -> tuple[CredentialField, ...]:
    return (
        _model_field("Google Gemini"),
        _image_model_field("Google Gemini"),
        _max_tokens("Max output tokens", "Optional. Longest reply allowed. Blank uses Google's default."),
        _temperature("Optional, 0–2. Blank uses Google's default, which Google recommends."),
        text(
            "thinkingLevel",
            "Thinking level",
            "",
            required=False,
            help_text=(
                "Optional: low, medium or high — how deeply Gemini reasons. "
                "Blank uses Google's default."
            ),
        ),
    )


def openai_llm_fields() -> tuple[CredentialField, ...]:
    return (
        _model_field("OpenAI"),
        _image_model_field("OpenAI"),
        _max_tokens("Max completion tokens", "Optional. Longest reply allowed. Blank uses OpenAI's default."),
        _temperature(
            "Optional, 0–2. Blank uses OpenAI's default. Some OpenAI models accept "
            "only their default, so leave this blank unless you need it."
        ),
    )


def perplexity_llm_fields() -> tuple[CredentialField, ...]:
    return (
        model(
            "model",
            "Model",
            help_text=(
                "Type the model name exactly as Perplexity's models page shows it. "
                "Perplexity does not offer a list we can read for you."
            ),
        ),
        _max_tokens("Max tokens", "Optional. Longest reply allowed. Blank uses Perplexity's default."),
        _temperature("Optional, 0–2. Blank uses Perplexity's default."),
        number(
            "topP",
            "Top P",
            "",
            required=False,
            help_text="Optional, 0–1. Blank uses Perplexity's default.",
        ),
    )


#: Values the old connect form filled in by itself when a field was left
#: blank, before these settings became optional. A saved connection holding
#: exactly one of these never chose it, so it is read as blank (the vendor's
#: default) — otherwise every older connection would keep sending, say, a
#: temperature that newer OpenAI models reject. Numbers only; no model names.
LEGACY_AUTOFILLED: dict[str, dict[str, str]] = {
    "anthropic_claude": {"maxTokens": "16000", "effort": "high"},
    "google_gemini": {"maxTokens": "8192", "temperature": "1", "thinkingLevel": "high"},
    "openai": {"maxTokens": "4096", "temperature": "0.2"},
    "perplexity": {"maxTokens": "4096", "temperature": "0.2", "topP": "0.9"},
}


def without_legacy_autofill(slug: str, values: dict[str, str]) -> dict[str, str]:
    """``values`` with the old form's automatic fill-ins removed (see above)."""
    legacy = LEGACY_AUTOFILLED.get(slug)
    if not legacy:
        return dict(values)
    return {
        k: v
        for k, v in values.items()
        if not (k in legacy and str(v or "").strip() == legacy[k])
    }


def parse_optional_int(raw: str, *, minimum: int = 1, maximum: int = 1_000_000) -> int | None:
    """The number the person typed, or None when blank or unreadable."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, value))


def parse_optional_float(
    raw: str, *, minimum: float = 0.0, maximum: float = 2.0
) -> float | None:
    """The number the person typed, or None when blank or unreadable."""
    text_value = str(raw or "").strip()
    if not text_value:
        return None
    try:
        value = float(text_value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, value))


def parse_optional_choice(raw: str, *, allowed: set[str]) -> str | None:
    """One of ``allowed`` when the person typed it, else None (vendor default)."""
    value = (raw or "").strip().lower()
    return value if value in allowed else None
