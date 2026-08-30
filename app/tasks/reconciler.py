"""The reconciler: nothing is allowed to stay stuck.

Everything else in the ingestion path is optimistic. The pipeline submits a job
and waits for it; when the wait runs out it walks away and lets the candidate
record be created from what it has. That is the right trade — a passport that
takes four minutes must not hold a Gmail message open — but it only works if
something comes back for the rows that were left behind.

This is that something. On each pass it takes the rows that have not moved for
`reconciler_stuck_after_seconds` and pushes each one step further:

* **has a job id** → poll it. Finished? Store the result and close the row.
  Still running? Touch it, so it drops off the stuck list until it goes quiet
  again — a healthy four-minute job is not a stuck one.
* **no job id** (died between claiming the row and getting an answer) →
  re-submit under the *same* idempotency key, so if the original submission did
  land, the service hands back the job already running instead of queueing a
  second copy of the same extraction.
* **out of attempts** → abandon it into the review queue, where a human can see
  it. Not retried silently forever, and not deleted either.

The bytes are recovered from object storage where the attachment was kept, and
from the mailbox where it was not — because a row whose résumé pass failed never
got as far as storing anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core.models import Attachment
from app.db.ingestion_state import (
    ABANDONED,
    FAILED,
    IngestionRow,
    IngestionStateStore,
)
from app.extraction.jobs import AsyncOCRJobClient, OCRJobError
from app.ingestion.multipass import IDENTITY_MODES, MultipassExtractor
from app.logging_config import get_logger
from app.tasks.celery_app import celery_app
from app.tasks.locks import LockNotAcquired, redis_lock

log = get_logger(__name__)

RECONCILE_LOCK = "ocr:reconciler"


@dataclass
class ReconcileReport:
    """What one pass did, in the shape the API and the logs both want."""

    scanned: int = 0
    completed: int = 0
    still_running: int = 0
    resubmitted: int = 0
    failed: int = 0
    abandoned: int = 0
    skipped: int = 0
    details: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scanned": self.scanned,
            "completed": self.completed,
            "still_running": self.still_running,
            "resubmitted": self.resubmitted,
            "failed": self.failed,
            "abandoned": self.abandoned,
            "skipped": self.skipped,
            "details": self.details,
        }


class Reconciler:
    """Drives stuck ingestion rows to a conclusion."""

    def __init__(
        self,
        state: Optional[IngestionStateStore] = None,
        client: Optional[AsyncOCRJobClient] = None,
        extractor: Optional[MultipassExtractor] = None,
        storage=None,
        email_client=None,
    ):
        self._state = state
        self._client = client
        self._extractor = extractor
        self._storage = storage
        self._email_client = email_client

    @property
    def state(self) -> IngestionStateStore:
        if self._state is None:
            self._state = IngestionStateStore()
        return self._state

    @property
    def client(self) -> AsyncOCRJobClient:
        if self._client is None:
            self._client = AsyncOCRJobClient()
        return self._client

    @property
    def extractor(self) -> MultipassExtractor:
        if self._extractor is None:
            self._extractor = MultipassExtractor(state=self.state, client=self.client)
        return self._extractor

    # ------------------------------------------------------------------ #
    def run_once(self, limit: Optional[int] = None) -> ReconcileReport:
        """One sweep over the stuck rows. Never raises; always reports."""
        report = ReconcileReport()
        rows = self.state.find_stuck(
            settings.reconciler_stuck_after_seconds,
            limit if limit is not None else settings.reconciler_batch_size,
        )
        report.scanned = len(rows)
        if not rows:
            return report

        log.info("Reconciler: %d stuck ingestion row(s)", len(rows))
        for row in rows:
            try:
                self._advance(row, report)
            except Exception as exc:  # noqa: BLE001 — one row never stops the sweep
                log.exception("Reconciler failed on row %s (%s)", row.id, row.ocr_mode)
                report.failed += 1
                report.details.append(
                    {"row_id": row.id, "mode": row.ocr_mode, "outcome": "error", "detail": str(exc)}
                )
        return report

    # ------------------------------------------------------------------ #
    def _advance(self, row: IngestionRow, report: ReconcileReport) -> None:
        if row.attempts >= settings.ocr_job_max_attempts and not row.ocr_job_id:
            self.state.mark_failed(row.id, row.last_error or "attempts exhausted",
                                   settings.ocr_job_max_attempts)
            report.abandoned += 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": "abandoned",
                 "detail": "no job id and no attempts left"}
            )
            return

        if row.ocr_job_id:
            self._poll_existing(row, report)
            return

        self._resubmit(row, report)

    # ------------------------------------------------------------------ #
    def _poll_existing(self, row: IngestionRow, report: ReconcileReport) -> None:
        """Ask about the job we already have. One read, no waiting."""
        try:
            outcome = self.client.poll(row.ocr_job_id or "", row.ocr_mode)
        except OCRJobError as exc:
            if exc.retryable:
                # The service is unreachable or busy. The job is still its
                # problem, not ours; try again next sweep.
                self.state.touch(row.id)
                report.still_running += 1
                report.details.append(
                    {"row_id": row.id, "mode": row.ocr_mode, "outcome": "deferred",
                     "detail": str(exc)}
                )
                return
            # A 404 on the job id: the service no longer retains it, so the only
            # way forward is a fresh submission. Clearing the id is what lets
            # `_resubmit` take over on the next pass.
            self.state.mark_failed(
                row.id, str(exc), settings.ocr_job_max_attempts, clear_job=True
            )
            report.failed += 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": "lost", "detail": str(exc)}
            )
            return

        if outcome.pending:
            self.state.touch(row.id)
            report.still_running += 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": "running",
                 "job_id": outcome.job_id}
            )
            return

        self._settle(row, outcome, report)

    # ------------------------------------------------------------------ #
    def _resubmit(self, row: IngestionRow, report: ReconcileReport) -> None:
        """Re-drive a row that never got a job id, under the same key."""
        if row.ocr_mode not in IDENTITY_MODES:
            # A résumé row is re-driven by the mail itself: the message was
            # never labelled done, so the next poll re-processes it and the
            # identical idempotency key re-attaches to any job still running.
            # Doing it here as well would race the pipeline for the same row.
            report.skipped += 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": "skipped",
                 "detail": "resume rows are re-driven by the Gmail poll"}
            )
            return

        if not self.state.claim_for_submit(row.id, settings.ocr_job_max_attempts):
            current = self.state.get(row.id) or row
            if current.status in (FAILED, ABANDONED) or current.attempts >= settings.ocr_job_max_attempts:
                self.state.mark_failed(
                    row.id, current.last_error or "attempts exhausted",
                    settings.ocr_job_max_attempts,
                )
                report.abandoned += 1
                report.details.append(
                    {"row_id": row.id, "mode": row.ocr_mode, "outcome": "abandoned",
                     "detail": current.last_error or "attempts exhausted"}
                )
            else:
                report.skipped += 1
                report.details.append(
                    {"row_id": row.id, "mode": row.ocr_mode, "outcome": "skipped",
                     "detail": f"claimed elsewhere (status={current.status})"}
                )
            return

        data = self._load_bytes(row)
        if data is None:
            status = self.state.mark_failed(
                row.id,
                "source attachment could not be recovered from storage or the mailbox",
                settings.ocr_job_max_attempts,
            )
            report.abandoned += 1 if status == ABANDONED else 0
            report.failed += 0 if status == ABANDONED else 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": status,
                 "detail": "source bytes unavailable"}
            )
            return

        payload, name = MultipassExtractor._payload_for(data, row.pages, row.filename, row.ocr_mode)
        try:
            # Keyed on the bytes as well as the mail — see `MultipassExtractor`.
            from app.extraction.jobs import content_key

            handle = self.client.submit(
                payload, name, row.ocr_mode,
                f"{row.idempotency_key}/{content_key(payload)}",
            )
        except OCRJobError as exc:
            status = self.state.mark_failed(row.id, str(exc), settings.ocr_job_max_attempts)
            report.abandoned += 1 if status == ABANDONED else 0
            report.failed += 0 if status == ABANDONED else 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": status, "detail": str(exc)}
            )
            return

        self.state.mark_submitted(row.id, handle.job_id)
        report.resubmitted += 1
        log.info(
            "Reconciler re-submitted %s row %s as job %s (duplicate=%s)",
            row.ocr_mode, row.id, handle.job_id, handle.duplicate,
        )

        # A short wait, because a re-attached duplicate is very often already
        # finished and closing the row now saves a whole sweep.
        outcome = self.client.wait(
            handle.job_id, row.ocr_mode, settings.reconciler_job_wait_seconds
        )
        if outcome.pending:
            self.state.touch(row.id)
            report.still_running += 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": "resubmitted",
                 "job_id": handle.job_id}
            )
            return

        fresh = self.state.get(row.id) or row
        self._settle(fresh, outcome, report)

    # ------------------------------------------------------------------ #
    def _settle(self, row: IngestionRow, outcome, report: ReconcileReport) -> None:
        """A terminal job: store what it produced, or record why it failed."""
        if row.ocr_mode in IDENTITY_MODES:
            result = self.extractor.complete(row, outcome)
            if result.status == "succeeded":
                report.completed += 1
            elif result.status == "abandoned":
                report.abandoned += 1
            else:
                report.failed += 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": result.status,
                 "job_id": outcome.job_id, "record_id": result.record_id,
                 "detail": result.detail}
            )
            return

        # A résumé job. There is nowhere to put the text from here — building a
        # candidate needs dedup, the confidence gate and the auto-reply, all of
        # which live in the pipeline. Closing the row is still worth doing: it
        # records that the extraction itself is done, and the next poll of the
        # unlabelled mail gets the result back instantly from the same key.
        if outcome.succeeded:
            self.state.mark_succeeded(row.id, candidate_id=row.candidate_id)
            report.completed += 1
            report.details.append(
                {"row_id": row.id, "mode": row.ocr_mode, "outcome": "succeeded",
                 "job_id": outcome.job_id,
                 "detail": "extraction ready; the mail poll will ingest it"}
            )
            return

        status = self.state.mark_failed(
            row.id, outcome.error or "OCR job failed", settings.ocr_job_max_attempts
        )
        report.abandoned += 1 if status == ABANDONED else 0
        report.failed += 0 if status == ABANDONED else 1
        report.details.append(
            {"row_id": row.id, "mode": row.ocr_mode, "outcome": status,
             "job_id": outcome.job_id, "detail": outcome.error}
        )

    # ------------------------------------------------------------------ #
    def _load_bytes(self, row: IngestionRow) -> Optional[bytes]:
        """The original attachment, from wherever it still exists.

        Object storage first — it is cheap, local and always the exact bytes
        that were classified. The mailbox is the fallback for rows whose résumé
        pass failed before anything was stored.
        """
        if row.storage_key:
            try:
                storage = self._storage
                if storage is None:
                    from app.storage.factory import get_storage_backend

                    storage = self._storage = get_storage_backend()
                return storage.load(row.storage_key)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read %s from storage: %s", row.storage_key, exc)

        try:
            client = self._email_client
            if client is None:
                from app.email_client import get_email_client

                client = self._email_client = get_email_client()
            attachment = Attachment(
                filename=row.filename or "attachment.pdf",
                mime_type="application/octet-stream",
                size=0,
                attachment_id=row.attachment_id,
            )
            return client.download_attachment(row.message_id, attachment)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Could not re-download attachment %s of message %s: %s",
                row.attachment_id, row.message_id, exc,
            )
            return None


# --------------------------------------------------------------------------- #
#  Entry points
# --------------------------------------------------------------------------- #
def reconcile_once(limit: Optional[int] = None) -> Dict[str, Any]:
    """One sweep, callable from the API, the CLI or a test."""
    return Reconciler().run_once(limit).as_dict()


@celery_app.task(name="app.tasks.reconciler.reconcile_ocr_jobs")
def reconcile_ocr_jobs(limit: int | None = None) -> Dict[str, Any]:
    """The beat task. Single-flighted, because two sweeps would race each other
    for the same rows and double-submit the ones without a job id."""
    try:
        with redis_lock(RECONCILE_LOCK, settings.reconciler_stuck_after_seconds):
            report = Reconciler().run_once(limit)
            if report.scanned:
                log.info("Reconciler pass: %s", report.as_dict())
            return report.as_dict()
    except LockNotAcquired:
        log.debug("Reconciler skipped: a sweep is already running")
        return ReconcileReport(skipped=1).as_dict()


def review_queue(limit: int = 100) -> List[Dict[str, Any]]:
    """Rows a human has to deal with, newest failure last."""
    rows = IngestionStateStore().review_queue(limit)
    return [
        {
            "row_id": r.id,
            "mode": r.ocr_mode,
            "message_id": r.message_id,
            "filename": r.filename,
            "pages": r.pages,
            "candidate_id": r.candidate_id,
            "attempts": r.attempts,
            "last_error": r.last_error,
            "job_id": r.ocr_job_id,
            "received_at": r.received_at,
            "completed_at": r.completed_at,
        }
        for r in rows
    ]
