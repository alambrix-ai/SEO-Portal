"""Shared connect-form fields for AI model connectors.

Defaults match each vendor's public docs where they publish one; operators can
override every value in the Connect dialog. Values are stored on the connector
and read by :mod:`app.llm.from_connector` when agents write.
"""
from __future__ import annotations

from app.connectors.base.credentials import CredentialField, number, text

# ── Anthropic Messages API ──────────────────────────────────────────────────
# Docs: max_tokens (required hard cap), output_config.effort
# (low|medium|high|xhigh|max), adaptive thinking.
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-5"
ANTHROPIC_DEFAULT_MAX_TOKENS = "16000"
ANTHROPIC_DEFAULT_EFFORT = "high"

# ── Google Gemini generateContent ───────────────────────────────────────────
# Docs: generationConfig.maxOutputTokens, temperature (default 1.0 recommended
# for Gemini 3), thinkingConfig.thinkingLevel low|medium|high.
GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"
GEMINI_DEFAULT_MAX_TOKENS = "8192"
GEMINI_DEFAULT_TEMPERATURE = "1"
GEMINI_DEFAULT_THINKING_LEVEL = "high"

# ── OpenAI Chat Completions / Responses ─────────────────────────────────────
OPENAI_DEFAULT_MODEL = "gpt-4o"
OPENAI_DEFAULT_MAX_TOKENS = "4096"
OPENAI_DEFAULT_TEMPERATURE = "0.2"

# ── Perplexity Sonar (OpenAI-compatible) ────────────────────────────────────
# Docs: max_tokens, temperature 0–2, top_p 0–1.
PERPLEXITY_DEFAULT_MODEL = "sonar"
PERPLEXITY_DEFAULT_MAX_TOKENS = "4096"
PERPLEXITY_DEFAULT_TEMPERATURE = "0.2"
PERPLEXITY_DEFAULT_TOP_P = "0.9"


def anthropic_llm_fields() -> tuple[CredentialField, ...]:
    return (
        text(
            "model",
            "Model",
            ANTHROPIC_DEFAULT_MODEL,
            default=ANTHROPIC_DEFAULT_MODEL,
            help_text=(
                "Exact Anthropic model id (for example claude-sonnet-4-5). "
                "Pre-filled; change it if you use a different model."
            ),
        ),
        number(
            "maxTokens",
            "Max tokens",
            ANTHROPIC_DEFAULT_MAX_TOKENS,
            required=False,
            default=ANTHROPIC_DEFAULT_MAX_TOKENS,
            help_text=(
                "Hard cap on total output tokens (thinking + reply). "
                "Anthropic Messages API: max_tokens."
            ),
        ),
        text(
            "effort",
            "Effort",
            ANTHROPIC_DEFAULT_EFFORT,
            required=False,
            default=ANTHROPIC_DEFAULT_EFFORT,
            help_text=(
                "Thinking depth via output_config.effort: low, medium, high, "
                "xhigh, or max. API default is high."
            ),
        ),
    )


def gemini_llm_fields() -> tuple[CredentialField, ...]:
    return (
        text(
            "model",
            "Model",
            GEMINI_DEFAULT_MODEL,
            default=GEMINI_DEFAULT_MODEL,
            help_text=(
                "Exact Gemini model id (for example gemini-2.0-flash). "
                "Pre-filled; change it if you use a different model."
            ),
        ),
        number(
            "maxTokens",
            "Max output tokens",
            GEMINI_DEFAULT_MAX_TOKENS,
            required=False,
            default=GEMINI_DEFAULT_MAX_TOKENS,
            help_text="generationConfig.maxOutputTokens — maximum reply length.",
        ),
        number(
            "temperature",
            "Temperature",
            GEMINI_DEFAULT_TEMPERATURE,
            required=False,
            default=GEMINI_DEFAULT_TEMPERATURE,
            help_text=(
                "generationConfig.temperature (0–2). Gemini 3 docs recommend "
                "keeping 1.0 unless you have a reason to change it."
            ),
        ),
        text(
            "thinkingLevel",
            "Thinking level",
            GEMINI_DEFAULT_THINKING_LEVEL,
            required=False,
            default=GEMINI_DEFAULT_THINKING_LEVEL,
            help_text=(
                "thinkingConfig.thinkingLevel: low, medium, or high. "
                "Controls how deeply Gemini reasons before answering."
            ),
        ),
    )


def openai_llm_fields() -> tuple[CredentialField, ...]:
    return (
        text(
            "model",
            "Model",
            OPENAI_DEFAULT_MODEL,
            default=OPENAI_DEFAULT_MODEL,
            help_text=(
                "Exact OpenAI model id (for example gpt-4o). "
                "Pre-filled; change it if you use a different model."
            ),
        ),
        number(
            "maxTokens",
            "Max completion tokens",
            OPENAI_DEFAULT_MAX_TOKENS,
            required=False,
            default=OPENAI_DEFAULT_MAX_TOKENS,
            help_text="max_completion_tokens — maximum tokens in the reply.",
        ),
        number(
            "temperature",
            "Temperature",
            OPENAI_DEFAULT_TEMPERATURE,
            required=False,
            default=OPENAI_DEFAULT_TEMPERATURE,
            help_text="Sampling temperature (0–2). Lower is more deterministic.",
        ),
    )


def perplexity_llm_fields() -> tuple[CredentialField, ...]:
    return (
        text(
            "model",
            "Model",
            PERPLEXITY_DEFAULT_MODEL,
            default=PERPLEXITY_DEFAULT_MODEL,
            help_text=(
                "Sonar model id: sonar, sonar-pro, sonar-reasoning-pro, or "
                "sonar-deep-research. Pre-filled; change if needed."
            ),
        ),
        number(
            "maxTokens",
            "Max tokens",
            PERPLEXITY_DEFAULT_MAX_TOKENS,
            required=False,
            default=PERPLEXITY_DEFAULT_MAX_TOKENS,
            help_text="Maximum completion tokens (Perplexity: max_tokens).",
        ),
        number(
            "temperature",
            "Temperature",
            PERPLEXITY_DEFAULT_TEMPERATURE,
            required=False,
            default=PERPLEXITY_DEFAULT_TEMPERATURE,
            help_text="Controls randomness (0–2).",
        ),
        number(
            "topP",
            "Top P",
            PERPLEXITY_DEFAULT_TOP_P,
            required=False,
            default=PERPLEXITY_DEFAULT_TOP_P,
            help_text="Nucleus sampling top_p (0–1).",
        ),
    )


def parse_int(raw: str, *, default: int, minimum: int = 1, maximum: int = 128_000) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def parse_float(
    raw: str, *, default: float, minimum: float = 0.0, maximum: float = 2.0
) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def parse_choice(raw: str, *, allowed: set[str], default: str) -> str:
    value = (raw or "").strip().lower()
    return value if value in allowed else default
