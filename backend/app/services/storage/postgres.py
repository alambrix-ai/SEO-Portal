"""Stored-file bytes kept in the ``stored_files.data`` column.

The default backend. The column is deferred on the model, so listing files
never loads blobs; :meth:`PostgresStorage.get` selects the column on its own
and only for the one row asked for, in the one workspace asked for.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.creative import StorageBackend, StoredFile

#: Shown when a file id does not exist in the caller's workspace. The same
#: message whether it never existed or belongs to someone else.
FILE_NOT_FOUND = "That file could not be found"


class PostgresStorage:
    name = StorageBackend.POSTGRES

    def put(self, db: Session, stored: StoredFile, data: bytes) -> None:
        stored.storage_backend = self.name
        stored.storage_key = ""
        stored.data = data

    def get(self, db: Session, *, tenant_id: str, file_id: str) -> bytes:
        row = db.execute(
            select(StoredFile.id, StoredFile.data).where(
                StoredFile.id == file_id,
                StoredFile.tenant_id == tenant_id,
                StoredFile.storage_backend == self.name,
            )
        ).first()
        if row is None or row.data is None:
            raise NotFoundError(FILE_NOT_FOUND)
        return bytes(row.data)

    def delete(self, db: Session, *, tenant_id: str, file_id: str) -> None:
        stored = db.execute(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.tenant_id == tenant_id,
                StoredFile.storage_backend == self.name,
            )
        ).scalar_one_or_none()
        if stored is not None:
            db.delete(stored)
            db.flush()
