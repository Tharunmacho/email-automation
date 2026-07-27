"""MongoDB GridFS storage backend.

Stores the original resume files inside the SAME MongoDB you connect to — a
GridFS 'bucket'. No extra credentials beyond the Mongo connection string.

Files are keyed by our storage key (used as the GridFS ``_id``), so retrieval is
a direct lookup and keys are guaranteed unique.
"""
from __future__ import annotations

import gridfs
from gridfs.errors import NoFile

from app.config import settings
from app.db.mongo import get_db
from app.storage.base import StorageBackend


class GridFSStorageBackend(StorageBackend):
    name = "gridfs"

    def __init__(self, bucket: str | None = None):
        self._bucket_name = bucket or settings.storage_gridfs_bucket
        self._fs = gridfs.GridFS(get_db(), collection=self._bucket_name)

    def save(self, key: str, data: bytes, content_type: str | None = None) -> str:
        # If a file with this key somehow already exists, replace it idempotently.
        if self._fs.exists(key):
            self._fs.delete(key)
        self._fs.put(data, _id=key, filename=key, contentType=content_type or "application/octet-stream")
        return key

    def load(self, key: str) -> bytes:
        try:
            return self._fs.get(key).read()
        except NoFile as exc:
            raise FileNotFoundError(f"No file in GridFS bucket '{self._bucket_name}' for key {key!r}") from exc

    def exists(self, key: str) -> bool:
        return self._fs.exists(key)

    def delete(self, key: str) -> None:
        if self._fs.exists(key):
            self._fs.delete(key)

    def url(self, key: str) -> str:
        return f"gridfs://{settings.mongo_db}/{self._bucket_name}/{key}"
