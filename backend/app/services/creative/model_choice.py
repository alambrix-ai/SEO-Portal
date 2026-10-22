"""Which AI models Creative Studio may use, and the picker the console shows.

Model ids never appear in code. The platform operator approves models with
four glob lists in the environment (see ``.env.example``):

* ``CREATIVE_ALLOWED_TEXT_MODELS`` / ``CREATIVE_DENIED_TEXT_MODELS``
* ``CREATIVE_ALLOWED_IMAGE_MODELS`` / ``CREATIVE_DENIED_IMAGE_MODELS``

A pattern is matched with :func:`fnmatch.fnmatchcase` against the lower-cased
model id. ``<connector>:<glob>`` limits a pattern to one connector, so
``ollama:*`` approves every model on a self-hosted Ollama connection and
nothing else. The deny list always wins, and an **empty allow list approves
nothing**: until the operator configures it, the picker says so in plain
English instead of failing.

Resolution is strict. The chosen connector must be connected, the model must
be approved and not denied, and the provider is built through
``effective_llm(..., allow_fallback=False)`` and
``provider_from_connector(..., model_override=...)``. Nothing ever falls back
to a different connector or model.

People see friendly names only ("Claude, best for writing"). The connector
slug and model id are carried in ``advanced`` for the "Advanced" disclosure.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from pydantic import Field

from app.connectors.base.connector import Capability
from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.core.logging import get_logger, log_failure
from app.llm.base import LLMError, LLMProvider
from app.llm.from_connector import effective_llm, provider_from_connector, valid_model_id
from app.schemas.common import ApiModel

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

    from app.connectors.base.connector import BaseConnector
    from app.models.workspace import Organization

log = get_logger(__name__)

# ── Friendly names ────────────────────────────────────────────────────────
#: Connector slug → what a marketer sees for writing. Order is picker order.
#: The self-hosted connectors (vLLM, Ollama) write text only.
TEXT_LABELS: dict[str, str] = {
    "anthropic_claude": "Claude, best for writing",
    "openai": "ChatGPT",
    "google_gemini": "Gemini",
    "perplexity": "Perplexity",
    "xai_grok": "Grok (xAI)",
    "vllm": "Your own model (vLLM)",
    "ollama": "Your own model (Ollama)",
}

#: Connector slug → what a marketer sees for pictures. Self-hosted
#: connectors make no images, so they have no entry.
IMAGE_LABELS: dict[str, str] = {
    "openai": "OpenAI image",
    "google_gemini": "Google image",
}

#: The image choice that uses no AI model. Always allowed.
NO_IMAGE = "none"
NO_IMAGE_LABEL = "No AI image: use my photos or brand colours"

#: The connection field holding the chosen image model (as ``images.py``).
_IMAGE_MODEL_FIELD = "imageModel"

#: Every connector a ``<connector>:`` pattern prefix may name.
_KNOWN_CONNECTORS: frozenset[str] = frozenset(TEXT_LABELS) | frozenset(IMAGE_LABELS)

#: Model ids are checked with ``from_connector.valid_model_id``, the same
#: provider-aware rule ``provider_from_connector`` applies: strict for every
#: vendor, with ``/`` allowed only for the self-hosted connectors (vLLM and
#: Ollama), whose model id goes in the request body and never in a URL.
_CONNECTOR_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# ── Plain-English reasons ─────────────────────────────────────────────────
NEEDS_APPROVAL = "An administrator needs to approve AI models for Creative Studio."
_UNKNOWN = "That AI model can't be used in Creative Studio. Choose one from the list."
_NOT_APPROVED = (
    "That AI model isn't approved for Creative Studio. Choose another one, "
    "or ask an administrator to approve it."
)
_NO_MODEL_CHOSEN = "Choose a model for this AI connection before using it here."


def _not_connected(label: str) -> str:
    return f"{label} isn't connected. Ask an admin to connect it, or choose a different one."


def _not_ready(label: str) -> str:
    return f"{label} isn't ready to use yet. Ask an admin to finish setting it up."


# Reason keys, so the console can choose its own wording or styling.
REASON_NEEDS_APPROVAL = "models_not_approved"
REASON_UNKNOWN = "unknown_connector"
REASON_NOT_CONNECTED = "not_connected"
REASON_NOT_APPROVED = "model_not_allowed"
REASON_NOT_READY = "model_unavailable"
REASON_NO_MODEL = "no_model_chosen"


class ModelChoiceError(InvalidInputError):
    """A Creative Studio model choice that cannot be used.

    ``message`` is authored plain English; ``details["reason"]`` is one of the
    ``REASON_*`` keys. Never carries vendor or exception text.
    """

    code = "model_unavailable"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, details={"reason": reason})
        self.reason = reason


# ── Allow-list matching ───────────────────────────────────────────────────
def _split(pattern: str) -> tuple[str | None, str]:
    """``(connector or None, glob)`` from one configured pattern."""
    text = (pattern or "").strip().lower()
    head, sep, rest = text.partition(":")
    if sep and rest and head in _KNOWN_CONNECTORS:
        return head, rest
    return None, text


def matches_any(patterns: Iterable[str], connector: str, model: str) -> bool:
    """True when ``model`` on ``connector`` matches one of ``patterns``."""
    slug = (connector or "").strip().lower()
    model_id = (model or "").strip().lower()
    if not model_id:
        return False
    for raw in patterns:
        scope, glob = _split(raw)
        if not glob or (scope is not None and scope != slug):
            continue
        if fnmatchcase(model_id, glob):
            return True
    return False


def is_approved(
    connector: str, model: str, allow: Iterable[str], deny: Iterable[str]
) -> bool:
    """Allowed and not denied. An empty allow list approves nothing."""
    allow = list(allow)
    if not allow:
        return False
    return matches_any(allow, connector, model) and not matches_any(deny, connector, model)


def text_approved(connector: str, model: str) -> bool:
    return is_approved(
        connector,
        model,
        settings.creative_allowed_text_models,
        settings.creative_denied_text_models,
    )


def image_approved(connector: str, model: str) -> bool:
    return is_approved(
        connector,
        model,
        settings.creative_allowed_image_models,
        settings.creative_denied_image_models,
    )


# ── Picker ────────────────────────────────────────────────────────────────
class AdvancedChoice(ApiModel):
    """Raw identifiers, for the "Advanced" disclosure only."""

    connector: str = ""
    model: str = ""


class ModelOption(ApiModel):
    #: What the console sends back: a connector slug, or ``"none"``.
    key: str
    label: str
    available: bool
    #: Why it can't be chosen right now, in plain English ("" when it can).
    reason: str = ""
    reason_code: str = ""
    advanced: AdvancedChoice = Field(default_factory=AdvancedChoice)


class ModelPicker(ApiModel):
    text: list[ModelOption] = Field(default_factory=list)
    image: list[ModelOption] = Field(default_factory=list)
    #: False when nothing can write yet; ``reason`` says why.
    text_ready: bool = False
    reason: str = ""
    reason_code: str = ""


def _saved(connector: BaseConnector, key: str) -> str:
    credentials = getattr(connector, "credentials", None)
    getter = getattr(credentials, "get", None)
    value = getter(key) if callable(getter) else ""
    return value.strip() if isinstance(value, str) else ""


def _option(
    key: str, label: str, model: str, *, approved: bool, allow_empty: bool
) -> ModelOption:
    advanced = AdvancedChoice(connector=key, model=model)
    if allow_empty:
        return ModelOption(
            key=key, label=label, available=False, reason=NEEDS_APPROVAL,
            reason_code=REASON_NEEDS_APPROVAL, advanced=advanced,
        )
    if not model:
        return ModelOption(
            key=key, label=label, available=False, reason=_NO_MODEL_CHOSEN,
            reason_code=REASON_NO_MODEL, advanced=advanced,
        )
    if not approved:
        return ModelOption(
            key=key, label=label, available=False, reason=_NOT_APPROVED,
            reason_code=REASON_NOT_APPROVED, advanced=advanced,
        )
    return ModelOption(key=key, label=label, available=True, advanced=advanced)


def build_picker(connectors: Mapping[str, BaseConnector]) -> ModelPicker:
    """The choices for one workspace, from its usable connections."""
    text_allow_empty = not settings.creative_allowed_text_models
    image_allow_empty = not settings.creative_allowed_image_models

    text: list[ModelOption] = []
    for slug, label in TEXT_LABELS.items():
        instance = connectors.get(slug)
        if instance is None or not instance.supports(Capability.COMPLETE):
            continue
        model = _saved(instance, "model")
        text.append(
            _option(
                slug, label, model,
                approved=text_approved(slug, model), allow_empty=text_allow_empty,
            )
        )

    image: list[ModelOption] = []
    for slug, label in IMAGE_LABELS.items():
        instance = connectors.get(slug)
        if instance is None or not instance.supports(Capability.GENERATE_IMAGE):
            continue
        model = _saved(instance, _IMAGE_MODEL_FIELD)
        image.append(
            _option(
                slug, label, model,
                approved=image_approved(slug, model), allow_empty=image_allow_empty,
            )
        )
    image.append(ModelOption(key=NO_IMAGE, label=NO_IMAGE_LABEL, available=True))

    picker = ModelPicker(text=text, image=image)
    if text_allow_empty:
        picker.reason, picker.reason_code = NEEDS_APPROVAL, REASON_NEEDS_APPROVAL
    elif not text:
        picker.reason = "Connect an AI model to start creating."
        picker.reason_code = REASON_NOT_CONNECTED
    elif not any(o.available for o in text):
        picker.reason, picker.reason_code = _NOT_APPROVED, REASON_NOT_APPROVED
    picker.text_ready = any(o.available for o in text)
    return picker


def picker_for(db: Session, org: Organization) -> ModelPicker:
    """:func:`build_picker` for one organisation's usable connections."""
    from app.services.connectors import build_connector_bundle

    return build_picker(build_connector_bundle(db, org).instances)


