"""Storage backends for Creative Studio files.

``get_storage()`` returns the backend new files are written to
(``STORAGE_BACKEND``). ``storage_for(row)`` returns the backend an existing
file was written to, so changing the setting never strands old files.
"""
from __future__ import annotations

from app.core.config import settings
from app.models.creative import StorageBackend, StoredFile
from app.services.storage.base import FileStorage, StorageNotConfigured, tenant_usage_bytes
from app.services.storage.postgres import FILE_NOT_FOUND, PostgresStorage
from app.services.storage.s3 import S3Storage

_BACKENDS: dict[str, FileStorage] = {
    StorageBackend.POSTGRES: PostgresStorage(),
    StorageBackend.S3: S3Storage(),
}


def backend_named(name: str) -> FileStorage:
    """The backend called ``name``; ``StorageNotConfigured`` for anything else."""
    backend = _BACKENDS.get(name)
    if backend is None:
        raise StorageNotConfigured("File storage is not set up correctly")
    return backend


def get_storage() -> FileStorage:
    """The backend new files are written to."""
    return backend_named(settings.storage_backend)


def storage_for(stored: StoredFile) -> FileStorage:
    """The backend holding an existing file's bytes."""
    return backend_named(stored.storage_backend)


__all__ = [
    "FILE_NOT_FOUND",
    "FileStorage",
    "PostgresStorage",
    "S3Storage",
    "StorageNotConfigured",
    "backend_named",
    "get_storage",
    "storage_for",
    "tenant_usage_bytes",
]
