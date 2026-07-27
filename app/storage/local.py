"""Local filesystem storage backend.

Files are laid out by date so a directory never accumulates millions of entries:
    <root>/<YYYY>/<MM>/<key>
"""
from __future__ import annotations

from pathlib import Path

from app.storage.base import StorageBackend


class LocalStorageBackend(StorageBackend):
    name = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def save(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def load(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def url(self, key: str) -> str:
        return str(self._path(key).resolve())
