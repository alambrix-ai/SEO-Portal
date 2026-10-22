"""The vendor's own list of models, read live with the customer's key.

No model name is written down anywhere in this codebase. Which models exist,
what they are called and which one is newest all come from the vendor's
official models endpoint, called with the key the customer connected:

* OpenAI     ``GET /v1/models``
  https://developers.openai.com/api/reference/resources/models/methods/list
  Fields: ``id``, ``created`` (unix seconds), ``owned_by``. No capability
  metadata, so nothing here guesses which ids write text or make images.
* Anthropic  ``GET /v1/models`` (paginated: ``has_more`` / ``last_id`` →
  ``after_id``)
  https://platform.claude.com/docs/en/api/models/list
  Fields: ``id``, ``display_name``, ``created_at`` (RFC 3339), ``lifecycle``,
  ``capabilities`` (thinking types, effort levels, …).
* Gemini     ``GET /v1beta/models`` (paginated: ``nextPageToken`` →
  ``pageToken``)
  https://ai.google.dev/api/models
  Fields: ``name`` ("models/…"), ``displayName``,
  ``supportedGenerationMethods``, ``thinking``. No creation time and no
  output-modality field.
* xAI        ``GET /v1/models``, the OpenAI shape (:func:`parse_openai`).
* vLLM and Ollama (self-hosted) ``GET {base}/v1/models``, the OpenAI shape
  from a server the customer runs (:func:`parse_self_hosted`).

Results are cached per key for :data:`CACHE_SECONDS`, so opening the connect
dialog twice does not call the vendor twice. The cache is keyed on a hash of
the key, never the key itself.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: How long a model list is reused before it is read again.
CACHE_SECONDS = 600

#: Kinds a model can be offered for in the connect dialog.
TEXT = "text"
IMAGE = "image"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One model, as the vendor describes it."""

    id: str
    label: str
    #: Unix seconds from the vendor's own field, or None when it gives none.
    created: int | None = None
    #: What this model may be chosen for: ``text``, ``image``, or both when
    #: the vendor publishes no capability metadata to tell them apart.
    kinds: tuple[str, ...] = (TEXT,)
    #: True when ``kinds`` comes from vendor metadata rather than "unknown".
    kinds_known: bool = False
    #: The vendor's capability block, where it publishes one (Anthropic).
    capabilities: dict[str, Any] = field(default_factory=dict, compare=False)
    #: Gemini's ``thinking`` flag; None when the vendor does not say.
    thinking: bool | None = None


class ModelListUnavailable(Exception):
    """The vendor publishes no models endpoint for this API."""


# ── Parsing (pure; tested without a network) ───────────────────────────────
def parse_openai(payload: Any) -> list[ModelInfo]:  # noqa: ANN401
    """``GET /v1/models`` → models. No capability metadata exists, so every
    model is offered for both text and images, and nothing is filtered."""
    out: list[ModelInfo] = []
    for item in (payload or {}).get("data") or []:
        model_id = str((item or {}).get("id") or "").strip()
        if not model_id:
            continue
        created = item.get("created")
        out.append(
            ModelInfo(
                id=model_id,
                label=model_id,
                created=int(created) if isinstance(created, int | float) and created > 0 else None,
                kinds=(TEXT, IMAGE),
                kinds_known=False,
            )
        )
    return newest_first(out)


#: Most models read from a self-hosted server, and the longest id accepted.
SELF_HOSTED_MAX_MODELS = 500
_SELF_HOSTED_MAX_ID = 200


def parse_self_hosted(payload: Any) -> list[ModelInfo]:  # noqa: ANN401
    """``GET {base}/v1/models`` on vLLM or Ollama → models.

    The reply comes from the customer's own server, so it is read
    defensively: anything but a list of objects is ignored, ids that are
    blank, over-long or carry control characters are dropped, repeats are
    dropped, and at most :data:`SELF_HOSTED_MAX_MODELS` are kept. These
    servers only write text here, so every model is offered for text.
    """
    body = payload if isinstance(payload, dict) else {}
    items = body.get("data")
    if not isinstance(items, list):
        return []
    out: list[ModelInfo] = []
    seen: set[str] = set()
    for item in items:
        if len(out) >= SELF_HOSTED_MAX_MODELS:
            break
        if not isinstance(item, dict):
            continue
        raw = item.get("id")
        model_id = raw.strip() if isinstance(raw, str) else ""
        if (
            not model_id
            or len(model_id) > _SELF_HOSTED_MAX_ID
            or not model_id.isprintable()
            or model_id in seen
        ):
            continue
        seen.add(model_id)
        created = item.get("created")
        out.append(
            ModelInfo(
                id=model_id,
                label=model_id,
                created=(
                    int(created)
                    if isinstance(created, int | float) and not isinstance(created, bool) and created > 0
                    else None
                ),
                kinds=(TEXT,),
                kinds_known=False,
            )
        )
    return newest_first(out)


