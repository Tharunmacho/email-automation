"""Celery tasks.

`poll_gmail` finds candidate messages and dispatches one `process_message` task
per message, so heavy per-resume work (OCR, LLM) is distributed across workers.
"""
from __future__ import annotations

from app.gmail.client import GmailClient
from app.ingestion.pipeline import IngestionPipeline
from app.logging_config import get_logger
from app.tasks.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="app.tasks.jobs.poll_gmail")
def poll_gmail() -> dict:
    gmail = GmailClient()
    ids = gmail.search_message_ids()
    for mid in ids:
        process_message.delay(mid)
    return {"dispatched": len(ids)}


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
        if result.status == "processed":
            from app.config import settings

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
