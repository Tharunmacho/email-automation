"""MongoDB GridFS storage backend.

Stores the original resume files inside the SAME MongoDB you connect to — a
GridFS 'bucket'. No extra credentials beyond the Mongo connection string.

Files are keyed by our storage key (used as the GridFS ``_id``), so retrieval is
a direct lookup and keys are guaranteed unique.

Sizing, because this is the one Mongo call measured in megabytes
--------------------------------------------------------------
Every other query here is a few kilobytes and finishes inside the client's
``socketTimeoutMS``. A résumé bundle is not: 11 MB of scanned pages against the
production host is a ~20-second upload with the link to itself, and longer once
several workers are uploading together. The flat 30-second socket timeout was
being asked to cover both, and the large ones lost — a `NetworkTimeout` mid-
upload, which failed the attachment and (before the runner stopped marking
failures read) retired the application.

Three things follow, each measured rather than guessed:

* the deadline scales with the payload, via CSOT, instead of being flat;
* uploads are capped in flight, because concurrency buys aggregate throughput
  but pays for it in per-write latency, and latency is what times out;
* a transient network failure is retried, since the bytes are still in hand.
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache

import gridfs
import pymongo
from gridfs.errors import NoFile
from pymongo.errors import ConnectionFailure, ExecutionTimeout, PyMongoError

from app.config import settings
from app.db.mongo import get_db
from app.logging_config import get_logger
from app.storage.base import StorageBackend

log = get_logger(__name__)

#: Failures worth trying again: the connection went away, or the deadline was
#: reached. Both describe the link, not the file, and the bytes are still in
#: memory — so the only thing a retry costs is the transfer.
#:
#: `ConnectionFailure` is the parent of `AutoReconnect`, `NetworkTimeout` and
#: `ServerSelectionTimeoutError`, which is every way a WAN blip arrives here.
_TRANSIENT = (ConnectionFailure, ExecutionTimeout)


@lru_cache(maxsize=1)
def _write_slots() -> threading.BoundedSemaphore:
    """Uploads in flight for this process, shared by every backend instance.

    Module-level on purpose: the thing being rationed is the network link, and
    there is one of those however many `GridFSStorageBackend` objects exist.
    """
    return threading.BoundedSemaphore(max(1, int(settings.storage_max_concurrent_writes)))


def _deadline_for(size: int) -> float:
    """Seconds this transfer may take, as `base + size / assumed throughput`.

    The point is proportionality, not accuracy. A 150 KB CV gets essentially
    the base allowance and an 11 MB bundle gets about two minutes, so neither a
    small file waits on a large file's budget nor a large file dies inside a
    small file's.
    """
    rate = max(1, int(settings.storage_write_min_throughput_bytes))
    return float(settings.storage_write_base_timeout_seconds) + size / rate


class GridFSStorageBackend(StorageBackend):
    name = "gridfs"

    def __init__(self, bucket: str | None = None):
        self._bucket_name = bucket or settings.storage_gridfs_bucket
        self._fs = gridfs.GridFS(get_db(), collection=self._bucket_name)

    def save(self, key: str, data: bytes, content_type: str | None = None) -> str:
        attempts = max(1, int(settings.storage_write_attempts))
        deadline = _deadline_for(len(data))
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            with _write_slots():
                try:
                    with pymongo.timeout(deadline):
                        # Replace idempotently — and, on a retry, clear whatever
                        # chunks the failed attempt left behind. GridFS writes
                        # its chunks before the files document, so an upload cut
                        # off part-way leaves orphans that a plain re-`put`
                        # would sit on top of.
                        if self._fs.exists(key):
                            self._fs.delete(key)
                        self._fs.put(
                            data,
                            _id=key,
                            filename=key,
                            contentType=content_type or "application/octet-stream",
                            chunkSizeBytes=int(settings.storage_gridfs_chunk_bytes),
                        )
                    return key
                except _TRANSIENT as exc:
                    last = exc
                    log.warning(
                        "GridFS write of %s (%.1f MB) failed on attempt %d/%d "
                        "within %.0fs: %s",
                        key, len(data) / 1048576, attempt, attempts, deadline, exc,
                    )

            if attempt < attempts:
                # Outside the semaphore: holding an upload slot while sleeping
                # would idle the very link the retry is waiting on.
                time.sleep(min(2 ** attempt, 10))

        raise RuntimeError(
            f"Could not write {key} ({len(data) / 1048576:.1f} MB) to GridFS after "
            f"{attempts} attempt(s): {last}"
        ) from last

    def load(self, key: str) -> bytes:
        try:
            # Sized the same way a write is, from what the stored file actually
            # weighs, so downloading a large bundle is not held to a query's
            # timeout either.
            with pymongo.timeout(_deadline_for(self._size_of(key))):
                return self._fs.get(key).read()
        except NoFile as exc:
            raise FileNotFoundError(f"No file in GridFS bucket '{self._bucket_name}' for key {key!r}") from exc

    def _size_of(self, key: str) -> int:
        """The stored length, or 0 if it cannot be read cheaply.

        Only feeds the deadline, so a miss costs the base allowance rather than
        an error — and the `get` below will raise `NoFile` properly anyway.
        """
        try:
            doc = get_db()[f"{self._bucket_name}.files"].find_one({"_id": key}, {"length": 1})
            return int(doc.get("length", 0)) if doc else 0
        except PyMongoError:
            return 0

    def exists(self, key: str) -> bool:
        return self._fs.exists(key)

    def delete(self, key: str) -> None:
        if self._fs.exists(key):
            self._fs.delete(key)

    def url(self, key: str) -> str:
        return f"gridfs://{settings.mongo_db}/{self._bucket_name}/{key}"
