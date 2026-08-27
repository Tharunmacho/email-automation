"""One process-wide door to the Veris job queue, and the throttle on it.

Fifty resumes arrive in one poll. Each one is a thread that opens a brand new
HTTPS client, pays for a TCP and TLS handshake, submits a job, polls it to
completion, and throws the connection away. Nothing coordinates them, so the
number of extractions actually queued at Veris is whatever the thread pool
happens to be — and a thread that is merely *waiting* on a job it already
submitted is a thread not submitting the next one.

This module fixes both halves of that.

**Connection reuse.** The client is thread-local, not per-attachment. A worker
thread handshakes once and then reuses that connection for every resume it ever
handles — submissions and status polls alike. At fifty resumes that is fifty
handshakes saved, and the saving grows with the batch.

**A real in-flight cap, decoupled from the worker count.** `MAX_INFLIGHT` is a
process-wide semaphore over *submitted, unfinished* jobs. It is what keeps the
Veris queue full: a thread acquires a slot, submits, waits, and releases the
slot the moment the job reaches a terminal state — at which point the next
resume in the batch takes that slot and is submitted immediately. Work is
therefore always queued at the service up to the cap, rather than up to however
many threads happen not to be blocked.

Raising the cap does not make Veris faster. What it does is stop *us* from being
the reason its queue is short, and it puts the limit in one configurable place
instead of leaving it implicit in `min(10, len(message_ids))`.

**Measurement.** Every submission records how long it waited for a slot, how
long the submit took and how long the whole job took. `snapshot()` returns the
counters and the p50/p95, which is what a load test needs to say anything
truthful about where the time went. They are read by
`GET /ingest/ocr-state`.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.extraction.jobs import AsyncOCRJobClient, JobHandle, JobOutcome, OCRJobError
from app.logging_config import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
#  Metrics
# --------------------------------------------------------------------------- #
def _percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile. Empty input is 0.0, not an error."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return round(ordered[index], 3)


@dataclass
class _Metrics:
    """What the last `sample_size` jobs cost, and how many there were.

    Bounded on purpose: a long-running worker must not accumulate a latency
    sample per resume forever. The counters are unbounded — they are integers —
    and only the timing lists are trimmed.
    """

    submitted: int = 0
    duplicates: int = 0
    succeeded: int = 0
    failed: int = 0
    timed_out: int = 0
    submit_errors: int = 0
    inflight: int = 0
    peak_inflight: int = 0
    # Time spent waiting for a free in-flight slot. If this is large, the cap is
    # the bottleneck and raising it is the right move. If it is ~0 and total
    # latency is high, Veris is the bottleneck and raising the cap will not help.
    queue_wait_ms: List[float] = field(default_factory=list)
    submit_ms: List[float] = field(default_factory=list)
    total_ms: List[float] = field(default_factory=list)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    sample_size: int = 512

    def _record(self, bucket: List[float], value: float) -> None:
        bucket.append(value)
        if len(bucket) > self.sample_size:
            del bucket[: len(bucket) - self.sample_size]

    def on_slot(self, waited_ms: float, inflight: int) -> None:
        with self._lock:
            self._record(self.queue_wait_ms, waited_ms)
            self.inflight = inflight
            self.peak_inflight = max(self.peak_inflight, inflight)

    def on_submitted(self, elapsed_ms: float, duplicate: bool) -> None:
        with self._lock:
            self.submitted += 1
            if duplicate:
                self.duplicates += 1
            self._record(self.submit_ms, elapsed_ms)

    def on_submit_error(self) -> None:
        with self._lock:
            self.submit_errors += 1

    def on_finished(self, outcome: Optional[JobOutcome], total_ms: float, inflight: int) -> None:
        with self._lock:
            self._record(self.total_ms, total_ms)
            self.inflight = inflight
            if outcome is None:
                pass
            elif outcome.succeeded:
                self.succeeded += 1
            elif outcome.timed_out or outcome.pending:
                self.timed_out += 1
            else:
                self.failed += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            queue_wait = list(self.queue_wait_ms)
            submit = list(self.submit_ms)
            total = list(self.total_ms)
            counters = {
                "submitted": self.submitted,
                "duplicates": self.duplicates,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "timed_out": self.timed_out,
                "submit_errors": self.submit_errors,
                "inflight": self.inflight,
                "peak_inflight": self.peak_inflight,
            }
        return {
            **counters,
            "max_inflight": settings.veris_max_inflight_jobs,
            "samples": len(total),
            "queue_wait_ms": {
                "p50": _percentile(queue_wait, 50), "p95": _percentile(queue_wait, 95)
            },
            "submit_ms": {"p50": _percentile(submit, 50), "p95": _percentile(submit, 95)},
            "total_ms": {
                "p50": _percentile(total, 50),
                "p95": _percentile(total, 95),
                "max": round(max(total), 3) if total else 0.0,
            },
        }

    def reset(self) -> None:
        """Zero everything. For a load test that wants a clean baseline."""
        with self._lock:
            self.submitted = self.duplicates = self.succeeded = 0
            self.failed = self.timed_out = self.submit_errors = 0
            self.peak_inflight = self.inflight
            self.queue_wait_ms.clear()
            self.submit_ms.clear()
            self.total_ms.clear()


metrics = _Metrics()


# --------------------------------------------------------------------------- #
#  The gateway
# --------------------------------------------------------------------------- #
class _InFlightLimit:
    """A resizable semaphore over submitted-but-unfinished jobs.

    `threading.Semaphore` cannot be resized, and the cap is a setting an
    operator will want to change during a load test without a restart. This is
    a condition variable and a counter, which can.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._held = 0

    @property
    def held(self) -> int:
        return self._held

    def acquire(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._held >= max(1, settings.veris_max_inflight_jobs):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                if not self._cv.wait(timeout=remaining if remaining is not None else 1.0):
                    # A periodic wake-up, so a cap raised mid-run is noticed by
                    # threads that are already parked.
                    continue
            self._held += 1
            return True

    def release(self) -> None:
        with self._cv:
            self._held = max(0, self._held - 1)
            self._cv.notify()


_limit = _InFlightLimit()
_local = threading.local()


def client() -> AsyncOCRJobClient:
    """This thread's client, created once and reused for its whole life.

    Thread-local rather than shared: it gives every worker thread a connection
    pool it keeps warm across attachments, without having to assume the
    underlying SDK is safe to drive from several threads at once.
    """
    existing = getattr(_local, "client", None)
    if existing is None:
        existing = AsyncOCRJobClient()
        _local.client = existing
    return existing


def close_thread_client() -> None:
    """Drop this thread's client. Called when a worker thread is retiring."""
    existing = getattr(_local, "client", None)
    if existing is not None:
        existing.close()
        _local.client = None


def run_job(
    data: bytes,
    filename: str,
    mode: str,
    idempotency_key: str,
    *,
    budget_seconds: Optional[float] = None,
    lang: Optional[str] = None,
    on_submitted: Optional[Any] = None,
) -> Tuple[Optional[JobHandle], Optional[JobOutcome]]:
    """Submit one extraction and wait for it, holding an in-flight slot.

    The slot is taken *before* the submission and given back the instant the
    job reaches a terminal state — or the instant the wait budget expires, since
    a job left to the reconciler is no longer occupying any of our attention.
    That release is what lets the next resume in the batch be submitted
    immediately rather than when some unrelated thread happens to free up.

    `on_submitted(handle)` fires as soon as the service accepts the work, so the
    caller can write the job id to its ingestion row before the wait begins —
    the wait is the part that can be interrupted, and the job id is the only
    thing that makes the extraction recoverable when it is.

    Raises `OCRJobError` from the submission, which is the caller's to classify.
    A *wait* that fails does not raise: it comes back as an outcome.
    """
    waited_at = time.monotonic()
    _limit.acquire()
    slot_ms = (time.monotonic() - waited_at) * 1000.0
    metrics.on_slot(slot_ms, _limit.held)

    started = time.monotonic()
    handle: Optional[JobHandle] = None
    outcome: Optional[JobOutcome] = None
    try:
        conn = client()
        submit_started = time.monotonic()
        try:
            handle = conn.submit(data, filename, mode, idempotency_key, lang=lang)
        except OCRJobError:
            metrics.on_submit_error()
            raise
        metrics.on_submitted((time.monotonic() - submit_started) * 1000.0, handle.duplicate)

        if on_submitted is not None:
            try:
                on_submitted(handle)
            except Exception:  # noqa: BLE001 — bookkeeping must not lose the job
                log.exception("on_submitted hook failed for job %s", handle.job_id)

        outcome = conn.wait(handle.job_id, mode, budget_seconds)
        return handle, outcome
    finally:
        _limit.release()
        metrics.on_finished(outcome, (time.monotonic() - started) * 1000.0, _limit.held)


def snapshot() -> Dict[str, Any]:
    """Counters and latencies, for the ops endpoint and the load harness."""
    return metrics.snapshot()


def reset_metrics() -> None:
    metrics.reset()