# ── Strict resolution ─────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TextChoice:
    connector: str
    model: str
    provider: LLMProvider


@dataclass(frozen=True, slots=True)
class ImageChoice:
    #: ``""`` for "No AI image".
    connector: str
    model: str

    @property
    def uses_ai(self) -> bool:
        return bool(self.connector)


def _clean_connector(value: object) -> str:
    slug = value.strip().lower() if isinstance(value, str) else ""
    if not _CONNECTOR_ID.fullmatch(slug):
        raise ModelChoiceError(REASON_UNKNOWN, _UNKNOWN)
    return slug


def _clean_model(value: object, connector: str) -> str | None:
    """``None`` for "use the connection's model"; otherwise an id valid for ``connector``."""
    if value is None:
        return None
    model = value.strip() if isinstance(value, str) else ""
    if not model:
        return None
    if not valid_model_id(connector, model):
        raise ModelChoiceError(REASON_NOT_APPROVED, _NOT_APPROVED)
    return model


def resolve_text(
    connectors: Mapping[str, BaseConnector],
    connector: str,
    model: str | None = None,
    *,
    names: dict[str, str] | None = None,
) -> TextChoice:
    """The provider for exactly this connector and model, or :class:`ModelChoiceError`.

    ``model`` ``None`` means the model saved on the connection; it is checked
    against the allow list like any other.
    """
    slug = _clean_connector(connector)
    wanted = _clean_model(model, slug)
    label = TEXT_LABELS.get(slug)
    if label is None:
        raise ModelChoiceError(REASON_UNKNOWN, _UNKNOWN)
    if not settings.creative_allowed_text_models:
        raise ModelChoiceError(REASON_NEEDS_APPROVAL, NEEDS_APPROVAL)

    using, _stale = effective_llm(slug, connectors.keys(), allow_fallback=False)
    instance = connectors.get(slug)
    if using != slug or instance is None or not instance.supports(Capability.COMPLETE):
        raise ModelChoiceError(REASON_NOT_CONNECTED, _not_connected(label))

    chosen = wanted if wanted is not None else _saved(instance, "model")
    if not chosen:
        raise ModelChoiceError(REASON_NO_MODEL, _NO_MODEL_CHOSEN)
    if not valid_model_id(slug, chosen):
        raise ModelChoiceError(REASON_NOT_APPROVED, _NOT_APPROVED)
    if not text_approved(slug, chosen):
        log.info("Creative text model refused by the allow list (connector %s)", slug)
        raise ModelChoiceError(REASON_NOT_APPROVED, _NOT_APPROVED)

    try:
        provider = provider_from_connector(
            slug, dict(connectors), names=names, model_override=chosen
        )
    except LLMError as exc:
        log_failure(log, "Creative text model could not be prepared", exc, connector=slug)
        raise ModelChoiceError(REASON_NOT_READY, _not_ready(label)) from exc
    return TextChoice(connector=slug, model=chosen, provider=provider)


