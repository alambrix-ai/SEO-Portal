"""Storage for versions: record, number, keep, prune, read — and never fail a run.

Everything kind-specific (what the metrics and items are, how two versions
compare) lives in the sibling modules; this one only knows the table, plus
the shape every comparison is returned in (see "Comparison shape" below).

Numbering: each (tenant, kind, subject) has versions 1, 2, 3 … with no gaps.
:func:`record` takes a transaction-scoped advisory lock on that triple before
reading the highest number, so two writers cannot both take the same one. The
database would also number a row inserted without one (migration 0021), but
that trigger alone would let two concurrent inserts collide on the unique key.

Retention: per tenant and kind, in ``Organization.settings["history_retention"]``
(``keep_versions`` and ``keep_days``), defaulting from config. The newest
``history_min_versions_kept`` versions of a subject are never removed. Every
prune writes an audit entry with counts only.
"""
from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import InvalidInputError, NotFoundError
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.models.history import AuditSnapshot, SnapshotActor

log = get_logger(__name__)

T = TypeVar("T")

#: Per-item state kept in one snapshot. Beyond this the items are cut (worst
#: first is kept, see each builder) and ``item_count`` records the true size,
#: so a diff can say it is incomplete rather than pretend to be exact.
MAX_ITEMS = 5000

#: Rows listed per group ("new", "fixed", …) in a comparison; counts are always complete.
COMPARE_LIST_LIMIT = 200

BASELINE_LABEL = "Baseline (current state when history started)"

#: Feature name for ``entitlements.feature_policy``.
FEATURE = "audit_history"

#: ``Organization.settings`` key holding the per-kind retention overrides.
RETENTION_SETTING = "history_retention"
MAX_KEEP_VERSIONS = 10_000
MAX_KEEP_DAYS = 3650

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext(:k))")

_BAD_CURSOR = "That page of history is no longer available. Please reload the list."
CURSOR_MAX_LENGTH = 200

#: Plain names used in audit entries ("recorded version 3 of the brand kit").
KIND_NAMES: dict[str, str] = {
    "technical_seo": "technical SEO audit",
    "seo_scores": "SEO and AEO page scores",
    "brand_profile": "brand profile",
    "site_analysis": "site analysis",
    "backlink_scan": "backlink scan",
    "competitor_scan": "competitor scan",
    "brand_kit": "brand kit",
    "product": "product",
    "agent_config": "agent settings",
    "workspace_policy": "workspace policy",
    "creative_text": "creative text",
    "local_ranks": "local rankings",
}


def kind_name(kind: str) -> str:
    return KIND_NAMES.get(kind, kind.replace("_", " "))


# ── Pure helpers ────────────────────────────────────────────────────────────
def cap_items(pairs: Iterable[tuple[str, Any]], limit: int = MAX_ITEMS) -> tuple[dict[str, Any], int]:
    """The first ``limit`` pairs as a dict, plus how many there were in all.

    The caller orders ``pairs`` most-important first, so a cut drops the
    least useful items.
    """
    kept: dict[str, Any] = {}
    total = 0
    for key, value in pairs:
        total += 1
        if len(kept) < limit:
            kept[key] = value
    return kept, total


def ids_to_prune(ids_newest_first: list[str], keep: int) -> list[str]:
    """The ids beyond the newest ``keep``. ``keep`` below 1 keeps everything."""
    if keep < 1:
        return []
    return ids_newest_first[keep:]


def change(now: float | int | None, before: float | int | None) -> float | int | None:
    if now is None or before is None:
        return None
    diff = now - before
    return round(diff, 1) if isinstance(diff, float) else diff


def lock_key(tenant_id: str, kind: str, subject_id: str) -> str:
    """The advisory-lock key for one subject's version numbers."""
    return f"history:{tenant_id}:{kind}:{subject_id}"


