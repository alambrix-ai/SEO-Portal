"""Placeholder for an S3-compatible object store.

Not implemented. Every call refuses with a plain message, and production
settings already refuse ``STORAGE_BACKEND=s3`` at start-up, so this is only
reachable in development. It exists so the interface has a second
implementation to keep honest.
"""
from __future__ import annotations

from typing import NoReturn

from sqlalchemy.orm import Session

from app.models.creative import StorageBackend, StoredFile
from app.services.storage.base import StorageNotConfigured

NOT_CONFIGURED = (
    "Cloud file storage is not set up for this workspace yet. "
    "Ask your administrator to switch file storage back to the built-in option."
)


class S3Storage:
    name = StorageBackend.S3

    def put(self, db: Session, stored: StoredFile, data: bytes) -> None:
        self._refuse()

    def get(self, db: Session, *, tenant_id: str, file_id: str) -> bytes:
        self._refuse()

    def delete(self, db: Session, *, tenant_id: str, file_id: str) -> None:
        self._refuse()

    @staticmethod
    def _refuse() -> NoReturn:
        raise StorageNotConfigured(NOT_CONFIGURED)
