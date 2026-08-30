"""Drain the mailboxes on a timer, from inside the API process.

Celery beat is the right home for this and, when a worker is running, it is
where it happens — see the `poll-mailboxes` entry in `app/tasks/celery_app.py`.
But a deployment without a worker had *nothing* polling: mail was only ever
fetched when somebody pressed Sync, which is why a résumé that had plainly been
sent did not appear until the page was reloaded, and then only because the
reload triggered a fetch.

So the API carries a fallback. Every tick it asks whether a worker is online; if
one is, it does nothing at all and lets beat own the schedule. If none is, it
runs one poll cycle itself, on a worker thread so the event loop stays free to
serve requests and push the WebSocket events the poll produces.

It shares `_inline_poll_lock` with the manual Sync endpoint, so a timer tick
landing on top of a sync is skipped rather than running the same messages twice.
"""
from __future__ import annotations

import asyncio
import threading

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: Held for the duration of one cycle. Non-blocking acquisition everywhere: an
#: overlapping tick has nothing useful to add, because the cycle already running
#: is looking at the same mailboxes.
poll_lock = threading.Lock()


def run_one_cycle() -> dict | None:
    """One poll of every configured mailbox, or None if one is already running."""
    if not poll_lock.acquire(blocking=False):
        log.debug("Skipping the scheduled poll: a cycle is already in progress")
        return None
    try:
        from app.ingestion.runner import IngestionRunner
        from app.tasks.jobs import summary_to_dict

        summary = IngestionRunner().run_once()
        return summary_to_dict(summary)
    finally:
        poll_lock.release()


def _worker_is_online() -> bool:
    try:
        from app.tasks.health import workers_online

        return bool(workers_online())
    except Exception:  # noqa: BLE001 — no broker means no worker
        return False


async def run_forever() -> None:
    """The timer. Cancelled at shutdown; never lets one bad cycle end the loop."""
    interval = max(10, int(settings.mail_poll_interval_seconds))
    log.info("In-process mail poller started (every %ds, when no worker is running)", interval)

    while True:
        try:
            await asyncio.sleep(interval)
            if _worker_is_online():
                # Beat owns the schedule while a worker is up. Polling from here
                # as well would double every search and race for the claims.
                continue
            summary = await asyncio.to_thread(run_one_cycle)
            if summary and summary.get("ingested_candidates"):
                log.info(
                    "Scheduled poll ingested %d candidate(s) from %d message(s)",
                    summary["ingested_candidates"], summary.get("fetched", 0),
                )
        except asyncio.CancelledError:
            log.info("In-process mail poller stopped")
            raise
        except Exception as exc:  # noqa: BLE001 — a failed cycle is not the end of polling
            log.warning("Scheduled poll failed (%s); trying again next tick", exc)
