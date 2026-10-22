"""Audit trail writer.

Every consequential action — human or agent — goes through here, so the Admin
screen's audit log and any later compliance export read from one table with one
shape. Request IP is stored encrypted: an audit trail is itself personal data.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.core import csv_safe
from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.db.base import utcnow
from app.models.workspace import AuditLogEntry, User


def record(
    db: Session,
    *,
    tenant_id: str,
    actor: str,
    action: str,
    actor_type: str = "user",
    actor_id: str | None = None,
    module: str = "",
    ip_address: str | None = None,
    context: dict | None = None,
    at: datetime | None = None,
    flush: bool = True,
) -> AuditLogEntry:
    """Append one entry. Caller owns the transaction."""
    entry = AuditLogEntry(
        tenant_id=tenant_id,
        at=at or utcnow(),
        actor=actor,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        module=module,
        ip_address=ip_address or None,
        context=context or {},
    )
    db.add(entry)
    if flush:
        db.flush()
    return entry


def record_user_action(
    db: Session,
    *,
    user: User,
    action: str,
    module: str = "",
    ip_address: str | None = None,
    context: dict | None = None,
) -> AuditLogEntry:
    return record(
        db,
        tenant_id=user.tenant_id,
        actor=user.name,
        actor_id=user.id,
        actor_type="user",
        action=action,
        module=module,
        ip_address=ip_address,
        context=context,
    )


def record_agent_action(
    db: Session,
    *,
    tenant_id: str,
    agent_name: str,
    action: str,
    module: str = "",
    context: dict | None = None,
) -> AuditLogEntry:
    return record(
        db,
        tenant_id=tenant_id,
        actor=agent_name,
        actor_type="agent",
        action=action,
        module=module,
        context=context,
    )


def record_system_action(
    db: Session, *, tenant_id: str, action: str, module: str = "", context: dict | None = None
) -> AuditLogEntry:
    return record(
        db,
        tenant_id=tenant_id,
        actor="System",
        actor_type="system",
        action=action,
        module=module,
        context=context,
    )


def unseen_count(
    db: Session, *, tenant_id: str, since: datetime | None, viewer_id: str, modules: set[str]
) -> int:
    """How many entries this person has not seen yet.

    What counts is what somebody would want to be told: an **agent** did
    something, or **another person** did. Three things therefore do not:

    * **Their own actions.** A dot that appears because you just clicked
      something is noise, and it trains people to ignore the dot.
    * **System bookkeeping.** "Provisioned the workspace with its agent fleet"
      is a true and useful audit row and not news to the person whose sign-up
      caused it. Nobody needs telling about the platform's own housekeeping.
    * **Modules they cannot see.** The panel is filtered by the access map, so
      counting what it will not show would leave a badge that never clears —
      a dot with nothing behind it.

    A user who has never opened the panel has ``since=None``, and everything
    that qualifies counts, which is right on a first visit.
    """
    stmt = select(func.count()).select_from(AuditLogEntry).where(
        AuditLogEntry.tenant_id == tenant_id,
        or_(
            AuditLogEntry.actor_type == "agent",
            and_(
                AuditLogEntry.actor_type == "user",
                or_(
                    AuditLogEntry.actor_id.is_(None),
                    AuditLogEntry.actor_id != viewer_id,
                ),
            ),
        ),
        or_(AuditLogEntry.module == "", AuditLogEntry.module.in_(modules)),
    )
    if since is not None:
        stmt = stmt.where(AuditLogEntry.at > since)
    return db.execute(stmt).scalar_one()


def mark_seen(db: Session, *, user: User, tenant_id: str) -> None:
    """Record that this person has now looked at the panel.

    Marked up to the newest entry that *existed*, not to the clock. The panel
    is fetched by one request and marked read by a second, and an agent runs
    on a schedule: anything written in the gap between the two would have been
    stamped as seen by a wall-clock marker without ever having been displayed.
    Agents here can tick every minute, so that gap is not theoretical.
    """
    newest = db.execute(
        select(func.max(AuditLogEntry.at)).where(AuditLogEntry.tenant_id == tenant_id)
    ).scalar()
    user.notifications_seen_at = newest or utcnow()
    db.flush()


def list_entries(
    db: Session, *, tenant_id: str, search: str = "", limit: int = 200
) -> list[AuditLogEntry]:
    """Most recent first, optionally filtered by actor or action text.

    The filter runs in SQL against the non-encrypted columns; ``actor`` is a
    display name rather than an email precisely so this stays searchable.
    """
    stmt = (
        select(AuditLogEntry)
        .where(AuditLogEntry.tenant_id == tenant_id)
        .order_by(desc(AuditLogEntry.at))
        .limit(limit)
    )
    if search:
        needle = f"%{search.lower()}%"
        from sqlalchemy import func, or_

        stmt = stmt.where(
            or_(
                func.lower(AuditLogEntry.actor).like(needle),
                func.lower(AuditLogEntry.action).like(needle),
            )
        )
    return list(db.execute(stmt).scalars())


# ── Export ─────────────────────────────────────────────────────────────────
#: Exported columns, in order. ``ip_address`` is deliberately absent: it is
#: personal data, stored encrypted, and an export leaves the platform's
#: control.
EXPORT_COLUMNS: tuple[str, ...] = (
    "id",
    "at",
    "actor",
    "actor_type",
    "actor_id",
    "action",
    "module",
    "context",
)

#: Longest cursor accepted; a real one is about 90 characters.
CURSOR_MAX_LENGTH = 200

_BAD_CURSOR = "This export page link is no longer valid. Please start the export again."


@dataclass(frozen=True)
class ExportPage:
    rows: list[dict[str, str]]
    next_cursor: str | None


def export_window(start: date, end: date) -> tuple[datetime, datetime]:
    """The UTC window ``[start 00:00, end + 1 day 00:00)``, after checking bounds.

    Both dates are inclusive, matching the date pickers. The range is capped
    by ``audit_export_max_days``, so an export is always a bounded query.
    """
    if end < start:
        raise InvalidInputError("The end date must be on or after the start date.")
    days = (end - start).days + 1
    if days > settings.audit_export_max_days:
        raise InvalidInputError(
            f"Choose a range of {settings.audit_export_max_days} days or fewer."
        )
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


def encode_cursor(at: datetime, entry_id: str) -> str:
    raw = f"{at.isoformat()}|{entry_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Parse a cursor from :func:`encode_cursor`, refusing anything else.

    A cursor is only a position in the caller's own, tenant-filtered and
    date-bounded query, so a forged one can do no more than move the page.
    It is still parsed strictly, so that only a timestamp and a UUID ever
    reach the query.
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


def _export_row(entry: AuditLogEntry) -> dict[str, str]:
    return {
        "id": str(entry.id),
        "at": entry.at.astimezone(UTC).isoformat(),
        "actor": entry.actor,
        "actor_type": entry.actor_type,
        "actor_id": str(entry.actor_id) if entry.actor_id else "",
        "action": entry.action,
        "module": entry.module or "",
        "context": json.dumps(entry.context or {}, ensure_ascii=False, sort_keys=True, default=str),
    }


def export_page(
    db: Session,
    *,
    tenant_id: str,
    start: date,
    end: date,
    cursor: str | None = None,
    limit: int | None = None,
) -> ExportPage:
    """One page of the audit log, oldest first, keyset-paginated on (at, id).

    At most ``audit_export_max_rows`` rows. ``next_cursor`` is set only when
    more rows remain in the window.
    """
    window_start, window_end = export_window(start, end)
    page_size = min(limit or settings.audit_export_max_rows, settings.audit_export_max_rows)
    stmt = (
        select(AuditLogEntry)
        .where(
            AuditLogEntry.tenant_id == tenant_id,
            AuditLogEntry.at >= window_start,
            AuditLogEntry.at < window_end,
        )
        .order_by(AuditLogEntry.at, AuditLogEntry.id)
        .limit(page_size + 1)
    )
    if cursor:
        after_at, after_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                AuditLogEntry.at > after_at,
                and_(AuditLogEntry.at == after_at, AuditLogEntry.id > after_id),
            )
        )
    entries = list(db.execute(stmt).scalars())
    more = len(entries) > page_size
    entries = entries[:page_size]
    next_cursor = encode_cursor(entries[-1].at, str(entries[-1].id)) if more and entries else None
    return ExportPage(rows=[_export_row(e) for e in entries], next_cursor=next_cursor)


def export_csv(rows: Sequence[dict[str, str]]) -> bytes:
    """CSV for people to open in a spreadsheet; every cell is neutralised."""
    return csv_safe.rows_to_csv(
        EXPORT_COLUMNS, ([row.get(col, "") for col in EXPORT_COLUMNS] for row in rows)
    )


def export_json(rows: Sequence[dict[str, str]]) -> bytes:
    return json.dumps(
        {"columns": list(EXPORT_COLUMNS), "rows": list(rows)}, ensure_ascii=False
    ).encode("utf-8")


def export_filename(start: date, end: date, fmt: str) -> str:
    """``audit-log-YYYY-MM-DD-to-YYYY-MM-DD.<fmt>``, safe in a header.

    Built only from parsed dates and an allow-listed format. Anything else is
    dropped, so nothing can break out of the quoted Content-Disposition value.
    """
    stem = f"audit-log-{start.isoformat()}-to-{end.isoformat()}"
    safe_stem = "".join(ch for ch in stem if ch.isascii() and (ch.isalnum() or ch == "-"))
    safe_ext = fmt if fmt in ("csv", "json") else "txt"
    return f"{safe_stem}.{safe_ext}"
