"""Versions of settings and content people edit: brand kit, product, agent
settings, workspace policy and creative text (the brand profile keeps its own
module, ``brand``, and reuses the comparison here).

A version's document is whatever the owning service handed to
``history.record_version``. It is stored whole, encrypted under the
workspace's key (``Ctx.HISTORY_PAYLOAD``), because it holds the customer's
own words and restoring needs all of it. Only the structural fields (colours,
fonts, file ids, switches, numbers, choices) are copied into ``inputs``.

Comparing two versions reads only the fields listed below, each with a plain
label and a rule for showing its value, so a person never sees raw JSON.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import InvalidInputError
from app.models.history import SnapshotKind
from app.services.history import store

#: Largest document one version may hold, as compact JSON.
MAX_DOCUMENT_BYTES = 64_000
#: Longest value shown for one field in a version's detail.
SHOWN_TEXT_LIMIT = 2000
#: Items of a list field shown before "and N more".
SHOWN_LIST_LIMIT = 20

TEXT, LIST, COLOUR, FONT, BOOL, NUMBER, FILE, FILES, CHOICE, OPTIONS = (
    "text", "list", "colour", "font", "bool", "number", "file", "files", "choice", "options",
)
#: Field types whose values are settings rather than the customer's words, so
#: they may sit in plaintext ``inputs``.
STRUCTURAL_TYPES = frozenset({COLOUR, FONT, BOOL, NUMBER, FILE, FILES, CHOICE})

#: "Review before publishing" choices, with the words people see.
GUARDRAIL_CHOICES: dict[str, str] = {
    "full": "Publish automatically",
    "hybrid": "Ask me for big changes",
    "human": "Always ask me",
}
PUBLISH_AUTOMATICALLY = "full"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    #: Where the value lives in the document; dots for nesting ("copy.headline").
    key: str
    label: str
    type: str = TEXT
    #: For ``choice``: stored value -> the words shown.
    choices: tuple[tuple[str, str], ...] = ()

    @property
    def structural(self) -> bool:
        return self.type in STRUCTURAL_TYPES


_GUARDRAIL = tuple(GUARDRAIL_CHOICES.items())

FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    SnapshotKind.BRAND_KIT: (
        FieldSpec("name", "Kit name"),
        FieldSpec("is_default", "Default kit", BOOL),
        FieldSpec("logo_file_id", "Logo", FILE),
        FieldSpec("logo_dark_file_id", "Logo for dark backgrounds", FILE),
        FieldSpec("primary_color", "Main colour", COLOUR),
        FieldSpec("secondary_color", "Second colour", COLOUR),
        FieldSpec("accent_color", "Accent colour", COLOUR),
        FieldSpec("text_color", "Text colour", COLOUR),
        FieldSpec("background_color", "Background colour", COLOUR),
        FieldSpec("heading_font", "Heading font", FONT),
        FieldSpec("body_font", "Body font", FONT),
        FieldSpec("voice", "Brand voice"),
        FieldSpec("banned_phrases", "Words to avoid", LIST),
        FieldSpec("disclaimer", "Disclaimer"),
    ),
    SnapshotKind.PRODUCT: (
        FieldSpec("name", "Product name"),
        FieldSpec("description", "Description"),
        FieldSpec("url", "Web page"),
        FieldSpec("price_text", "Price"),
        FieldSpec("approved_claims", "Approved claims", LIST),
        FieldSpec("image_file_ids", "Product photos", FILES),
        FieldSpec("disclaimer", "Disclaimer"),
        FieldSpec("is_archived", "Archived", BOOL),
    ),
    SnapshotKind.AGENT_CONFIG: (
        FieldSpec("schedule", "How often it runs", CHOICE),
        FieldSpec("scope", "What it works on"),
        FieldSpec("notify_channel", "Where it sends updates", CHOICE),
        FieldSpec("llm_connector", "AI model connection", CHOICE),
        FieldSpec("max_actions_per_day", "Most actions per day", NUMBER),
        FieldSpec("autonomy", "Acts without asking", BOOL),
        FieldSpec("settings", "Other settings", OPTIONS),
    ),
    SnapshotKind.WORKSPACE_POLICY: (
        FieldSpec("guardrail", "Review before publishing", CHOICE, _GUARDRAIL),
        FieldSpec("autonomous", "Agents act on their own", BOOL),
    ),
    SnapshotKind.CREATIVE_TEXT: (
        FieldSpec("format_id", "Format", CHOICE),
        FieldSpec("copy.headline", "Headline"),
        FieldSpec("copy.body", "Main text"),
        FieldSpec("copy.cta", "Button text"),
        FieldSpec("copy.claim_ids", "Claims used", FILES),
    ),
}

#: How a comparison counts changes for each kind: (one, many).
NOUNS: dict[str, tuple[str, str]] = {
    SnapshotKind.BRAND_KIT: ("setting", "settings"),
    SnapshotKind.PRODUCT: ("detail", "details"),
    SnapshotKind.AGENT_CONFIG: ("setting", "settings"),
    SnapshotKind.WORKSPACE_POLICY: ("setting", "settings"),
    SnapshotKind.CREATIVE_TEXT: ("part", "parts"),
    SnapshotKind.BRAND_PROFILE: ("field", "fields"),
}


def fields_for(kind: str) -> tuple[FieldSpec, ...]:
    if kind == SnapshotKind.BRAND_PROFILE:
        from app.services.history import brand

        return tuple(
            FieldSpec(key, label, LIST if key in brand.LIST_FIELDS else TEXT)
            for key, label in brand.FIELD_LABELS.items()
        )
    if kind not in FIELDS:
        raise ValueError(f"No field list for history kind {kind!r}")
    return FIELDS[kind]


# ── Reading values ──────────────────────────────────────────────────────────
def get_value(document: dict[str, Any] | None, key: str) -> Any:  # noqa: ANN401 - stored JSON
    value: Any = document or {}
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _humanise(value: str) -> str:
    words = str(value).replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


def normalise(spec: FieldSpec, value: Any) -> Any:  # noqa: ANN401 - stored JSON
    """The comparable form of one value."""
    if spec.type in (LIST, FILES):
        items = value if isinstance(value, list) else ([] if value in (None, "") else [value])
        return [str(v) for v in items]
    if spec.type == BOOL:
        return bool(value)
    if spec.type == NUMBER:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if spec.type == COLOUR:
        return str(value or "").strip().lower()
    if spec.type == OPTIONS:
        return value if isinstance(value, dict) else {}
    if spec.type == FILE:
        return str(value) if value else ""
    return "" if value is None else str(value)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _scalar_text(value: Any) -> str:  # noqa: ANN401 - stored JSON
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None or value == "":
        return "Not set"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value[:SHOWN_LIST_LIMIT]) or "None"
    if isinstance(value, dict):
        return "Set"
    return str(value)


def display(spec: FieldSpec, value: Any) -> str:  # noqa: ANN401 - stored JSON
    """One value in plain words."""
    norm = normalise(spec, value)
    if spec.type == BOOL:
        return "Yes" if norm else "No"
    if spec.type == NUMBER:
        return "Not set" if norm is None else str(norm)
    if spec.type == COLOUR:
        return norm.upper() if norm else "Not set"
    if spec.type == FONT:
        return " ".join(w.capitalize() for w in str(norm).split("_")) if norm else "Not set"
    if spec.type == FILE:
        return "Uploaded" if norm else "Not set"
    if spec.type == FILES:
        return _plural(len(norm), "item", "items") if norm else "None"
    if spec.type == CHOICE:
        if not norm:
            return "Not set"
        return dict(spec.choices).get(norm) or _humanise(norm)
    if spec.type == LIST:
        if not norm:
            return "None"
        shown = ", ".join(norm[:SHOWN_LIST_LIMIT])
        more = len(norm) - SHOWN_LIST_LIMIT
        return shown + (f" and {more} more" if more > 0 else "")
    if spec.type == OPTIONS:
        return _plural(len(norm), "option set", "options set") if norm else "None"
    return norm[:SHOWN_TEXT_LIMIT] if norm else "Not set"


# ── Documents ───────────────────────────────────────────────────────────────
def document_of(payload: Any) -> dict[str, Any]:  # noqa: ANN401 - caller's document
    """Check a document is a JSON object of a bounded size, and return a copy."""
    if not isinstance(payload, dict):
        raise InvalidInputError("This version could not be kept: it is not in the expected form.")
    try:
        raw = json.dumps(payload, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise InvalidInputError("This version could not be kept: it is not in the expected form.") from exc
    if len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise InvalidInputError("This version is too large to keep in the history.")
    return json.loads(raw)


def snapshot(kind: str, payload: Any) -> store.Snapshot:  # noqa: ANN401 - caller's document
    document = document_of(payload)
    specs = fields_for(kind)
    inputs = {
        spec.key: normalise(spec, get_value(document, spec.key)) for spec in specs if spec.structural
    }
    filled = sum(1 for spec in specs if normalise(spec, get_value(document, spec.key)) not in ("", [], {}, None))
    return store.Snapshot(
        metrics={"fields_set": filled, "fields_total": len(specs)},
        items={},
        item_count=0,
        inputs=inputs,
        secret=document,
    )


def same(kind: str, a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """True when every listed field is equal."""
    return all(
        normalise(spec, get_value(a, spec.key)) == normalise(spec, get_value(b, spec.key))
        for spec in fields_for(kind)
    )


def fields_view(kind: str, document: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every listed field of one version, in plain words."""
    return [
        {"field": spec.key, "label": spec.label, "value": display(spec, get_value(document, spec.key))}
        for spec in fields_for(kind)
    ]


