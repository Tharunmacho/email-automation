"""Submit OCR work to the Veris job queue and wait for it, politely.

The synchronous endpoints (`/v1/resume/extract` and friends) hold the HTTP
connection open for the whole extraction. A 9-page scanned bundle took long
enough to blow a 180-second timeout, and when it did there was no job to ask
about afterwards — the work was simply gone, and the mail was retried from the
top. That is the failure this module exists to remove.

`POST /v1/jobs` queues the work and answers in milliseconds with a job id;
`GET /v1/jobs/{id}` reports on it. Three things follow, and all three are the
point:

* **Idempotency.** Every submission carries an ``Idempotency-Key`` derived from
  the mail it came from, so a retry after a dropped connection re-attaches to
  the job already running instead of starting a second one. The service answers
  such a resubmission with ``duplicate: true`` and the original job id.
* **Backpressure is a wait, not a failure.** A full queue answers 429 or 503
  with ``Retry-After``. Honouring it is what keeps a mailbox full of 60-page
  scans from turning into a retry storm against a service that is already
  struggling.
* **Nothing is lost to a timeout.** A wait that runs out of budget leaves the
  job running and the job id on the ingestion row, so the reconciler picks the
  result up later rather than paying for the extraction twice.

The transport is the ``veriis`` SDK, which already models the job API and its
error taxonomy. Everything here is the policy on top of it.
"""
from __future__ import annotations

import contextlib
import hashlib
import random
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Protocol

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

# The modes the OCR service accepts, mirrored so callers do not have to import
# the SDK to name one.
MODE_RESUME = "resume"
MODE_AADHAAR = "aadhaar"
MODE_PASSPORT = "passport"
MODE_DOCUMENT = "document"

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
_TERMINAL = (SUCCEEDED, FAILED)


class JobRecorder(Protocol):
    """How the extraction layer reports a job back to the ingestion ledger.

    A protocol rather than an import, deliberately. `app.extraction` knows how
    to read documents and nothing about MongoDB; inverting the dependency keeps
    it that way, and keeps every extraction test free of a database.
    """

    def on_submitted(self, mode: str, job_id: str, idempotency_key: str) -> None:
        ...

    def on_finished(self, mode: str, job_id: str, status: str, error: str = "") -> None:
        ...


@dataclass
class JobContext:
    """Which piece of mail the extraction currently running belongs to.

    Carried as ambient context rather than threaded through six signatures in
    `text_extractor`, because every one of those functions is about reading a
    PDF and none of them is about email. What it buys is the idempotency key:
    without it a résumé job is keyed on the file's own hash, which is correct
    but says nothing about *which* delivery of that file is being retried.
    """

    provider: str = "email"
    account_id: str = ""
    message_id: str = ""
    attachment_id: str = ""
    recorder: Optional[JobRecorder] = None

    def key_for(self, mode: str, fallback_digest: str = "") -> str:
        if self.message_id:
            handle = (self.attachment_id or "")[:64]
            return f"{self.provider}/{self.account_id}/{self.message_id}/{handle}/{mode}"
        # No mail context (a CLI parse, a manual re-run): the file's own content
        # is the next best identity, and re-reading the same bytes should still
        # re-attach rather than re-bill.
        return f"content/{fallback_digest}/{mode}"


_JOB_CONTEXT: ContextVar[Optional[JobContext]] = ContextVar("ocr_job_context", default=None)


def current_job_context() -> Optional[JobContext]:
    return _JOB_CONTEXT.get()


@contextlib.contextmanager
def use_job_context(context: Optional[JobContext]) -> Iterator[Optional[JobContext]]:
    """Bind ``context`` for the duration of one attachment's extraction."""
    token = _JOB_CONTEXT.set(context)
    try:
        yield context
    finally:
        _JOB_CONTEXT.reset(token)


def content_key(data: bytes) -> str:
    """A short, stable digest of the bytes, for keying a context-free run."""
    return hashlib.sha256(data).hexdigest()[:32]