def _rfc3339_seconds(value: Any) -> int | None:  # noqa: ANN401
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = int(parsed.timestamp())
    # The docs: "May be set to an epoch value if the release date is unknown."
    return seconds if seconds > 0 else None


def parse_anthropic_page(payload: Any) -> tuple[list[ModelInfo], str]:  # noqa: ANN401
    """One page of ``GET /v1/models`` → ``(models, next after_id or "")``.

    Retired models are left out (requests naming them fail). Every model on
    this API writes text; none makes images.
    """
    body = payload or {}
    out: list[ModelInfo] = []
    for item in body.get("data") or []:
        model_id = str((item or {}).get("id") or "").strip()
        if not model_id or item.get("lifecycle") == "retired":
            continue
        out.append(
            ModelInfo(
                id=model_id,
                label=str(item.get("display_name") or model_id),
                created=_rfc3339_seconds(item.get("created_at")),
                kinds=(TEXT,),
                kinds_known=True,
                capabilities=dict(item.get("capabilities") or {}),
            )
        )
    after = str(body.get("last_id") or "") if body.get("has_more") else ""
    return out, after


def parse_gemini_page(payload: Any) -> tuple[list[ModelInfo], str]:  # noqa: ANN401
    """One page of ``GET /v1beta/models`` → ``(models, nextPageToken or "")``.

    Only models whose ``supportedGenerationMethods`` include
    ``generateContent`` are kept: that is the method both writing and image
    creation call here. Gemini publishes no output-modality field, so a
    generateContent model is offered for both text and images.
    """
    body = payload or {}
    out: list[ModelInfo] = []
    for item in body.get("models") or []:
        name = str((item or {}).get("name") or "").strip()
        methods = item.get("supportedGenerationMethods") or []
        if not name or "generateContent" not in methods:
            continue
        model_id = name.removeprefix("models/")
        thinking = item.get("thinking")
        out.append(
            ModelInfo(
                id=model_id,
                label=str(item.get("displayName") or model_id),
                created=None,
                kinds=(TEXT, IMAGE),
                kinds_known=False,
                thinking=thinking if isinstance(thinking, bool) else None,
            )
        )
    return out, str(body.get("nextPageToken") or "")


def newest_first(models: list[ModelInfo]) -> list[ModelInfo]:
    """Newest first by the vendor's timestamp; undated ones after, by name."""
    dated = sorted((m for m in models if m.created), key=lambda m: (-(m.created or 0), m.id))
    undated = sorted((m for m in models if not m.created), key=lambda m: m.label.lower())
    return [*dated, *undated]


def suggested_text_model(models: list[ModelInfo]) -> str:
    """The vendor's newest text model by its own ``created`` time, or "".

    Only where the vendor says which models write text (``kinds_known``) and
    when they were released. Otherwise nothing is pre-selected and the person
    chooses: the newest id on an unlabelled list could be a speech or image
    model.
    """
    candidates = [m for m in models if m.kinds_known and TEXT in m.kinds and m.created]
    if not candidates:
        return ""
    return max(candidates, key=lambda m: (m.created or 0, m.id)).id


def paginate(
    fetch: Callable[[str], tuple[list[ModelInfo], str]], *, max_pages: int = 50
) -> list[ModelInfo]:
    """Follow a vendor's cursor until it stops (with a ceiling, not a loop forever)."""
    out: list[ModelInfo] = []
    cursor = ""
    seen: set[str] = set()
    for _ in range(max_pages):
        page, cursor = fetch(cursor)
        out.extend(page)
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    unique: dict[str, ModelInfo] = {}
    for model in out:
        unique.setdefault(model.id, model)
    return list(unique.values())


# ── Cache ──────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[float, list[ModelInfo]]] = {}


def cache_key(vendor: str, base_url: str, api_key: str) -> tuple[str, str, str]:
    digest = hashlib.sha256((api_key or "").encode()).hexdigest()
    return vendor, base_url.rstrip("/"), digest


def cached(
    key: tuple[str, str, str], load: Callable[[], list[ModelInfo]]
) -> list[ModelInfo]:
    """``load()`` once per :data:`CACHE_SECONDS` for this key. Failures are not cached."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    models = load()
    with _lock:
        _cache[key] = (time.monotonic(), models)
    return models


def find(models: list[ModelInfo], model_id: str) -> ModelInfo | None:
    wanted = (model_id or "").strip()
    return next((m for m in models if m.id == wanted), None)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# ── Plain-language messages, shared by the card, the dialog and the runs ───
def stale_model_message(model: str, vendor: str) -> str:
    return f"Choose a model — your saved model {model} is no longer offered by {vendor}."


def stale_image_model_message(model: str, vendor: str) -> str:
    return (
        f"Choose an image model — your saved image model {model} is no longer "
        f"offered by {vendor}."
    )


def no_image_model_message(vendor: str) -> str:
    return f"Choose an image model in your {vendor} connection to create images."
