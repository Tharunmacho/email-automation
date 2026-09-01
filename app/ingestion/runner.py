"""Poll Gmail and drive the pipeline over each candidate message.

This is the top-level 'batch' the CLI (`run-once`, `watch`) and the Celery beat
schedule both call. It owns Gmail post-processing (mark read / label) so a
message is only marked done once its resumes are safely stored.
"""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Any, List, Sequence

from app.config import settings
from app.email_client import get_email_client, GmailClient
from app.ingestion.pipeline import IngestionPipeline, ProcessResult
from app.logging_config import get_logger
from app.tasks.locks import claim_message

log = get_logger(__name__)


def _identity_kwargs(email: Any) -> dict:
    """The stable way to address this message again, if we know it.

    A UID stops addressing anything the moment the message is filed into another
    folder, so the RFC822 ``Message-ID`` — carried on `EmailMessage.thread_id`
    for IMAP accounts — is what a later re-label has to search on. Only real
    strings are passed on: a client that does not supply them keeps the plain
    two-argument call it has always had.
    """
    found = {}
    for key, attr in (("rfc_message_id", "thread_id"), ("subject", "subject"),
                      ("from_addr", "from_addr")):
        value = getattr(email, attr, None)
        if isinstance(value, str) and value.strip():
            found[key] = value.strip()
    return found


def mark_message_done(
    gmail,
    message_id: str,
    status: str,
    email: Any = None,
    attachments: Sequence[Any] = (),
) -> None:
    """Gmail-side bookkeeping for a message the pipeline has finished with.

    Only messages that were actually processed as candidate resumes are marked
    read and labelled; non-resume emails stay untouched in the inbox. A
    *suppressed* message — re-fetched only because Gmail's search index had not
    caught up with a delete — has its `deleted` label re-asserted instead, and
    is never stamped "processed", which is how a retired email ended up carrying
    both labels at once.

    ``attachments`` is what separates the two kinds of *skip*. A message the
    detector never accepted has none, and is somebody's ordinary mail that we
    leave alone. A message that produced attachment verdicts and still skipped
    is one the pipeline is permanently finished with — every résumé on it was
    refused on nationality, was not a résumé, or was already ingested — because
    a single retryable failure would have made the whole message an `error`
    instead. Those have to be labelled: without it the poll re-fetched a
    nationality-rejected CV every time and paid for a full local OCR and a
    Veris parse to reach the same refusal, for ever.

    Shared by the batch runner and the per-message Celery task, so the two paths
    cannot drift into labelling the same outcome differently.
    """
    where = _identity_kwargs(email)

    if status == "skipped" and attachments:
        status = "processed"

    if status == "suppressed":
        # Apply before remove, always. On a folder-based account applying the
        # label *is* a move, and the move is what takes the message out of the
        # old folder — so removing first would only leave it somewhere the move
        # then has to find it again.
        if settings.gmail_deleted_label:
            gmail.apply_label(message_id, settings.gmail_deleted_label, **where)
        if settings.gmail_processed_label:
            gmail.remove_label(message_id, settings.gmail_processed_label, **where)
    elif status == "processed":
        if settings.gmail_mark_read:
            gmail.mark_read(message_id)
        if settings.gmail_processed_label:
            gmail.apply_label(message_id, settings.gmail_processed_label, **where)
    elif status == "error":
        # Left in the inbox on purpose. An error is retryable — OCR was down,
        # Mongo blinked — and filing it as processed is what would hide it from
        # the next poll forever. It is only marked read so the operator can see
        # the runner has been through it.
        if settings.gmail_mark_read:
            gmail.mark_read(message_id)


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


