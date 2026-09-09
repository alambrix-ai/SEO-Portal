"""Declarative base and the PostgreSQL column conventions models share."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ── Shared column types ────────────────────────────────────────────────────
# Native uuid columns, surfaced to Python as strings so the API layer never
# has to convert. Random v4 keys keep row ids unguessable in a public product.
PK = UUID(as_uuid=False)
FK = UUID(as_uuid=False)
# JSONB rather than JSON: indexable, and it round-trips without re-parsing.
JSONDoc = JSONB


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} {pk}>"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class TenantScopedMixin:
    """Every business row belongs to exactly one organisation.

    The column is a real foreign key with ``ON DELETE CASCADE``, so closing an
    account removes its data in one statement, and it is the column every
    row-level-security policy keys on — see ``app/db/rls.py``. Application
    queries still filter on it explicitly; RLS is the second line of defence,
    not the first.
    """

    tenant_id: Mapped[str] = mapped_column(
        FK,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
