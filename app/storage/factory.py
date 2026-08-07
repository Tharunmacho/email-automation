"""Resolve the configured storage backend.

Add S3/GCS by implementing StorageBackend and registering it here — nothing
else in the pipeline changes.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.storage.base import StorageBackend


@lru_cache(maxsize=4)
def get_storage_backend(name: str | None = None) -> StorageBackend:
    backend = (name or settings.storage_backend).lower()
    if backend == "gridfs":
        from app.storage.gridfs import GridFSStorageBackend

        return GridFSStorageBackend()
    if backend == "local":
        from app.storage.local import LocalStorageBackend

        return LocalStorageBackend(settings.storage_local_dir)
    # Extension points — implement and wire up when needed:
    # if backend == "s3":  return S3StorageBackend(...)
    # if backend == "gcs": return GCSStorageBackend(...)
    raise ValueError(
        f"Unsupported STORAGE_BACKEND={backend!r}. "
        "Supported: 'gridfs', 'local'."
    )
