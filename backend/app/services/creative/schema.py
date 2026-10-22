"""The copy schema: what a model may return for one creative, and nothing else.

Model output is untrusted (LLM05). It is parsed into :class:`CreativeCopy`,
which is strict (no type coercion) and refuses unknown fields, so a model
cannot add a field the template was never meant to render:

* Length caps come from the format catalogue. The model-independent caps on
  the fields hold for every format; :func:`parse_copy` then applies the
  chosen format's own, tighter caps.
* Claims are not written by the model. It picks them by index from the
  product's ``approved_claims``, and :func:`ground_claims` swaps each index
  for the approved text, so a claim on a creative is always one a person
  approved word for word (LLM09).
* There is no disclaimer field. Disclaimers come from the product and the
  brand kit and are added by the template (:func:`resolve_disclaimer`).
* Colours only ever take the ``#rrggbb`` shape (:data:`HexColour`), because
  they are written into CSS where autoescaping does not apply.

What the copy *says* (banned phrases, unsupported numbers, unprovable
promises) is checked by :mod:`app.services.creative.qa`.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.models.creative import normalise_hex
from app.services.creative.formats import (
    MAX_BODY_CHARS,
    MAX_CTA_CHARS,
    MAX_HEADLINE_CHARS,
    FormatSpec,
    get_format,
)

#: Claims one creative may show; more would not fit any layout legibly.
MAX_CLAIMS_PER_CREATIVE = 3

#: A colour as ``#rrggbb``, lower case. Anything else is refused.
HexColour = Annotated[str, AfterValidator(normalise_hex)]

# C0/C1 controls other than the newline body text may use, plus the
# zero-width and bidirectional-override characters that can make rendered
# text read differently from what a reviewer approved.
_HIDDEN_CHARS = re.compile(
    "[\x00-\x09\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]"
)
_WORD = re.compile(r"\S+")


def _clean_line(value: str) -> str:
    if "\n" in value or _HIDDEN_CHARS.search(value):
        raise ValueError("Text contains characters that are not allowed")
    return unicodedata.normalize("NFC", value.strip())


def _clean_block(value: str) -> str:
    if _HIDDEN_CHARS.search(value):
        raise ValueError("Text contains characters that are not allowed")
    return unicodedata.normalize("NFC", value.strip())


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


class CreativeCopy(BaseModel):
    """The copy for one creative, exactly as the model must return it."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    headline: str = Field(max_length=MAX_HEADLINE_CHARS)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)
    cta: str = Field(max_length=MAX_CTA_CHARS)
    #: Indices into the product's ``approved_claims``.
    claim_ids: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list, max_length=MAX_CLAIMS_PER_CREATIVE
    )

    @field_validator("headline", "cta")
    @classmethod
    def _one_line(cls, value: str) -> str:
        cleaned = _clean_line(value)
        if not cleaned:
            raise ValueError("This field cannot be empty")
        return cleaned

    @field_validator("body")
    @classmethod
    def _block(cls, value: str) -> str:
        return _clean_block(value)

    @field_validator("claim_ids")
    @classmethod
    def _unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("A claim can only be used once")
        return value

    @model_validator(mode="after")
    def _fits_format_and_claims(self, info: ValidationInfo) -> CreativeCopy:
        context: Mapping[str, Any] = info.context or {}
        spec = context.get("format")
        if isinstance(spec, FormatSpec):
            _check_limits(self, spec)
        claim_count = context.get("claim_count")
        # Deny by default: without the product's claims there is nothing an
        # index could safely refer to.
        if self.claim_ids and not isinstance(claim_count, int):
            raise ValueError("This creative cannot use product claims")
        if isinstance(claim_count, int) and any(i >= claim_count for i in self.claim_ids):
            raise ValueError("A claim must be one of the product's approved claims")
        return self


