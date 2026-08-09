"""Celery tasks.

Two ways to poll, for two different volumes:

* `run_poll_cycle` runs a whole batch in one task via `IngestionRunner`, which
  already fans out across a thread pool. This is what the API and beat use — it
  returns a complete summary, so the caller can report what happened.
* `poll_gmail` dispatches one `process_message` task per message, so per-resume
  work spreads across separate worker processes. Worth switching to when a
  single batch stops fitting comfortably in one task.

Both take the same lock, so the two styles can never run on top of each other.
"""
from __future__ import annotations

from app.config import settings
from app.gmail.client import GmailClient
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.runner import BatchSummary, IngestionRunner
from app.logging_config import get_logger
from app.tasks.celery_app import celery_app
from app.tasks.locks import POLL_LOCK, LockNotAcquired, redis_lock

log = get_logger(__name__)


def summary_to_dict(summary: BatchSummary) -> dict:
    """Serialise a batch summary for the Celery result backend and the API.

    Both consumers need the identical shape, so this is the one place that
    defines it.
    """
    return {
        "fetched": summary.fetched,
        "processed": summary.processed,
        "skipped": summary.skipped,
        "suppressed": summary.suppressed,
        "errors": summary.errors,
        "ingested_candidates": summary.ingested_candidates,
        "results": [
            {
                "message_id": r.message_id,
                "status": r.status,
                "reason": r.reason,
                "attachments": [
                    {
                        "filename": a.filename,
                        "status": a.status,
                        "candidate_id": a.candidate_id,
                        "detail": a.detail,
                    }
                    for a in r.attachments
                ],
            }
            for r in summary.results
        ],
    }


@celery_app.task(name="app.tasks.jobs.run_poll_cycle")
def run_poll_cycle(query: str | None = None) -> dict:
    """One complete Gmail poll, start to finish, under the poll lock."""
    try:
        with redis_lock(POLL_LOCK, settings.poll_lock_ttl_seconds):
            summary = IngestionRunner().run_once(query=query)
            return summary_to_dict(summary)
    except LockNotAcquired:
        # A beat tick landing on top of a manual sync is routine, not a failure.
        # Reported rather than raised so the UI can say so plainly.
        log.info("Poll skipped: another cycle is already running")
        return {
            "fetched": 0,
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "ingested_candidates": 0,
            "results": [],
            "skipped_reason": "Another poll cycle is already running.",
        }


@celery_app.task(name="app.tasks.jobs.poll_gmail")
def poll_gmail() -> dict:
    try:
        with redis_lock(POLL_LOCK, settings.poll_lock_ttl_seconds):
            gmail = GmailClient()
            ids = gmail.search_message_ids()
            for mid in ids:
                process_message.delay(mid)
            return {"dispatched": len(ids)}
    except LockNotAcquired:
        log.info("Fan-out poll skipped: another cycle is already running")
        return {"dispatched": 0, "skipped_reason": "Another poll cycle is already running."}


@celery_app.task(
    name="app.tasks.jobs.process_message",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_message(self, message_id: str) -> dict:
    gmail = GmailClient()
    pipeline = IngestionPipeline()
    try:
        email = gmail.get_message(message_id)
        result = pipeline.process_email(email, gmail=gmail)
        # Label skipped messages too, matching IngestionRunner. The Gmail query
        # excludes the processed label, so an unlabelled message comes back on
        # every poll — which is how a deleted candidate kept reappearing.
        if result.status in ("processed", "skipped"):
            if settings.gmail_mark_read:
                gmail.mark_read(message_id)
            if settings.gmail_processed_label:
                gmail.apply_label(message_id, settings.gmail_processed_label)
        return {
            "message_id": message_id,
            "status": result.status,
            "candidates": result.ingested_ids,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("process_message failed for %s", message_id)
        raise self.retry(exc=exc)
