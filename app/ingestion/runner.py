"""Poll Gmail and drive the pipeline over each candidate message.

This is the top-level 'batch' the CLI (`run-once`, `watch`) and the Celery beat
schedule both call. It owns Gmail post-processing (mark read / label) so a
message is only marked done once its resumes are safely stored.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List

from app.config import settings
from app.gmail.client import GmailClient
from app.ingestion.pipeline import IngestionPipeline, ProcessResult
from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class BatchSummary:
    fetched: int = 0
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    ingested_candidates: int = 0
    results: List[ProcessResult] = field(default_factory=list)


class IngestionRunner:
    def __init__(self, gmail: GmailClient | None = None, pipeline: IngestionPipeline | None = None):
        self.gmail = gmail or GmailClient()
        self.pipeline = pipeline or IngestionPipeline()

    def run_once(self, query: str | None = None) -> BatchSummary:
        summary = BatchSummary()
        message_ids = self.gmail.search_message_ids(query=query)
        summary.fetched = len(message_ids)
        log.info("Fetched %d message(s) matching query", summary.fetched)

        for mid in message_ids:
            try:
                email = self.gmail.get_message(mid)
                result = self.pipeline.process_email(email, gmail=self.gmail)
                summary.results.append(result)

                if result.status == "processed":
                    summary.processed += 1
                    summary.ingested_candidates += len(result.ingested_ids)
                    self._finalize(mid)
                elif result.status == "skipped":
                    summary.skipped += 1
                    # Optionally still mark read so we don't re-scan it forever.
                    if settings.gmail_mark_read:
                        self.gmail.mark_read(mid)
                else:
                    summary.errors += 1
            except Exception:  # noqa: BLE001
                summary.errors += 1
                log.exception("Failed to process message %s", mid)

        log.info(
            "Batch done: fetched=%d processed=%d skipped=%d errors=%d candidates=%d",
            summary.fetched, summary.processed, summary.skipped,
            summary.errors, summary.ingested_candidates,
        )
        return summary

    def watch(self, interval_seconds: int = 60, query: str | None = None) -> None:
        log.info("Watching Gmail every %ds (Ctrl+C to stop)", interval_seconds)
        while True:
            try:
                self.run_once(query=query)
            except Exception:  # noqa: BLE001
                log.exception("Poll cycle failed; will retry next interval")
            time.sleep(interval_seconds)

    def _finalize(self, message_id: str) -> None:
        if settings.gmail_mark_read:
            self.gmail.mark_read(message_id)
        if settings.gmail_processed_label:
            self.gmail.apply_label(message_id, settings.gmail_processed_label)
