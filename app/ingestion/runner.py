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
from app.tasks.locks import claim_message

log = get_logger(__name__)


def mark_message_done(gmail, message_id: str, status: str) -> None:
    """Gmail-side bookkeeping for a message the pipeline has finished with.

    Only messages that were actually processed as candidate resumes are marked
    read and labelled; non-resume emails stay untouched in the inbox. A
    *suppressed* message — re-fetched only because Gmail's search index had not
    caught up with a delete — has its `deleted` label re-asserted instead, and
    is never stamped "processed", which is how a retired email ended up carrying
    both labels at once.

    Shared by the batch runner and the per-message Celery task, so the two paths
    cannot drift into labelling the same outcome differently.
    """
    if status == "suppressed":
        if settings.gmail_deleted_label:
            gmail.apply_label(message_id, settings.gmail_deleted_label)
        if settings.gmail_processed_label:
            gmail.remove_label(message_id, settings.gmail_processed_label)
    elif status in ("processed", "skipped", "error"):
        if settings.gmail_mark_read:
            gmail.mark_read(message_id)
        if settings.gmail_processed_label:
            gmail.apply_label(message_id, settings.gmail_processed_label)


# A dropped connection is not a bad email. Fetching a message is a pure read, so
# it is safe to repeat — unlike the pipeline behind it, which writes a candidate.
_FETCH_ATTEMPTS = 3


def _fetch_with_retry(gmail, message_id: str):
    """Read one message, riding out a transport hiccup rather than failing it.

    Without this a single dropped socket cost the batch a whole email: the
    message stayed unlabelled and was silently re-fetched next poll, so the run
    that dropped it just reported one fewer candidate than the inbox held.
    """
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            return gmail.get_message(message_id)
        except Exception as err:  # noqa: BLE001 — any transport failure is retryable
            if attempt == _FETCH_ATTEMPTS:
                raise
            log.warning(
                "Fetching message %s failed (%s); retrying (%d/%d)",
                message_id, err, attempt, _FETCH_ATTEMPTS,
            )
            time.sleep(attempt)


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
            # Claim the message first. Beat fans out one Celery task per email
            # and gives the poll lock straight back, so a manual sync starting a
            # minute later re-fetches messages that are still being extracted —
            # the claim, not the poll lock, is what keeps the two off each other.
            with claim_message(mid) as claimed:
                if not claimed:
                    return ProcessResult(
                        mid, "skipped", "already being processed by another worker"
                    )

                gmail_client = self.gmail
                try:
                    email = _fetch_with_retry(gmail_client, mid)
                    result = self.pipeline.process_email(email, gmail=gmail_client)
                except Exception:  # noqa: BLE001
                    log.exception("Failed to process message %s", mid)
                    return None

                # Post-processing gets its own guard. The candidate is already in
                # Mongo by this point, so a Gmail hiccup here must not discard the
                # result — that reported "Ingested Candidates=0" for a poll that had
                # just written a profile.
                try:
                    mark_message_done(gmail_client, mid, result.status)
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

        # Auto-assign any unallocated candidates remaining in MongoDB Atlas
        try:
            if self.pipeline.repo.unassigned_count() > 0:
                from app.assignment import rebalance_all
                rebalance_res = rebalance_all(repo=self.pipeline.repo)
                log.info("Post-poll auto-assignment: %s candidate(s) allocated", rebalance_res.get("moved", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("Post-poll auto-assignment step failed: %s", exc)

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
