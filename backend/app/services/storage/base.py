"""The storage interface every Creative Studio file goes through.

A backend keeps the *bytes* of a :class:`~app.models.creative.StoredFile`.
The row itself (kind, MIME type, size, SHA-256, who uploaded it) always lives
in the database, so listing, auditing, quota counting and purging never
depend on which backend holds the bytes.

Every read is keyed on ``(tenant_id, file_id)``. A backend must never return
bytes for a file id alone: the tenant filter is what stops one workspace
reading another's files, with row-level security as the second line.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.models.creative import StoredFile


class StorageNotConfigured(ConflictError):
    """The configured storage backend cannot be used in this deployment."""

    code = "storage_not_configured"


@runtime_checkable
class FileStorage(Protocol):
    """Where the bytes of a stored file are kept."""

    #: The value written to ``StoredFile.storage_backend``.
    name: str

    def put(self, db: Session, stored: StoredFile, data: bytes) -> None:
        """Attach ``data`` to ``stored`` (already tenant-stamped, not yet flushed)."""
        ...

    def get(self, db: Session, *, tenant_id: str, file_id: str) -> bytes:
        """The bytes of ``file_id`` in ``tenant_id``; ``NotFoundError`` otherwise."""
        ...

    def delete(self, db: Session, *, tenant_id: str, file_id: str) -> None:
        """Remove ``file_id`` in ``tenant_id`` (row and bytes); a no-op if absent."""
        ...


def tenant_usage_bytes(db: Session, tenant_id: str) -> int:
    """Bytes a workspace stores across every backend, from the rows' sizes."""
    total = db.execute(
        select(func.coalesce(func.sum(StoredFile.size_bytes), 0)).where(
            StoredFile.tenant_id == tenant_id
        )
    ).scalar_one()
    return int(total)