def _account_label(client: Any) -> str:
    """The mailbox a client speaks for, for the log line that counts them.

    Best effort: an IMAP client knows its own username and anything else is
    named by its type. The bare count was not enough to debug with — "1
    account(s)" reads the same whether that one is the mailbox you meant or the
    `.env` fallback quietly standing in for the two you configured.

    The type check is not defensive padding. `getattr` on a client that
    synthesises attributes — a Mock in the tests, a proxy in principle — hands
    back an object rather than a name, and joining that raised a `TypeError`
    from inside the log call, taking the whole batch down. A line that only
    describes the work must never be able to stop it.
    """
    label = getattr(client, "imap_username", None)
    return label if isinstance(label, str) and label else type(client).__name__


class IngestionRunner:
    def __init__(
        self,
        clients: List[Any] | None = None,
        pipeline: IngestionPipeline | None = None,
        gmail: Any | None = None,
    ):
        if clients is not None:
            self.clients = clients
        elif gmail is not None:
            self.clients = [gmail]
        else:
            from app.email_client import get_all_email_clients
            self.clients = get_all_email_clients()
        self.pipeline = pipeline or IngestionPipeline()

    def run_once(self, query: str | None = None) -> BatchSummary:
        summary = BatchSummary()
        effective_query = query if query is not None else settings.gmail_query
        
        # Collect message IDs from all clients concurrently
        client_messages = []
        if len(self.clients) <= 1:
            for client in self.clients:
                mids = client.search_message_ids(query=effective_query)
                if mids:
                    client_messages.extend([(client, mid) for mid in mids])
        else:
            def _search_client(c: Any) -> list[tuple[Any, str]]:
                try:
                    mids = c.search_message_ids(query=effective_query)
                    return [(c, mid) for mid in mids] if mids else []
                except Exception as err:
                    log.warning("Search failed for client %s: %s", getattr(c, "imap_username", c), err)
                    return []

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.clients)) as search_exec:
                futures = [search_exec.submit(_search_client, client) for client in self.clients]
                for fut in concurrent.futures.as_completed(futures):
                    client_messages.extend(fut.result())
                
        summary.fetched = len(client_messages)
        log.info(
            "Fetched %d message(s) across %d account(s) [%s] matching query '%s'",
            summary.fetched,
            len(self.clients),
            ", ".join(_account_label(c) for c in self.clients),
            effective_query,
        )

        def _process_one_message(client: Any, mid: str) -> ProcessResult | None:
            # Claim the message first. Beat fans out one Celery task per email
            # and gives the poll lock straight back, so a manual sync starting a
            # minute later re-fetches messages that are still being extracted —
            # the claim, not the poll lock, is what keeps the two off each other.
            with claim_message(mid) as claimed:
                if not claimed:
                    return ProcessResult(
                        mid, "skipped", "already being processed by another worker"
                    )

                try:
                    email = _fetch_with_retry(client, mid)
                    result = self.pipeline.process_email(email, gmail=client)
                except Exception:  # noqa: BLE001
                    log.exception("Failed to process message %s", mid)
                    return None

                # Post-processing gets its own guard. The candidate is already in
                # Mongo by this point, so a Gmail hiccup here must not discard the
                # result — that reported "Ingested Candidates=0" for a poll that had
                # just written a profile.
                try:
                    mark_message_done(
                        client, mid, result.status, email=email,
                        attachments=result.attachments,
                    )
                except Exception as err:  # noqa: BLE001
                    log.warning(
                        "Processed %s but could not mark it done in Gmail (%s); "
                        "it will be re-fetched and skipped as a duplicate next poll",
                        mid, err,
                    )

                return result

        # Bounded by the batch, then by the setting. The threads are
        # I/O-bound (Gmail, Veris, the LLM), so this sits well above the
        # core count; what stops us flooding Veris is the gateway's
        # in-flight cap, not this number.
        max_workers = min(max(1, settings.ingestion_max_workers), max(1, len(client_messages)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_msg = {executor.submit(_process_one_message, client, mid): mid for client, mid in client_messages}
            for future in concurrent.futures.as_completed(future_to_msg):
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
            log.debug("  %s -> %s | %s", res.message_id, res.status, detail)

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