class OCRJobError(RuntimeError):
    """Something went wrong talking to the job queue.

    ``retryable`` is the whole reason this class exists: a 429 and a 400 are
    both failures, but one means "come back in eight seconds" and the other
    means "this file will never extract". The reconciler burns an attempt on the
    first and abandons the row on the second.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: Optional[float] = None,
        status: Optional[int] = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status


@dataclass
class JobHandle:
    """What the service says when it accepts work."""

    job_id: str
    mode: str
    status: str
    duplicate: bool = False
    status_url: str = ""


@dataclass
class JobOutcome:
    """Where a job ended up, from the caller's point of view."""

    job_id: str
    mode: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    error_retryable: bool = False
    attempts: int = 0
    polls: int = 0
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == SUCCEEDED

    @property
    def pending(self) -> bool:
        """Still with the service: not finished, but not lost either."""
        return self.status in (QUEUED, RUNNING)


def _as_dict(obj: Any) -> Optional[Dict[str, Any]]:
    """Pydantic model, mapping or None → a plain dict we can store in Mongo."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return dict(obj)


class AsyncOCRJobClient:
    """Submit, poll and wait — with the retry policy the service asks for.

    Both ``sleep`` and ``rand`` are injectable so the backoff can be asserted on
    in tests without any test spending eight real seconds asleep.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ):
        self._client = client
        self._base_url = base_url or settings.veris_ocr_base_url
        self._api_key = api_key or settings.veris_ocr_api_key
        self._timeout = timeout if timeout is not None else settings.veris_timeout_seconds
        self._sleep = sleep
        self._rand = rand

    # ---- transport -------------------------------------------------------- #
    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise OCRJobError(
                    "VERIS_OCR_API_KEY is not set, so no OCR job can be submitted.",
                    retryable=False,
                )
            try:
                from veriis import VerisOCR
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise OCRJobError(
                    "The 'veriis' package is required for async OCR jobs "
                    f"(pip install 'veriis>=0.1.2'): {exc}",
                    retryable=False,
                ) from exc
            self._client = VerisOCR(
                base_url=self._base_url,
                api_key=self._api_key,
                timeout=self._timeout,
                # Transport-level retries are off: the retry policy lives here,
                # where it can honour Retry-After and record an attempt on the
                # ingestion row. Two independent retry loops would multiply.
                max_retries=0,
            )
        return self._client

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — closing must never raise
                pass
        self._client = None

    def __enter__(self) -> "AsyncOCRJobClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- error taxonomy --------------------------------------------------- #
    @staticmethod
    def _translate(exc: Exception) -> OCRJobError:
        """Map an SDK exception onto "wait and try again" or "give up"."""
        status = getattr(exc, "status", None)
        retry_after = getattr(exc, "retry_after", None)

        try:
            from veriis import (
                VerisOCRAuthenticationError,
                VerisOCRBadRequestError,
                VerisOCRConnectionError,
                VerisOCRNotFoundError,
                VerisOCRRateLimitError,
                VerisOCRServerError,
                VerisOCRTimeoutError,
                VerisOCRValidationError,
            )
        except ImportError:  # pragma: no cover - dependency is pinned
            return OCRJobError(str(exc), retryable=False, status=status)

        # A full queue, a restarting worker or a dropped connection are all
        # "the file is fine, the service is busy".
        if isinstance(
            exc,
            (
                VerisOCRRateLimitError,
                VerisOCRServerError,
                VerisOCRTimeoutError,
                VerisOCRConnectionError,
            ),
        ):
            return OCRJobError(str(exc), retryable=True, retry_after=retry_after, status=status)

        # A malformed file, a rejected key or a job id that does not exist will
        # look exactly the same on the tenth attempt as on the first.
        if isinstance(
            exc,
            (
                VerisOCRBadRequestError,
                VerisOCRValidationError,
                VerisOCRAuthenticationError,
                VerisOCRNotFoundError,
            ),
        ):
            return OCRJobError(str(exc), retryable=False, status=status)

        # Anything unrecognised: 5xx is the service's problem, everything else
        # is ours. Guessing "retryable" on an unknown 4xx would loop forever.
        retryable = bool(status and 500 <= int(status) < 600)
        return OCRJobError(str(exc), retryable=retryable, retry_after=retry_after, status=status)

    # ---- backoff ---------------------------------------------------------- #
    def _backoff(
        self,
        attempt: int,
        retry_after: Optional[float] = None,
        *,
        elapsed: float = 0.0,
    ) -> float:
        """How long to wait before attempt ``attempt`` (1-based).

        ``Retry-After`` wins outright when the service sends one — it knows how
        long its queue is and we do not.

        Otherwise there are two regimes, and having only the second one was
        costing more latency than the extraction itself. Pure exponential backoff
        from 1.5s doubles to 1.5, 3, 6, 12, 24 — so a job that genuinely finished
        at eight seconds was not *observed* until twenty-two, and the wait, not
        the work, set the response time. Almost every résumé finishes inside the
        first half-minute, so:

        * **Fast phase** — for the first ``ocr_job_fast_poll_seconds`` of the
          wait, poll at a flat short interval. A job that finishes at 8s is seen
          at 8s. This is what a per-résumé latency target is actually made of.
        * **Backoff phase** — past that, the job is a long one (a 60-page scan,
          or a queue that is genuinely deep), nobody is waiting on the next poll
          landing quickly, and the polling should get out of the service's way.

        Both phases are jittered across the whole interval rather than a
        fraction of it, so fifty résumés submitted together do not come back in
        lockstep and re-create the burst that got them throttled.
        """
        if retry_after is not None and retry_after > 0:
            return min(float(retry_after), settings.ocr_job_backoff_cap_seconds)

        if elapsed < settings.ocr_job_fast_poll_seconds:
            interval = settings.ocr_job_fast_poll_interval_seconds
            # Jitter is narrower here: the point of the fast phase is a tight,
            # predictable observation delay, and 0.5x-1.0x of an already short
            # interval would just add polls without adding responsiveness.
            return round(interval * (0.85 + 0.3 * self._rand()), 3)

        # Measured from the end of the fast phase, so the first backoff step is
        # the base interval rather than whatever the attempt counter had reached
        # while polling quickly.
        steps = max(0, attempt - self._fast_phase_polls())
        base = settings.ocr_job_backoff_base_seconds * (2 ** steps)
        capped = min(base, settings.ocr_job_backoff_cap_seconds)
        return round(capped * (0.5 + 0.5 * self._rand()), 3)

    @staticmethod
    def _fast_phase_polls() -> int:
        """Roughly how many polls the fast phase spends, for the backoff reset."""
        interval = max(0.05, settings.ocr_job_fast_poll_interval_seconds)
        return max(1, int(settings.ocr_job_fast_poll_seconds / interval))

    # ---- operations ------------------------------------------------------- #
    def submit(
        self,
        data: bytes,
        filename: str,
        mode: str,
        idempotency_key: str,
        *,
        lang: Optional[str] = None,
        max_backpressure_retries: Optional[int] = None,
    ) -> JobHandle:
        """Queue one extraction. Waits out a busy queue; raises on a bad file.

        The same ``idempotency_key`` may be submitted any number of times: the
        service returns the original job rather than queueing a second one, and
        that is exactly what makes the reconciler safe to run.
        """
        from veriis import FileDescriptor

        retries = (
            max_backpressure_retries
            if max_backpressure_retries is not None
            else settings.ocr_job_submit_retries
        )
        descriptor = FileDescriptor(data=data, filename=filename or "attachment.pdf")

        last: Optional[OCRJobError] = None
        for attempt in range(1, max(1, retries) + 1):
            try:
                accepted = self.client.jobs.submit(
                    descriptor,
                    mode=mode,
                    lang=lang,
                    idempotency_key=idempotency_key,
                )
            except Exception as exc:  # noqa: BLE001 — translated immediately
                err = self._translate(exc)
                if not err.retryable or attempt >= max(1, retries):
                    raise err from exc
                delay = self._backoff(attempt, err.retry_after)
                log.warning(
                    "OCR queue pushed back on %s (%s); retrying in %.1fs [attempt %d/%d]",
                    mode, err, delay, attempt, retries,
                )
                last = err
                self._sleep(delay)
                continue

            handle = JobHandle(
                job_id=str(getattr(accepted, "job_id", "")),
                mode=str(getattr(accepted, "mode", mode)),
                status=str(getattr(accepted, "status", QUEUED)),
                duplicate=bool(getattr(accepted, "duplicate", False)),
                status_url=str(getattr(accepted, "status_url", "") or ""),
            )
            log.info(
                "Queued OCR job %s mode=%s duplicate=%s key=%s",
                handle.job_id, handle.mode, handle.duplicate, idempotency_key,
            )
            return handle

        raise last or OCRJobError(f"Could not submit {mode} job", retryable=True)

    def poll(self, job_id: str, mode: str = "") -> JobOutcome:
        """One status read. Never sleeps; never retries."""
        try:
            job = self.client.jobs.get(job_id)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

        error = getattr(job, "error", None)
        return JobOutcome(
            job_id=str(getattr(job, "job_id", job_id)),
            mode=str(getattr(job, "mode", mode)),
            status=str(getattr(job, "status", QUEUED)),
            result=_as_dict(getattr(job, "result", None)),
            error=str(getattr(error, "message", "") or "") if error else "",
            error_retryable=bool(getattr(error, "retryable", False)) if error else False,
            attempts=int(getattr(job, "attempts", 0) or 0),
        )

    def wait(
        self,
        job_id: str,
        mode: str = "",
        budget_seconds: Optional[float] = None,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> JobOutcome:
        """Poll until the job finishes or the budget runs out.

        Running out of budget is **not** an error. The job keeps running on the
        service, its id is already on the ingestion row, and the reconciler will
        collect the result — so the outcome comes back with ``timed_out`` set
        and the last known status, and the caller carries on with the work it
        can do meanwhile.
        """
        budget = budget_seconds if budget_seconds is not None else settings.ocr_job_wait_seconds
        started = now()
        deadline = started + max(0.0, float(budget))
        attempt = 0
        polls = 0
        last = JobOutcome(job_id=job_id, mode=mode, status=QUEUED)

        while True:
            attempt += 1
            try:
                last = self.poll(job_id, mode)
                polls += 1
                if last.status in _TERMINAL:
                    last.polls = polls
                    return last
                retry_after = None
            except OCRJobError as err:
                if not err.retryable:
                    raise
                retry_after = err.retry_after
                log.debug("Transient error polling job %s: %s", job_id, err)

            delay = self._backoff(attempt, retry_after, elapsed=now() - started)
            if now() + delay >= deadline:
                last.polls = polls
                last.timed_out = True
                log.info(
                    "Job %s (%s) is still %s after %.0fs; leaving it to the reconciler",
                    job_id, mode, last.status, budget,
                )
                return last
            self._sleep(delay)

    def run(
        self,
        data: bytes,
        filename: str,
        mode: str,
        idempotency_key: str,
        *,
        budget_seconds: Optional[float] = None,
        lang: Optional[str] = None,
    ) -> tuple[JobHandle, JobOutcome]:
        """Submit and wait. The handle is returned even when the wait times out,
        because the job id is the only thing that makes the work recoverable."""
        handle = self.submit(data, filename, mode, idempotency_key, lang=lang)
        outcome = self.wait(handle.job_id, mode, budget_seconds)
        return handle, outcome

    def retry_job(self, job_id: str) -> JobHandle:
        """Ask the service to re-run a failed job it still retains."""
        try:
            accepted = self.client.jobs.retry(job_id)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return JobHandle(
            job_id=str(getattr(accepted, "job_id", job_id)),
            mode=str(getattr(accepted, "mode", "")),
            status=str(getattr(accepted, "status", QUEUED)),
            duplicate=bool(getattr(accepted, "duplicate", False)),
            status_url=str(getattr(accepted, "status_url", "") or ""),
        )

    def queue_stats(self) -> Dict[str, Any]:
        """Queue depth, for the ops endpoint. Never raises — it is a gauge."""
        try:
            return _as_dict(self.client.jobs.stats()) or {}
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not read OCR queue stats: %s", exc)
            return {}
