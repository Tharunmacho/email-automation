"""Storage backend interface.

The pipeline only ever talks to this ABC, so swapping local disk for S3 or GCS
later is a new subclass + a config value — no pipeline changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    #: Short identifier persisted on each record (e.g. "local", "s3").
    name: str = "base"

    @abstractmethod
    def save(self, key: str, data: bytes, content_type: str | None = None) -> str:
        """Persist ``data`` under ``key``; return the canonical storage key."""

    @abstractmethod
    def load(self, key: str) -> bytes:
        """Return the bytes previously stored under ``key``."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abstractmethod
    def url(self, key: str) -> str:
        """A reference locator for the object (a path, or a signed URL)."""