def resolve_image(
    connectors: Mapping[str, BaseConnector],
    choice: str,
    model: str | None = None,
) -> ImageChoice:
    """The image connector and model to use, or :class:`ModelChoiceError`.

    ``"none"`` (no AI image) is always allowed, whatever the allow lists say.
    """
    slug = _clean_connector(choice)
    if slug == NO_IMAGE:
        return ImageChoice(connector="", model="")
    wanted = _clean_model(model, slug)
    label = IMAGE_LABELS.get(slug)
    if label is None:
        raise ModelChoiceError(REASON_UNKNOWN, _UNKNOWN)
    if not settings.creative_allowed_image_models:
        raise ModelChoiceError(REASON_NEEDS_APPROVAL, NEEDS_APPROVAL)

    instance = connectors.get(slug)
    if instance is None or not instance.supports(Capability.GENERATE_IMAGE):
        raise ModelChoiceError(REASON_NOT_CONNECTED, _not_connected(label))

    chosen = wanted if wanted is not None else _saved(instance, _IMAGE_MODEL_FIELD)
    if not chosen:
        raise ModelChoiceError(REASON_NO_MODEL, _NO_MODEL_CHOSEN)
    if not valid_model_id(slug, chosen):
        raise ModelChoiceError(REASON_NOT_APPROVED, _NOT_APPROVED)
    if not image_approved(slug, chosen):
        log.info("Creative image model refused by the allow list (connector %s)", slug)
        raise ModelChoiceError(REASON_NOT_APPROVED, _NOT_APPROVED)
    return ImageChoice(connector=slug, model=chosen)


__all__ = [
    "IMAGE_LABELS",
    "NEEDS_APPROVAL",
    "NO_IMAGE",
    "NO_IMAGE_LABEL",
    "TEXT_LABELS",
    "AdvancedChoice",
    "ImageChoice",
    "ModelChoiceError",
    "ModelOption",
    "ModelPicker",
    "TextChoice",
    "build_picker",
    "image_approved",
    "is_approved",
    "matches_any",
    "picker_for",
    "resolve_image",
    "resolve_text",
    "text_approved",
]
