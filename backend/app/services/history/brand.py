"""Brand profile versions: one on every generate, save and restore.

A version's ``items`` is the whole profile as it was stored, so it can be
compared with the current one field by field and restored exactly. Restoring
writes that profile back and records a *new* version, so the version being
replaced is still in the list — nothing is lost by restoring.

A save that changes none of the profile's fields (only the time zone, say)
does not add a version: the list would fill with identical entries.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.db.base import utcnow
from app.models.history import AuditSnapshot, SnapshotKind, SnapshotSource
from app.models.workspace import Organization
from app.services import brand_profile
from app.services.history import store

KIND = SnapshotKind.BRAND_PROFILE

FIELD_LABELS: dict[str, str] = {
    "business_name": "Business name",
    "summary": "What the business does",
    "industry": "Industry",
    "tone": "Tone of voice",
    "language": "Language",
    "call_to_action": "Main call to action",
    "products_services": "Products and services",
    "audiences": "Who you sell to",
    "value_propositions": "Why customers choose you",
    "differentiators": "What makes you different",
    "keywords": "What customers search for",
    "locations": "Areas you serve",
    "competitors": "Competitors",
    "source_url": "Website",
}
#: Fields compared and shown; bookkeeping (generated_at, edited, …) is not content.
FIELDS = tuple(FIELD_LABELS)
LIST_FIELDS = frozenset(brand_profile.LIST_FIELDS)


# ── Pure ────────────────────────────────────────────────────────────────────
def content(profile: dict[str, Any] | None) -> dict[str, Any]:
    profile = profile or {}
    out: dict[str, Any] = {}
    for key in FIELDS:
        if key in LIST_FIELDS:
            value = profile.get(key) or []
            out[key] = [str(v) for v in value] if isinstance(value, list) else [str(value)]
        else:
            out[key] = str(profile.get(key) or "")
    return out


def same(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    return content(a) == content(b)


def field_diff(version: dict[str, Any] | None, current: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every field, this version against the current profile."""
    a, b = content(version), content(current)
    out = []
    for key in FIELDS:
        row: dict[str, Any] = {
            "field": key,
            "label": FIELD_LABELS[key],
            "type": "list" if key in LIST_FIELDS else "text",
            "version": a[key],
            "current": b[key],
            "changed": a[key] != b[key],
        }
        if key in LIST_FIELDS:
            low_a = {s.lower() for s in a[key]}
            low_b = {s.lower() for s in b[key]}
            row["only_in_version"] = [s for s in a[key] if s.lower() not in low_b]
            row["only_in_current"] = [s for s in b[key] if s.lower() not in low_a]
        out.append(row)
    return out


def changed_labels(now: dict[str, Any] | None, before: dict[str, Any] | None) -> list[str]:
    a, b = content(now), content(before)
    return [FIELD_LABELS[k] for k in FIELDS if a[k] != b[k]]


def filled(profile: dict[str, Any] | None) -> int:
    return sum(1 for v in content(profile).values() if v)


def restored_profile(version: dict[str, Any], *, version_at: str, now: str) -> dict[str, Any]:
    """The profile to store when restoring ``version``."""
    profile = {k: v for k, v in dict(version).items() if k not in ("restored_from_at", "restored_at")}
    profile.update(edited=True, edited_at=now, restored_at=now, restored_from_at=version_at)
    return profile


def _when(moment: Any) -> str:  # noqa: ANN401
    return moment.strftime("%d %b %Y, %H:%M") if moment else ""


# ── Database ────────────────────────────────────────────────────────────────
def record_version(
    db: Session,
    org: Organization,
    profile: dict[str, Any] | None,
    *,
    source: str,
    label: str = "",
    user: Any = None,  # noqa: ANN401 - a User
    force: bool = False,
) -> AuditSnapshot | None:
    """Keep ``profile`` as a version, unless it is the same as the latest one."""
    if not profile:
        return None
    latest = store.latest(db, org.id, KIND)
    if not force and latest is not None and same(latest.items, profile):
        return None
    return store.record(
        db,
        tenant_id=org.id,
        kind=KIND,
        source=source,
        label=label,
        metrics={"fields_filled": filled(profile), "fields_total": len(FIELDS)},
        items=dict(profile),
        created_by=getattr(user, "id", "") or "",
        created_by_name=getattr(user, "name", "") or "",
    )


def ensure_baseline(db: Session, org: Organization) -> bool:
    if store.count(db, org.id, KIND):
        return False
    profile = brand_profile.get(org)
    if not profile:
        return False
    record_version(db, org, profile, source=SnapshotSource.BASELINE, label=store.BASELINE_LABEL)
    return True


def history(db: Session, org: Organization, limit: int) -> dict[str, Any]:
    rows = store.recent(db, org.id, KIND, limit + 1)
    current = brand_profile.get(org)
    entries = []
    for i, row in enumerate(rows[:limit]):
        previous = rows[i + 1] if i + 1 < len(rows) else None
        entries.append({
            **store.base_entry(row),
            "headline": None,
            "change": None,
            "metrics": row.metrics,
            "changed_fields": changed_labels(row.items, previous.items) if previous else None,
            "is_current": same(row.items, current),
        })
    return {"entries": entries}


def detail(db: Session, org: Organization, row: AuditSnapshot) -> dict[str, Any]:
    previous = store.previous_of(db, row)
    current = brand_profile.get(org)
    return {
        **store.base_entry(row),
        "headline": None,
        "change": None,
        "metrics": row.metrics,
        "profile": content(row.items),
        "is_current": same(row.items, current),
        "diff": field_diff(row.items, current),
        "changed_fields": changed_labels(row.items, previous.items) if previous else None,
        "comparison": None,
    }


def restore(db: Session, org: Organization, row: AuditSnapshot, *, user: Any) -> dict[str, Any]:  # noqa: ANN401
    current = brand_profile.get(org)
    if same(row.items, current):
        raise ConflictError("This version is already the current brand profile.")
    # If the profile being replaced was never kept (saved before history was
    # recorded, or a recording failed), keep it now so restoring loses nothing.
    record_version(db, org, current, source=SnapshotSource.EDITED, label="Profile before a restore")
    profile = restored_profile(
        row.items,
        version_at=row.created_at.isoformat() if row.created_at else "",
        now=utcnow().isoformat(),
    )
    brand_profile.store(org, profile)
    db.flush()
    record_version(
        db, org, profile,
        source=SnapshotSource.RESTORED,
        label=f"Restored the version from {_when(row.created_at)}",
        user=user,
        force=True,
    )
    return profile