def guarded(db: Session, label: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T | None:
    """Run ``fn`` in a savepoint; on any error, roll back only that and log.

    History is a record of the work, never a condition of it: a missing table
    (migration not yet run), a bad row or a bug here must not undo or fail
    the run or save it describes.
    """
    try:
        with db.begin_nested():
            return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        log_failure(log, "Could not record history; carrying on", exc, what=label)
        return None


# ── Retention policy ────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Retention:
    keep_versions: int
    #: 0 keeps versions for ever.
    keep_days: int
    #: True when the workspace set this kind itself rather than using the default.
    custom: bool = False


def _bounded_int(value: Any, low: int, high: int) -> int | None:  # noqa: ANN401 - stored JSON
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def retention_for(org_settings: dict[str, Any] | None, kind: str) -> Retention:
    """The policy for one kind, read defensively from the workspace settings."""
    stored = ((org_settings or {}).get(RETENTION_SETTING) or {})
    entry = stored.get(kind) if isinstance(stored, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    versions = _bounded_int(entry.get("keep_versions"), 1, MAX_KEEP_VERSIONS)
    days = _bounded_int(entry.get("keep_days"), 0, MAX_KEEP_DAYS)
    minimum = settings.history_min_versions_kept
    return Retention(
        keep_versions=max(versions if versions is not None else settings.history_keep_versions_default, minimum),
        keep_days=days if days is not None else settings.history_keep_days_default,
        custom=versions is not None or days is not None,
    )


def retention_doomed(
    rows_newest_first: Sequence[tuple[str, datetime]],
    *,
    keep_versions: int,
    keep_days: int,
    min_kept: int,
    now: datetime,
) -> list[str]:
    """Which versions the policy removes. Pure.

    Beyond ``keep_versions`` (never below ``min_kept``) everything goes; with
    ``keep_days`` set, anything older goes too, except the newest ``min_kept``.
    """
    keep = max(keep_versions, min_kept, 1)
    cutoff = now - timedelta(days=keep_days) if keep_days > 0 else None
    doomed: list[str] = []
    for index, (snapshot_id, created_at) in enumerate(rows_newest_first):
        if index < min_kept:
            continue
        if index >= keep or (cutoff is not None and created_at is not None and created_at < cutoff):
            doomed.append(snapshot_id)
    return doomed


def apply_retention(db: Session, tenant_id: str, kind: str, subject_id: str) -> int:
    """Remove the versions of one subject the workspace's policy no longer keeps."""
    from app.models.workspace import Organization
    from app.services import audit

    org = db.get(Organization, tenant_id)
    policy = retention_for(getattr(org, "settings", None), kind)
    rows = db.execute(
        select(AuditSnapshot.id, AuditSnapshot.created_at)
        .where(
            AuditSnapshot.tenant_id == tenant_id,
            AuditSnapshot.kind == kind,
            AuditSnapshot.subject_id == subject_id,
        )
        .order_by(AuditSnapshot.version.desc())
    ).all()
    doomed = retention_doomed(
        [(r[0], r[1]) for r in rows],
        keep_versions=policy.keep_versions,
        keep_days=policy.keep_days,
        min_kept=settings.history_min_versions_kept,
        now=utcnow(),
    )
    if not doomed:
        return 0
    db.execute(
        delete(AuditSnapshot)
        .where(AuditSnapshot.tenant_id == tenant_id, AuditSnapshot.id.in_(doomed))
        .execution_options(synchronize_session=False)
    )
    audit.record_system_action(
        db,
        tenant_id=tenant_id,
        action=f"removed {len(doomed)} old version{'s' if len(doomed) != 1 else ''} of the {kind_name(kind)} (history retention)",
        module=_module_of(kind),
        context={"kind": kind, "subject_id": subject_id, "removed": len(doomed), "kept": len(rows) - len(doomed)},
    )
    return len(doomed)


def _module_of(kind: str) -> str:
    from app.services import history

    module = history.KIND_MODULE.get(kind)
    return module.value if module is not None else ""


# ── Writing ─────────────────────────────────────────────────────────────────
def next_version(db: Session, tenant_id: str, kind: str, subject_id: str) -> int:
    highest = db.scalar(
        select(func.max(AuditSnapshot.version)).where(
            AuditSnapshot.tenant_id == tenant_id,
            AuditSnapshot.kind == kind,
            AuditSnapshot.subject_id == subject_id,
        )
    )
    return int(highest or 0) + 1


def _actor_of(created_by: str, actor_type: str, agent_slug: str) -> str:
    if actor_type:
        return actor_type
    if agent_slug:
        return SnapshotActor.AGENT
    return SnapshotActor.USER if created_by else SnapshotActor.SYSTEM


def record(
    db: Session,
    *,
    tenant_id: str,
    kind: str,
    source: str,
    metrics: dict[str, Any],
    items: dict[str, Any],
    item_count: int | None = None,
    run_id: str = "",
    label: str = "",
    created_by: str = "",
    created_by_name: str = "",
    subject_id: str = "",
    inputs: dict[str, Any] | None = None,
    actor_type: str = "",
    agent_slug: str = "",
    model: str = "",
    payload_encrypted: str = "",
) -> AuditSnapshot:
    """Add the next version of one subject, audit it, then apply retention.

    The caller owns the transaction. The advisory lock is released when it
    commits or rolls back, so the number cannot be taken twice.
    """
    from app.services import audit

    subject_id = (subject_id or "")[:64]
    db.execute(_LOCK_SQL, {"k": lock_key(tenant_id, kind, subject_id)})
    version = next_version(db, tenant_id, kind, subject_id)
    actor = _actor_of(created_by, actor_type, agent_slug)
    row = AuditSnapshot(
        tenant_id=tenant_id,
        kind=kind,
        subject_id=subject_id,
        version=version,
        source=source,
        run_id=(run_id or "")[:36],
        label=(label or "")[:200],
        created_by=(created_by or "")[:36],
        created_by_name=(created_by_name or "")[:160],
        actor_type=actor,
        agent_slug=(agent_slug or "")[:64],
        model=(model or "")[:128],
        # Taken after the lock, so creation order matches version order.
        created_at=utcnow(),
        inputs=inputs or {},
        metrics=metrics,
        items=items,
        item_count=len(items) if item_count is None else item_count,
        payload_encrypted=payload_encrypted or "",
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=tenant_id,
        actor=_audit_actor(actor, created_by_name, agent_slug),
        actor_type=actor,
        actor_id=(created_by or None) if actor == SnapshotActor.USER else None,
        action=f"recorded version {version} of the {kind_name(kind)}",
        module=_module_of(kind),
        context={"kind": kind, "subject_id": subject_id, "version": version, "snapshot_id": row.id},
    )
    apply_retention(db, tenant_id, kind, subject_id)
    return row


def _audit_actor(actor: str, created_by_name: str, agent_slug: str) -> str:
    if actor == SnapshotActor.USER and created_by_name:
        return created_by_name[:160]
    if actor == SnapshotActor.AGENT and agent_slug:
        return agent_slug.replace("_", " ").capitalize()[:160]
    return "System"


# ── Reading ─────────────────────────────────────────────────────────────────
def _tenant_kind(tenant_id: str, kind: str) -> Any:  # noqa: ANN401 - a SQL expression
    return and_(AuditSnapshot.tenant_id == tenant_id, AuditSnapshot.kind == kind)


def count(db: Session, tenant_id: str, kind: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(AuditSnapshot)
            .where(AuditSnapshot.tenant_id == tenant_id, AuditSnapshot.kind == kind)
        )
        or 0
    )


def recent(db: Session, tenant_id: str, kind: str, limit: int) -> list[AuditSnapshot]:
    """Newest first."""
    return list(
        db.scalars(
            select(AuditSnapshot)
            .where(AuditSnapshot.tenant_id == tenant_id, AuditSnapshot.kind == kind)
            .order_by(AuditSnapshot.created_at.desc(), AuditSnapshot.id.desc())
            .limit(limit)
        )
    )


def page(
    db: Session,
    tenant_id: str,
    kind: str,
    *,
    subject_id: str | None,
    after: tuple[datetime, str] | None,
    limit: int,
) -> list[AuditSnapshot]:
    """Newest first, starting after the cursor position; ``subject_id`` None means every subject."""
    stmt = select(AuditSnapshot).where(_tenant_kind(tenant_id, kind))
    if subject_id is not None:
        stmt = stmt.where(AuditSnapshot.subject_id == subject_id)
    if after is not None:
        at, last_id = after
        stmt = stmt.where(
            or_(
                AuditSnapshot.created_at < at,
                and_(AuditSnapshot.created_at == at, AuditSnapshot.id < last_id),
            )
        )
    return list(db.scalars(stmt.order_by(AuditSnapshot.created_at.desc(), AuditSnapshot.id.desc()).limit(limit)))


def latest(db: Session, tenant_id: str, kind: str) -> AuditSnapshot | None:
    rows = recent(db, tenant_id, kind, 1)
    return rows[0] if rows else None


def latest_of(db: Session, tenant_id: str, kind: str, subject_id: str) -> AuditSnapshot | None:
    """The highest version of one subject."""
    return db.scalars(
        select(AuditSnapshot)
        .where(_tenant_kind(tenant_id, kind), AuditSnapshot.subject_id == subject_id)
        .order_by(AuditSnapshot.version.desc())
        .limit(1)
    ).first()


def parse_id(value: str) -> str | None:
    """A canonical UUID string, or None: only a UUID ever reaches a primary-key lookup."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def get(db: Session, tenant_id: str, kind: str, snapshot_id: str) -> AuditSnapshot:
    """One version, only if it belongs to this workspace and kind (else 404)."""
    parsed = parse_id(snapshot_id)
    if parsed is None:
        raise NotFoundError("That history entry was not found")
    row = db.scalars(
        select(AuditSnapshot).where(_tenant_kind(tenant_id, kind), AuditSnapshot.id == parsed)
    ).first()
    if row is None or row.tenant_id != tenant_id or row.kind != kind:
        raise NotFoundError("That history entry was not found")
    return row


def previous_of(db: Session, row: AuditSnapshot) -> AuditSnapshot | None:
    """The version just before ``row`` of the same subject."""
    return db.scalars(
        select(AuditSnapshot)
        .where(
            _tenant_kind(row.tenant_id, row.kind),
            AuditSnapshot.subject_id == (row.subject_id or ""),
            AuditSnapshot.version < row.version,
            AuditSnapshot.id != row.id,
        )
        .order_by(AuditSnapshot.version.desc())
        .limit(1)
    ).first()


def first_at(db: Session, tenant_id: str, kind: str) -> Any:  # noqa: ANN401 - datetime | None
    return db.scalar(
        select(func.min(AuditSnapshot.created_at)).where(
            AuditSnapshot.tenant_id == tenant_id, AuditSnapshot.kind == kind
        )
    )


def plan_cap(db: Session, org: Any) -> int | None:  # noqa: ANN401 - an Organization
    """How many versions the plan shows (None: no cap). Raises when history is not in the plan."""
    from app.core.exceptions import ForbiddenError
    from app.services import entitlements

    policy = entitlements.feature_policy(db, org, FEATURE)
    if not policy.allowed:
        raise ForbiddenError(policy.reason or "History is not included in your plan.")
    return policy.max_history


def visible_limit(db: Session, org: Any, requested: int) -> int:  # noqa: ANN401 - an Organization
    """How many entries to show: the request, capped by the plan if it caps it.

    Raises when the plan does not include history at all.
    """
    cap = plan_cap(db, org)
    if cap is not None:
        return max(1, min(requested, cap))
    return requested


# ── Cursors ─────────────────────────────────────────────────────────────────
def encode_cursor(row: AuditSnapshot) -> str:
    raw = f"{row.created_at.isoformat()}|{row.id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Parse a cursor from :func:`encode_cursor`, refusing anything else.

    A cursor is only a position in the caller's own tenant-filtered query, so
    a forged one can do no more than move the page; it is still parsed
    strictly so only a timestamp and a UUID reach the query.
    """
    if not cursor or len(cursor) > CURSOR_MAX_LENGTH:
        raise InvalidInputError(_BAD_CURSOR)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        at_text, _, id_text = raw.partition("|")
        at = datetime.fromisoformat(at_text)
        entry_id = str(uuid.UUID(id_text))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise InvalidInputError(_BAD_CURSOR) from exc
    if at.tzinfo is None:
        raise InvalidInputError(_BAD_CURSOR)
    return at, entry_id


# ── Entries ─────────────────────────────────────────────────────────────────
def source_label(row: AuditSnapshot) -> str:
    return row.label or {
        "baseline": BASELINE_LABEL,
        "agent_run": "Agent run",
        "generated": "Created from your website",
        "edited": "Edited",
        "restored": "Restored",
    }.get(row.source, row.source)


def base_entry(row: AuditSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "version": getattr(row, "version", None),
        "subject_id": getattr(row, "subject_id", "") or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "source": row.source,
        "label": source_label(row),
        "created_by": row.created_by_name or None,
        "actor_type": getattr(row, "actor_type", "") or None,
        "agent_slug": getattr(row, "agent_slug", "") or None,
        "model": getattr(row, "model", "") or None,
        "run_id": row.run_id or None,
        "truncated": row.item_count > len(row.items or {}),
    }


def version_ref(row: AuditSnapshot) -> dict[str, Any]:
    """The short reference to one side of a comparison."""
    return {
        "id": row.id,
        "version": getattr(row, "version", None),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "label": source_label(row),
    }


# ── Snapshots handed to ``record`` by kind adapters ─────────────────────────
@dataclass(slots=True)
class Snapshot:
    """What one version stores, as built by a kind's adapter."""

    metrics: dict[str, Any] = field(default_factory=dict)
    items: dict[str, Any] = field(default_factory=dict)
    item_count: int | None = None
    #: Structural only: ids, options, counts. Never the customer's words.
    inputs: dict[str, Any] = field(default_factory=dict)
    #: The customer's own content, stored encrypted; None when there is none.
    secret: dict[str, Any] | None = None


# ── Comparison shape ────────────────────────────────────────────────────────
# Every kind compares two versions into the same structure, so one screen can
# show any of them in plain words:
#
#   {"counts": {"new", "fixed", "worse", "changed", "unchanged"},
#    "new": [Change], "fixed": [Change], "worse": [Change], "changed": [Change],
#    "measures": [Measure], "summary": str, "truncated": bool}
#
#   Change  = {"label", "where", "before", "after"}: short plain strings or None.
#   Measure = {"label", "before", "after", "change", "better", "verdict"}:
#             a headline number; ``better`` says which direction is good
#             ("higher" / "lower"), ``verdict`` is "better" / "worse" / "same".
def change_row(label: str, *, where: str | None = None, before: Any = None, after: Any = None) -> dict[str, Any]:  # noqa: ANN401
    def text_of(value: Any) -> str | None:  # noqa: ANN401
        if value is None:
            return None
        return str(value)[:300]

    return {
        "label": str(label)[:200],
        "where": (str(where)[:300] if where else None),
        "before": text_of(before),
        "after": text_of(after),
    }


def measure(label: str, before: float | int | None, after: float | int | None, *, better: str | None) -> dict[str, Any]:
    diff = change(after, before)
    verdict: str | None = None
    if diff is not None:
        if diff == 0:
            verdict = "same"
        elif better in ("higher", "lower"):
            verdict = "better" if (diff > 0) == (better == "higher") else "worse"
    return {"label": label, "before": before, "after": after, "change": diff, "better": better, "verdict": verdict}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary_text(counts: dict[str, int], *, noun: tuple[str, str], fixed_word: str = "fixed") -> str:
    """One plain sentence, e.g. "2 new issues, 5 fixed and 1 got worse; 40 unchanged."."""
    parts: list[str] = []
    if counts.get("new"):
        parts.append(f"{_plural(counts['new'], 'new ' + noun[0], 'new ' + noun[1])}")
    if counts.get("fixed"):
        parts.append(f"{counts['fixed']} {fixed_word}")
    if counts.get("worse"):
        parts.append(f"{counts['worse']} got worse")
    if counts.get("changed"):
        parts.append(f"{_plural(counts['changed'], noun[0], noun[1])} changed")
    if not parts:
        return "Nothing changed between these two versions."
    sentence = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    sentence = sentence[0].upper() + sentence[1:]
    if counts.get("unchanged"):
        sentence += f"; {counts['unchanged']} unchanged"
    return sentence + "."


def comparison(
    *,
    noun: tuple[str, str],
    new: Sequence[dict[str, Any]] = (),
    fixed: Sequence[dict[str, Any]] = (),
    worse: Sequence[dict[str, Any]] = (),
    changed: Sequence[dict[str, Any]] = (),
    unchanged: int = 0,
    measures: Sequence[dict[str, Any]] = (),
    truncated: bool = False,
    fixed_word: str = "fixed",
) -> dict[str, Any]:
    counts = {
        "new": len(new),
        "fixed": len(fixed),
        "worse": len(worse),
        "changed": len(changed),
        "unchanged": max(int(unchanged), 0),
    }
    limit = COMPARE_LIST_LIMIT
    return {
        "counts": counts,
        "new": list(new)[:limit],
        "fixed": list(fixed)[:limit],
        "worse": list(worse)[:limit],
        "changed": list(changed)[:limit],
        "measures": list(measures),
        "summary": summary_text(counts, noun=noun, fixed_word=fixed_word),
        "truncated": bool(truncated),
    }
