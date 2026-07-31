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
        effective_query = query if query is not None else settings.gmail_query
        message_ids = self.gmail.search_message_ids(query=effective_query)
        summary.fetched = len(message_ids)
        log.info("Fetched %d message(s) matching query '%s'", summary.fetched, effective_query)

        import concurrent.futures

        def _process_one_message(mid: str) -> ProcessResult | None:
            # Use thread-local Gmail client for thread-safety
            gmail_client = GmailClient()
            try:
                email = gmail_client.get_message(mid)
                result = self.pipeline.process_email(email, gmail=gmail_client)
                if result.status == "processed":
                    if settings.gmail_mark_read:
                        gmail_client.mark_read(mid)
                    if settings.gmail_processed_label:
                        gmail_client.apply_label(mid, settings.gmail_processed_label)
                elif result.status == "skipped" and settings.gmail_mark_read:
                    gmail_client.mark_read(mid)
                return result
            except Exception:  # noqa: BLE001
                log.exception("Failed to process message %s", mid)
                return None

        max_workers = min(10, max(1, len(message_ids)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_mid = {executor.submit(_process_one_message, mid): mid for mid in message_ids}
            for future in concurrent.futures.as_completed(future_to_mid):
                res = future.result()
                if res is None:
                    summary.errors += 1
                else:
                    summary.results.append(res)
                    if res.status == "processed":
                        summary.processed += 1
                        summary.ingested_candidates += len(res.ingested_ids)
                    elif res.status == "skipped":
                        summary.skipped += 1
                    else:
                        summary.errors += 1

        log.info(
            "Batch done: fetched=%d processed=%d skipped=%d errors=%d candidates=%d",
            summary.fetched, summary.processed, summary.skipped,
            summary.errors, summary.ingested_candidates,
        )
        return summary

    def watch(self, interval_seconds: int = 60, query: str | None = None) -> None:
        log.info("Watching Gmail every %ds (auto-recovering background mode)", interval_seconds)
        backoff = 0
        while True:
            try:
                self.run_once(query=query)
                backoff = 0
            except Exception as exc:  # noqa: BLE001
                backoff = min(backoff + 10, 60)
                log.warning(
                    "Poll cycle encountered error (%s); auto-recovering in %ds...",
                    exc, backoff,
                )
                time.sleep(backoff)
                continue
            time.sleep(interval_seconds)

    def _finalize(self, message_id: str) -> None:
        if settings.gmail_mark_read:
            self.gmail.mark_read(message_id)
        if settings.gmail_processed_label:
            self.gmail.apply_label(message_id, settings.gmail_processed_label)
