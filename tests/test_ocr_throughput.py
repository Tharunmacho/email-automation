"""Fifty resumes at once: what actually sets the wall clock.

Two things used to, and neither was the extraction.

**The polling curve.** Pure exponential backoff from 1.5s doubles to 1.5, 3, 6,
12, 24 — so a job that genuinely finished at eight seconds was not *observed*
until twenty-two. The waiting cost more than the work, and no amount of extra
concurrency fixes a per-job latency floor. Hence the fast phase.

**A thread that has already submitted.** Every worker submitted one job and then
sat on that thread polling it. The number of extractions actually queued at
Veris was therefore the number of *threads*, and the forty-first résumé had
nothing submitted for it until some thread finished entirely. Hence the
in-flight cap, which is released the moment a job is terminal rather than when
the thread that owns it moves on.

Both are pinned here with a fake clock and a fake service. Nothing in this file
sleeps for real, and nothing talks to Veris.
"""
from __future__ import annotations

import threading
import time
from typing import List

import pytest

from app.config import settings
from app.extraction import ocr_gateway
from app.extraction.jobs import (
    QUEUED,
    RUNNING,
    SUCCEEDED,
    AsyncOCRJobClient,
    JobOutcome,
)


# --------------------------------------------------------------------------- #
#  A clock and a service that cost nothing
# --------------------------------------------------------------------------- #
class FakeClock:
    """Monotonic time the test advances by sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: List[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def time(self) -> float:
        return self.now


class FakeJob:
    def __init__(self, job_id: str, status: str):
        self.job_id = job_id
        self.mode = "resume"
        self.status = status
        self.result = {"pages": [{"text": "extracted"}]}
        self.error = None
        self.attempts = 1


class FakeService:
    """Answers `queued` until `finish_at`, then `succeeded`."""

    def __init__(self, clock: FakeClock, finish_after: float):
        self.clock = clock
        self.finish_at = clock.now + finish_after
        self.polls = 0
        self.jobs = self

    def get(self, job_id: str) -> FakeJob:
        self.polls += 1
        status = SUCCEEDED if self.clock.now >= self.finish_at else QUEUED
        return FakeJob(job_id, status)


def make_client(clock: FakeClock, service: FakeService) -> AsyncOCRJobClient:
    return AsyncOCRJobClient(
        client=service,
        sleep=clock.sleep,
        rand=lambda: 0.5,          # mid-jitter, so the arithmetic is exact
    )


# --------------------------------------------------------------------------- #
#  Latency: a job that finishes at 8s must be seen at ~8s
# --------------------------------------------------------------------------- #
def test_a_job_that_finishes_quickly_is_observed_quickly():
    """The regression that motivated the fast phase.

    Under pure exponential backoff this job — done at 8s — was not noticed until
    roughly 22s, and the *waiting* was two thirds of the response time.
    """
    clock = FakeClock()
    service = FakeService(clock, finish_after=8.0)
    started = clock.now

    outcome = make_client(clock, service).wait("job-1", "resume", budget_seconds=240.0)

    observed = clock.now - started
    assert outcome.status == SUCCEEDED
    assert observed < 9.0, f"took {observed:.1f}s to notice a job done at 8.0s"


def test_a_whole_batch_stays_inside_the_latency_target():
    """Fifty jobs, each finishing in a realistic 6-12s, all observed inside 15s.

    This is the per-résumé target stated as a test. It is about *our* polling,
    not about how fast Veris extracts — if the service takes a minute, no
    polling strategy makes that fifteen seconds, and the test says so by
    fixing the service's own duration.
    """
    worst = 0.0
    for index in range(50):
        clock = FakeClock()
        finish_after = 6.0 + (index % 7)          # 6.0 .. 12.0
        service = FakeService(clock, finish_after)
        started = clock.now
        outcome = make_client(clock, service).wait(f"job-{index}", "resume", 240.0)
        assert outcome.status == SUCCEEDED
        worst = max(worst, clock.now - started)
    assert worst <= 15.0, f"worst observed latency was {worst:.1f}s"


def test_the_fast_phase_does_not_hammer_the_service():
    """Responsiveness must not be bought with a poll storm."""
    clock = FakeClock()
    service = FakeService(clock, finish_after=10.0)
    make_client(clock, service).wait("job-1", "resume", 240.0)
    # ~0.6s apart over ten seconds.
    assert service.polls <= 25, f"{service.polls} polls to cover 10 seconds"


def test_a_long_job_still_backs_off():
    """Past the fast window the polling must get out of the service's way.

    A 60-page scan nobody is waiting on should not be polled twice a second for
    four minutes.
    """
    clock = FakeClock()
    service = FakeService(clock, finish_after=200.0)
    client = make_client(clock, service)
    client.wait("job-1", "resume", budget_seconds=240.0)

    tail = client._backoff(
        attempt=60, elapsed=settings.ocr_job_fast_poll_seconds + 120.0
    )
    assert tail >= settings.ocr_job_backoff_base_seconds


def test_retry_after_still_wins_outright():
    """The service knows its own queue depth; the fast phase must not ignore it."""
    clock = FakeClock()
    client = make_client(clock, FakeService(clock, 1.0))
    assert client._backoff(attempt=1, retry_after=8.0, elapsed=0.0) == 8.0


def test_backoff_is_jittered_so_a_batch_does_not_return_in_lockstep():
    clock = FakeClock()
    service = FakeService(clock, 1.0)
    low = AsyncOCRJobClient(client=service, sleep=clock.sleep, rand=lambda: 0.0)
    high = AsyncOCRJobClient(client=service, sleep=clock.sleep, rand=lambda: 1.0)
    elapsed = settings.ocr_job_fast_poll_seconds + 1.0
    assert low._backoff(attempt=50, elapsed=elapsed) != high._backoff(attempt=50, elapsed=elapsed)


# --------------------------------------------------------------------------- #
#  Throughput: the in-flight cap, and when the slot is given back
# --------------------------------------------------------------------------- #
class SlowGatewayClient:
    """Records concurrency: how many calls are inside `wait` at the same time."""

    def __init__(self, hold: float = 0.05):
        self.hold = hold
        self.concurrent = 0
        self.peak = 0
        self.submitted = 0
        self.order: List[str] = []
        self._lock = threading.Lock()

    def submit(self, data, filename, mode, key, *, lang=None, **_kw):
        with self._lock:
            self.submitted += 1
            self.order.append(key)
        from app.extraction.jobs import JobHandle
        return JobHandle(job_id=f"job-{key}", mode=mode, status=QUEUED)

    def wait(self, job_id, mode="", budget_seconds=None, **_kw):
        with self._lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            time.sleep(self.hold)
        finally:
            with self._lock:
                self.concurrent -= 1
        return JobOutcome(job_id=job_id, mode=mode, status=SUCCEEDED, result={"pages": []})


@pytest.fixture
def gateway_client(monkeypatch):
    fake = SlowGatewayClient()
    monkeypatch.setattr(ocr_gateway, "client", lambda: fake)
    ocr_gateway.reset_metrics()
    return fake


def test_in_flight_jobs_are_capped_process_wide(gateway_client, monkeypatch):
    """Forty resumes, a cap of six: never more than six submitted-and-unfinished.

    The cap is what stops a burst turning into a flood at the service, and it
    has to hold across threads, not per thread.
    """
    monkeypatch.setattr(settings, "veris_max_inflight_jobs", 6)

    threads = [
        threading.Thread(
            target=ocr_gateway.run_job,
            args=(b"pdf", f"r{i}.pdf", "resume", f"key-{i}"),
        )
        for i in range(40)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert gateway_client.submitted == 40
    assert gateway_client.peak <= 6, f"peak in flight was {gateway_client.peak}, cap was 6"


def test_the_cap_is_actually_used_not_merely_respected(gateway_client, monkeypatch):
    """A cap of six that only ever runs two jobs would pass the test above.

    What throughput depends on is the slot being *reused* the instant a job
    finishes — so with more work than slots, the cap should be saturated.
    """
    monkeypatch.setattr(settings, "veris_max_inflight_jobs", 6)

    threads = [
        threading.Thread(
            target=ocr_gateway.run_job,
            args=(b"pdf", f"r{i}.pdf", "resume", f"key-{i}"),
        )
        for i in range(40)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert gateway_client.peak >= 4, (
        f"only reached {gateway_client.peak} concurrent jobs with 40 waiting and 6 slots"
    )


def test_a_slot_is_released_even_when_the_submission_fails(gateway_client, monkeypatch):
    """A raising submit must not leak the slot, or the batch deadlocks.

    This is the failure that turns a transient 400 on one résumé into a stalled
    poll cycle for the other forty-nine.
    """
    from app.extraction.jobs import OCRJobError

    monkeypatch.setattr(settings, "veris_max_inflight_jobs", 2)

    def boom(*_a, **_kw):
        raise OCRJobError("bad file", retryable=False)

    monkeypatch.setattr(gateway_client, "submit", boom)

    for i in range(6):
        with pytest.raises(OCRJobError):
            ocr_gateway.run_job(b"pdf", f"r{i}.pdf", "resume", f"key-{i}")

    # Every slot came back, so a good file still goes through afterwards.
    assert ocr_gateway._limit.held == 0
    assert ocr_gateway.snapshot()["submit_errors"] == 6


def test_the_job_id_is_recorded_before_the_wait_begins(gateway_client):
    """The wait is the interruptible part; the id is what makes it recoverable.

    Recording the submission only *after* the wait would mean a job abandoned at
    the budget had no id on its row — and the reconciler would have nothing to
    go back for.
    """
    seen: List[str] = []
    ocr_gateway.run_job(
        b"pdf", "r.pdf", "resume", "key-1",
        on_submitted=lambda handle: seen.append(handle.job_id),
    )
    assert seen == ["job-key-1"]


def test_a_failing_hook_does_not_lose_the_job(gateway_client):
    """Bookkeeping is not allowed to discard an extraction that was accepted."""
    def broken(_handle):
        raise RuntimeError("mongo is down")

    handle, outcome = ocr_gateway.run_job(
        b"pdf", "r.pdf", "resume", "key-1", on_submitted=broken
    )
    assert handle is not None and outcome.succeeded


# --------------------------------------------------------------------------- #
#  Connection reuse
# --------------------------------------------------------------------------- #
def test_one_client_per_thread_not_one_per_resume(monkeypatch):
    """Fifty resumes on one thread must not mean fifty TLS handshakes."""
    built = []

    class CountingClient:
        def __init__(self, *a, **kw):
            built.append(self)

        def close(self):
            pass

    monkeypatch.setattr(ocr_gateway, "AsyncOCRJobClient", CountingClient)
    ocr_gateway.close_thread_client()

    first = ocr_gateway.client()
    for _ in range(50):
        assert ocr_gateway.client() is first
    assert len(built) == 1

    ocr_gateway.close_thread_client()


def test_each_thread_gets_its_own_client(monkeypatch):
    """Thread-local, so no assumption is made about the SDK being thread-safe."""
    built = []
    lock = threading.Lock()

    class DummyClient:
        def __init__(self, *a, **kw):
            with lock:
                built.append(self)

        def close(self):
            pass

    monkeypatch.setattr(ocr_gateway, "AsyncOCRJobClient", DummyClient)
    ocr_gateway.close_thread_client()

    # References are held in `seen` for the whole test: releasing them would let
    # CPython recycle the addresses, and identity comparison would then report
    # four separate clients as one.
    seen = []

    def grab():
        client = ocr_gateway.client()
        with lock:
            seen.append(client)
        ocr_gateway.close_thread_client()

    threads = [threading.Thread(target=grab) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 4
    assert len({id(c) for c in seen}) == 4


# --------------------------------------------------------------------------- #
#  Measurement
# --------------------------------------------------------------------------- #
def test_the_snapshot_reports_what_a_load_test_needs(gateway_client):
    for i in range(5):
        ocr_gateway.run_job(b"pdf", f"r{i}.pdf", "resume", f"key-{i}")

    snap = ocr_gateway.snapshot()
    assert snap["submitted"] == 5
    assert snap["succeeded"] == 5
    assert snap["peak_inflight"] >= 1
    assert snap["total_ms"]["p50"] > 0
    assert "queue_wait_ms" in snap and "p95" in snap["queue_wait_ms"]


def test_percentiles_do_not_explode_on_an_empty_sample():
    ocr_gateway.reset_metrics()
    snap = ocr_gateway.snapshot()
    assert snap["total_ms"]["p50"] == 0.0
    assert snap["total_ms"]["max"] == 0.0