def changed_labels(kind: str, now: dict[str, Any] | None, before: dict[str, Any] | None) -> list[str]:
    return [
        spec.label
        for spec in fields_for(kind)
        if normalise(spec, get_value(now, spec.key)) != normalise(spec, get_value(before, spec.key))
    ]


def _option_changes(spec: FieldSpec, before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(set(before) | set(after), key=str):
        if before.get(key) != after.get(key):
            rows.append(
                store.change_row(
                    f"{spec.label}: {_humanise(str(key))}",
                    before=_scalar_text(before.get(key)) if key in before else "Not set",
                    after=_scalar_text(after.get(key)) if key in after else "Not set",
                )
            )
    return rows


def compare(kind: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Which listed fields differ between two versions, with both values in words."""
    changed: list[dict[str, Any]] = []
    unchanged = 0
    for spec in fields_for(kind):
        a = normalise(spec, get_value(before, spec.key))
        b = normalise(spec, get_value(after, spec.key))
        if a == b:
            unchanged += 1
            continue
        if spec.type == OPTIONS:
            changed.extend(_option_changes(spec, a, b))
            continue
        shown_before = display(spec, a)
        shown_after = display(spec, b)
        if shown_before == shown_after:
            # Same count of photos or claims, but different ones.
            shown_after += " (replaced)"
        changed.append(store.change_row(spec.label, before=shown_before, after=shown_after))
    return store.comparison(noun=NOUNS.get(kind, ("setting", "settings")), changed=changed, unchanged=unchanged)


# ── Workspace policy ────────────────────────────────────────────────────────
def guardrail_of(document: dict[str, Any] | None) -> str:
    value = str(get_value(document, "guardrail") or "")
    return value if value in GUARDRAIL_CHOICES else ""


def needs_confirmation(current: dict[str, Any] | None, target: dict[str, Any] | None) -> bool:
    """True when restoring ``target`` would switch the workspace to "Publish automatically"."""
    return guardrail_of(target) == PUBLISH_AUTOMATICALLY and guardrail_of(current) != PUBLISH_AUTOMATICALLY


def current_policy(db: Session, org: Any) -> dict[str, Any]:  # noqa: ANN401 - an Organization
    """The workspace policy in force now, read from its own tables."""
    from app.models.workspace import OnboardingState

    guardrail = db.scalar(select(OnboardingState.guardrail).where(OnboardingState.tenant_id == org.id))
    return {
        "guardrail": guardrail if guardrail in GUARDRAIL_CHOICES else "hybrid",
        "autonomous": bool(getattr(org, "global_autonomy", False)),
    }


CONFIRM_PUBLISH_AUTOMATICALLY = (
    'Restoring this version switches your workspace to "Publish automatically": agents will publish '
    "changes without asking you. Confirm to continue."
)

