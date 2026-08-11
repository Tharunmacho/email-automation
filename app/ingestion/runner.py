"""Poll Gmail and drive the pipeline over each candidate message.

This is the top-level 'batch' the CLI (`run-once`, `watch`) and the Celery beat
schedule both call. It owns Gmail post-processing (mark read / label) so a
message is only marked done once its resumes are safely stored.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, List

from app.config import settings
from app.email_client import get_email_client, GmailClient
from app.ingestion.pipeline import IngestionPipeline, ProcessResult
from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class BatchSummary:
    fetched: int = 0
    processed: int = 0
    skipped: int = 0
    # Re-fetched emails belonging to a candidate the user deleted. Counted apart
    # from skips: these are the delete holding, not the pipeline declining work.
    suppressed: int = 0
    errors: int = 0
    ingested_candidates: int = 0
    results: List[ProcessResult] = field(default_factory=list)


class IngestionRunner:
    def __init__(self, gmail: Any | None = None, pipeline: IngestionPipeline | None = None):
        self._custom_gmail = gmail is not None
        self.gmail = gmail or get_email_client()
        self.pipeline = pipeline or IngestionPipeline()

    def run_once(self, query: str | None = None) -> BatchSummary:
        summary = BatchSummary()
        effective_query = query if query is not None else settings.gmail_query
        message_ids = self.gmail.search_message_ids(query=effective_query)
        summary.fetched = len(message_ids)
        log.info("Fetched %d message(s) matching query '%s'", summary.fetched, effective_query)

        import concurrent.futures

        def _process_one_message(mid: str) -> ProcessResult | None:
            # Use custom runner client if passed explicitly (e.g. test stub), else fresh client per thread for SSL thread-safety
            gmail_client = self.gmail if self._custom_gmail else get_email_client()
            try:
                email = gmail_client.get_message(mid)
                result = self.pipeline.process_email(email, gmail=gmail_client)
            except Exception as exc:  # noqa: BLE001
                log.exception("Failed to process message %s", mid)
                return ProcessResult(message_id=mid, status="error", reason=f"Message processing failed: {exc}")

            # Post-processing gets its own guard. The candidate is already in
            # Mongo by this point, so a Gmail hiccup here must not discard the
            # result — that reported "Ingested Candidates=0" for a poll that had
            # just written a profile.
            try:
                if result.status == "suppressed":
                    # Re-fetched only because Gmail's search index had not caught
                    # up with the delete. Re-assert the label; never stamp this
                    # one "processed", which is how a deleted email ended up
                    # carrying both labels at once.
                    if settings.gmail_deleted_label:
                        gmail_client.apply_label(mid, settings.gmail_deleted_label)
                    if settings.gmail_processed_label:
                        gmail_client.remove_label(mid, settings.gmail_processed_label)
                elif result.status == "processed":
                    # Only move/label messages that were successfully processed as candidate resumes.
                    # All non-resume (skipped) emails remain untouched in the Inbox.
                    if settings.gmail_mark_read:
                        gmail_client.mark_read(mid)
                    if settings.gmail_processed_label:
                        gmail_client.apply_label(mid, settings.gmail_processed_label)
            except Exception as err:  # noqa: BLE001
                log.warning(
                    "Processed %s but could not mark it done in Gmail (%s); "
                    "it will be re-fetched and skipped as a duplicate next poll",
                    mid, err,
                )

            return result

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
                    elif res.status == "suppressed":
                        summary.suppressed += 1
                    else:
                        summary.errors += 1

        log.info(
            "Batch done: fetched=%d processed=%d skipped=%d suppressed=%d errors=%d candidates=%d",
            summary.fetched, summary.processed, summary.skipped,
            summary.suppressed, summary.errors, summary.ingested_candidates,
        )
        # Per-message detail, so a poll that ingests nothing says why.
        for res in summary.results:
            if res.status == "processed":
                continue
            detail = "; ".join(
                f"{a.filename}: {a.status}" + (f" ({a.detail})" if a.detail else "")
                for a in res.attachments
            ) or res.reason
            log.info("  %s -> %s | %s", res.message_id, res.status, detail)
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