def _check_limits(copy: CreativeCopy, spec: FormatSpec) -> None:
    limits = spec.limits
    if len(copy.headline) > limits.headline_chars:
        raise ValueError(f"The headline is too long for this format (max {limits.headline_chars} characters)")
    if limits.headline_words is not None and word_count(copy.headline) > limits.headline_words:
        raise ValueError(f"The headline is too long for this format (max {limits.headline_words} words)")
    if len(copy.body) > limits.body_chars:
        if limits.body_chars == 0:
            raise ValueError("This format has no room for body text")
        raise ValueError(f"The body text is too long for this format (max {limits.body_chars} characters)")
    if len(copy.cta) > limits.cta_chars:
        raise ValueError(f"The button text is too long for this format (max {limits.cta_chars} characters)")


def _approved_texts(approved_claims: Sequence[Any]) -> list[str | None]:
    """Approved claims as text; an entry that is not usable text is ``None``
    so it keeps its index but can never be shown."""
    return [c.strip() if isinstance(c, str) and c.strip() else None for c in approved_claims]


def parse_copy(
    data: Mapping[str, Any] | str | bytes,
    *,
    format_id: str,
    approved_claims: Sequence[Any],
) -> CreativeCopy:
    """Validate model output (a mapping or JSON text) for one format.

    Raises ``pydantic.ValidationError`` (or ``ValueError`` for an unknown
    format); callers turn either into a plain-English refusal.
    """
    context = {"format": get_format(format_id), "claim_count": len(approved_claims)}
    if isinstance(data, (str, bytes)):
        return CreativeCopy.model_validate_json(data, context=context)
    return CreativeCopy.model_validate(data, context=context)


def ground_claims(copy: CreativeCopy, approved_claims: Sequence[Any]) -> tuple[str, ...]:
    """Swap the copy's claim indices for the approved claim texts.

    Re-checked here, not only in the schema, because the product's claims can
    change between validation and rendering: an index that no longer points
    at usable approved text is refused, never skipped or guessed.
    """
    texts = _approved_texts(approved_claims)
    grounded: list[str] = []
    for index in copy.claim_ids:
        text = texts[index] if 0 <= index < len(texts) else None
        if text is None:
            raise ValueError("A claim must be one of the product's approved claims")
        grounded.append(text)
    return tuple(grounded)


def resolve_disclaimer(*, product_disclaimer: str = "", kit_disclaimer: str = "") -> str:
    """The disclaimer the template adds: the product's, then the brand kit's.

    Never taken from model output; the copy schema has no field for one.
    """
    parts: list[str] = []
    for part in (product_disclaimer, kit_disclaimer):
        cleaned = (part or "").strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class ComposedCopy:
    """Everything the template renders as text, from trusted sources only
    (claims and disclaimer) or validated ones (headline, body, button)."""

    headline: str
    body: str
    cta: str
    claims: tuple[str, ...]
    disclaimer: str


def compose_copy(
    copy: CreativeCopy,
    *,
    approved_claims: Sequence[Any],
    product_disclaimer: str = "",
    kit_disclaimer: str = "",
) -> ComposedCopy:
    return ComposedCopy(
        headline=copy.headline,
        body=copy.body,
        cta=copy.cta,
        claims=ground_claims(copy, approved_claims),
        disclaimer=resolve_disclaimer(
            product_disclaimer=product_disclaimer, kit_disclaimer=kit_disclaimer
        ),
    )


class Palette(BaseModel):
    """The colours one creative is drawn with, all ``#rrggbb``."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    background: HexColour
    text: HexColour
    accent: HexColour
    #: Text drawn on the accent colour (the button label).
    on_accent: HexColour


__all__ = [
    "MAX_CLAIMS_PER_CREATIVE",
    "ComposedCopy",
    "CreativeCopy",
    "HexColour",
    "Palette",
    "compose_copy",
    "ground_claims",
    "parse_copy",
    "resolve_disclaimer",
    "word_count",
]
