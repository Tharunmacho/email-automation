"""What a GridFS write is allowed to cost, and what it does when the link fails.

A résumé bundle is the only Mongo call in this system measured in megabytes.
Against the production host it moves at roughly half a megabyte a second, so an
11 MB scan is a ~20-second upload alone and considerably slower with other
workers on the same link — while `socketTimeoutMS` gave it 30 seconds flat.
That is how a perfectly good CV died with `NetworkTimeout: timed out` partway
through `insert_many`.
"""
from __future__ import annotations

import threading

import pytest
from pymongo.errors import NetworkTimeout

from app.config import settings
from app.storage import gridfs as gridfs_backend
from app.storage.gridfs import GridFSStorageBackend, _deadline_for, _write_slots


class _FakeGridFS:
    """Stands in for `gridfs.GridFS`, recording what it was asked to do."""

    def __init__(self, failures: int = 0, on_put=None):
        self.failures = failures
        self.calls: list[tuple] = []
        self.stored: dict[str, bytes] = {}
        self._on_put = on_put

    def exists(self, key):
        return key in self.stored

    def delete(self, key):
        self.calls.append(("delete", key))
        self.stored.pop(key, None)

    def put(self, data, _id=None, filename=None, contentType=None, chunkSizeBytes=None):
        self.calls.append(("put", _id, chunkSizeBytes))
        if self._on_put:
            self._on_put()
        if self.failures > 0:
            self.failures -= 1
            raise NetworkTimeout("timed out")
        self.stored[_id] = data


class _null_timeout:
    """CSOT needs a live client to enforce a deadline; the deadline itself is
    tested directly against `_deadline_for`."""

    def __init__(self, _seconds):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def backend(monkeypatch):
    """A backend wired to a fake bucket, with retry backoff removed."""
    def _make(**kwargs):
        fake = _FakeGridFS(**kwargs)
        monkeypatch.setattr(gridfs_backend, "get_db", lambda: {})
        monkeypatch.setattr(gridfs_backend.gridfs, "GridFS", lambda *_a, **_k: fake)
        # Real sleeps would make the retry tests take seconds to say nothing.
        monkeypatch.setattr(gridfs_backend.time, "sleep", lambda _s: None)
        monkeypatch.setattr(gridfs_backend.pymongo, "timeout", _null_timeout)
        return GridFSStorageBackend(bucket="test_bucket"), fake
    return _make


# --------------------------------------------------------------------------- #
#  The deadline
# --------------------------------------------------------------------------- #
def test_a_big_file_gets_more_time_than_a_small_one():
    """The whole point. One flat timeout cannot serve both, and when it was
    asked to, the big ones were the ones that lost."""
    small = _deadline_for(150 * 1024)
    big = _deadline_for(11 * 1024 * 1024)

    assert big > small
    # 11 MB at the assumed floor is well past the 30s socket timeout that used
    # to govern it — which is exactly why that timeout kept firing.
    assert big > 30


def test_a_tiny_file_still_gets_the_base_allowance():
    """Proportionality must not mean a small file gets almost no time: a fast
    upload can still be waiting on a slow server."""
    assert _deadline_for(0) == pytest.approx(settings.storage_write_base_timeout_seconds)


# --------------------------------------------------------------------------- #
#  Failing, and trying again
# --------------------------------------------------------------------------- #
def test_a_dropped_connection_mid_upload_is_retried(backend):
    """The bytes are still in hand, so the only thing a retry costs is the
    transfer. Failing the attachment instead costs the application."""
    store, fake = backend(failures=1)

    store.save("2026/09/cv.pdf", b"x" * 1024)

    assert len([c for c in fake.calls if c[0] == "put"]) == 2
    assert "2026/09/cv.pdf" in fake.stored


def test_a_retry_clears_what_the_failed_attempt_left_behind(backend):
    """GridFS writes its chunks before the files document, so an upload cut off
    part-way leaves orphans a plain re-`put` would sit on top of."""
    store, fake = backend(failures=1)
    fake.stored["2026/09/cv.pdf"] = b"partial"

    store.save("2026/09/cv.pdf", b"x" * 1024)

    order = [c[0] for c in fake.calls]
    assert order.count("delete") >= 1
    assert order.index("delete") < order.index("put")


def test_giving_up_says_how_big_the_file_was_and_how_often_we_tried(backend):
    """The operator's next question is always 'was it the file or the link?',
    and the size is what answers it."""
    store, _fake = backend(failures=99)

    with pytest.raises(RuntimeError) as err:
        store.save("2026/09/big.pdf", b"x" * (11 * 1024 * 1024))

    message = str(err.value)
    assert "11.0 MB" in message
    assert str(settings.storage_write_attempts) in message


def test_a_failure_that_is_not_the_network_is_not_retried(backend):
    """A retry is for the link. Anything else is a real answer, and repeating it
    just spends the link three times to hear it again."""
    def _boom():
        raise ValueError("bad content type")

    store, fake = backend(on_put=_boom)

    with pytest.raises(ValueError):
        store.save("2026/09/cv.pdf", b"x")

    assert len([c for c in fake.calls if c[0] == "put"]) == 1


# --------------------------------------------------------------------------- #
#  Not all at once
# --------------------------------------------------------------------------- #
def test_uploads_are_capped_in_flight(backend, monkeypatch):
    """Concurrency buys aggregate throughput and pays in per-write latency, and
    it is the slowest single write that decides whether anything times out."""
    monkeypatch.setattr(settings, "storage_max_concurrent_writes", 2)
    _write_slots.cache_clear()

    live = 0
    peak = 0
    lock = threading.Lock()
    hold = threading.Event()

    def _observe():
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        hold.wait(0.05)
        with lock:
            live -= 1

    store, _fake = backend(on_put=_observe)

    threads = [
        threading.Thread(target=store.save, args=(f"k{i}", b"x" * 16))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak <= 2, f"{peak} uploads were in flight at once"
    _write_slots.cache_clear()


def test_the_measured_chunk_size_is_the_one_actually_used(backend):
    """255 KB → 24.6s, 1 MB → 18.6s, 4 MB → 19.7s on the same 11 MB file. The
    setting carries that result; this is what makes sure it reaches GridFS."""
    store, fake = backend()

    store.save("2026/09/cv.pdf", b"x" * 1024)

    put = [c for c in fake.calls if c[0] == "put"][0]
    assert put[2] == settings.storage_gridfs_chunk_bytes
