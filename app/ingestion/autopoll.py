"""Drain the mailboxes on a timer, from inside the API process — 24/7.

There is no Sync button any more, so this timer is the only thing that reads
the mailboxes. It is on by default (`mail_autopoll_enabled`) and it owns the
schedule outright: Celery beat does not poll mail at all.

It used to stand down whenever a Celery worker was online, on the assumption
that beat would poll instead. The pm2 deployment (`ecosystem.config.js`) runs a
worker and *no* beat, so with a worker up nothing polled at all. One owner, in
the process that is always running, removes that whole class of gap — and it
also means a timed poll and a beat poll can never run side by side over the
same messages.

Each tick is exactly what one press of Sync used to do (`run_one_cycle`):

1. drain every configured mailbox, batch after batch, until it is empty
   (`app.tasks.jobs.drain_mailbox`);
2. collect Aadhaar / passport jobs the batch could not wait out;
3. send any auto-reply that is still owed.

It takes the same cross-server claim as the manual endpoints
(`claim_inline_poll`), so two API processes — or a tick landing on a manual
`POST /ingest/poll` — never run two cycles over the same mail.
"""
from __future__ import annotations

import asyncio

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: How soon after startup the first cycle runs. Not zero, so the port is open
#: and the WebSocket relay is up before the first candidates are pushed; not a
#: full interval, so a restart does not leave mail waiting a minute for nothing.
FIRST_TICK_DELAY_SECONDS = 10


def run_one_cycle(query: str | None = None) -> dict | None:
    """One full poll of every mailbox, or None if a cycle is already running."""
    from app.tasks.locks import claim_inline_poll

    claim = claim_inline_poll()
    if claim is None:
        log.debug("Skipping the scheduled poll: a cycle is already in progress")
        return None
    try:
        from app.ingestion.runner import IngestionRunner
        from app.tasks.jobs import drain_mailbox

        summary = drain_mailbox(IngestionRunner(), query=query)

        # The two sweeps the Sync button used to run after its batch. Without
        # beat these are the only things that collect a passport extraction
        # which outlived the batch, or send a reply a restart swallowed.
        try:
            from app.api.routes import _collect_pending_identity_jobs

            _collect_pending_identity_jobs()
        except Exception as exc:  # noqa: BLE001 — a sweep is not the cycle
            log.warning("Identity sweep after the scheduled poll failed: %s", exc)
        try:
            from app.ingestion.pipeline import flush_pending_auto_replies

            flush_pending_auto_replies()
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto-reply sweep after the scheduled poll failed: %s", exc)
        return summary
    finally:
        claim.release()


async def run_forever() -> None:
    """The timer. Cancelled at shutdown; never lets one bad cycle end the loop."""
    interval = max(10, int(settings.mail_poll_interval_seconds))
    log.info("Automatic mail poller started: every %ds, all configured mailboxes", interval)

    delay = min(FIRST_TICK_DELAY_SECONDS, interval)
    while True:
        try:
            await asyncio.sleep(delay)
            delay = interval
            # On a worker thread: a cycle is IMAP, OCR and the LLM, and the
            # event loop has to stay free to serve requests and push the
            # WebSocket events the cycle produces.
            summary = await asyncio.to_thread(run_one_cycle)
            if summary and (summary.get("fetched") or summary.get("errors")):
                log.info(
                    "Scheduled poll: fetched=%d processed=%d candidates=%d errors=%d backlog=%d",
                    summary.get("fetched", 0), summary.get("processed", 0),
                    summary.get("ingested_candidates", 0), summary.get("errors", 0),
                    summary.get("backlog", 0),
                )
        except asyncio.CancelledError:
            log.info("Automatic mail poller stopped")
            raise
        except Exception as exc:  # noqa: BLE001 — a failed cycle is not the end of polling
            log.warning("Scheduled poll failed (%s); trying again next tick", exc)
