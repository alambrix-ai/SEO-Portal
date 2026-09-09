"""Audit trail writer.

Every consequential action — human or agent — goes through here, so the Admin
screen's audit log and any later compliance export read from one table with one
shape. Request IP is stored encrypted: an audit trail is itself personal data.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

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
