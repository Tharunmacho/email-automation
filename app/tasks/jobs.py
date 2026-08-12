"""Celery tasks.

Two ways to poll, for two different callers:

* `poll_gmail` — what beat runs. It searches the mailbox under a short lock,
  queues one `process_message` task per email, and returns. The resumes are then
  extracted concurrently, one per worker slot, and a tick that lands while the
  previous batch is still being processed queues its own work instead of being
  turned away.
* `run_poll_cycle` runs a whole batch in one task via `IngestionRunner`, which
  fans out across a thread pool and returns a complete summary. The API uses it
  for a manual sync, where the UI has to report what happened.

Both take the poll lock, so the two styles never search the mailbox at the same
time. That is no longer what stops the *work* overlapping — `poll_gmail` gives
the lock back as soon as it has dispatched — so both paths additionally claim
each message before touching it.
"""
from __future__ import annotations

from app.config import settings
from app.email_client import get_email_client, GmailClient
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.runner import BatchSummary, IngestionRunner, mark_message_done
from app.logging_config import get_logger
from app.tasks.celery_app import celery_app
from app.tasks.locks import POLL_LOCK, LockNotAcquired, claim_message, redis_lock

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
def poll_gmail(query: str | None = None) -> dict:
    """Search the mailbox and queue one `process_message` per email.

    The lock covers the search and the dispatch only — seconds — so the next
    scheduled tick finds it free even while a hundred resumes are still being
    extracted. Re-dispatching a message that is already in flight is harmless:
    the task claims it, finds the claim held, and returns.
    """
    try:
        with redis_lock(POLL_LOCK, settings.poll_dispatch_lock_ttl_seconds):
            gmail = get_email_client()
            ids = gmail.search_message_ids(query=query)
            for mid in ids:
                process_message.delay(mid)
            log.info("Dispatched %d message(s) for processing", len(ids))
            return {"dispatched": len(ids), "message_ids": ids}
    except LockNotAcquired:
        # Only a manual sync can hold the lock long enough to cause this now,
        # and the work it is doing is the work this tick would have queued.
        log.info("Fan-out poll skipped: another cycle is already running")
        return {
            "dispatched": 0,
            "message_ids": [],
            "skipped_reason": "Another poll cycle is already running.",
        }


@celery_app.task(
    name="app.tasks.jobs.process_message",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_message(self, message_id: str) -> dict:
    """One email, end to end: claim it, process it, mark it done in Gmail."""
    return _process_one(self, message_id)


@celery_app.task(
    name="app.tasks.jobs.process_single_message",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_single_message(self, message_id: str) -> dict:
    """Alias for `process_message`, under the name the fan-out contract uses.

    Registered as a second task rather than renamed: a rename would strand every
    `process_message` already sitting in Redis when the workers restart, and
    those are real candidate emails. Both names take the same per-message claim
    lock, so a message dispatched under either one is still processed once.
    """
    return _process_one(self, message_id)


def _process_one(task, message_id: str) -> dict:
    """The body both task names share. `task` is only used to retry."""
    with claim_message(message_id) as claimed:
        if not claimed:
            # Another worker has it — either a re-dispatch from the next beat
            # tick, or a manual sync that fetched the same unlabelled message.
            return {"message_id": message_id, "status": "skipped",
                    "reason": "already being processed", "candidates": []}

        gmail = get_email_client()
        pipeline = IngestionPipeline()
        try:
            email = gmail.get_message(message_id)
            result = pipeline.process_email(email, gmail=gmail)
        except Exception as exc:  # noqa: BLE001
            log.exception("process_message failed for %s", message_id)
            raise task.retry(exc=exc)

        # Post-processing gets its own guard, matching IngestionRunner: the
        # candidate is already in Mongo, so a Gmail hiccup here must not turn a
        # successful ingestion into a retry that ingests it all over again.
        try:
            mark_message_done(gmail, message_id, result.status)
        except Exception as err:  # noqa: BLE001
            log.warning(
                "Processed %s but could not mark it done in Gmail (%s); "
                "it will be re-fetched and skipped as a duplicate next poll",
                message_id, err,
            )

        return {
            "message_id": message_id,
            "status": result.status,
            "reason": result.reason,
            "candidates": result.ingested_ids,
        }


@celery_app.task(name="app.tasks.jobs.process_single_message")
def process_single_message(message_id: str) -> dict:
    """Alias for `process_message`, under the name the fan-out contract uses.

    Registered as its own task rather than renaming the original: a rename would
    strand every `process_message` already sitting in Redis when the workers
    restart, and those are real candidate emails. Both names route to the same
    claim lock, so dispatching a message under either one still guarantees a
    single worker processes it.
    """
    return process_message.run(message_id)
