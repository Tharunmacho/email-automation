"""Keep every open job order's AI shortlist current without anybody asking.

    sweep          every open order: plan, and run only where something is pending
    catch_up       one order, now — the sweep's step, and the panel opening
    nudge          a saved order, brought up to date on a background thread
    run_forever    the timer, started with the API (`routes._startup`)

A recruiter should open a job order and find it already analysed. So the API
process walks the open orders on a timer (`jev_auto_match_interval_seconds`)
and, for each, asks `runner.plan` what is not current: a candidate added since
the last run, a CV that changed, an order that was edited, a result past its
TTL. Only that is sent to Jev. An order where nothing changed costs two small
reads and creates no run at all, which is what makes it affordable to look
often.

Orders are taken newest first and one at a time, so the first backfill after
this ships finishes whole orders rather than inching every order forward
together. Saving an order does not wait for the timer: `nudge` brings that one
order up to date straight away. Opening its panel does the same
(`POST /job-orders/{id}/matching/preload`), for the order a recruiter is
looking at right now.

Same pipeline, same cache, same lock as a button press — there is no second
way of scoring. An automatic run differs in one thing only: a candidate Jev
just failed on is held back for a while instead of being re-sent every sweep
(`runner.AUTO_RETRY_FAILED_AFTER`); "Retry remaining" still sends them.

Closed and filled orders are never matched automatically. Off with
`JEV_AUTO_MATCH=false`; never on inside the test suite.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from pymongo import DESCENDING

from app.config import settings
from app.logging_config import get_logger
from app.matching import jev, prefilter, runner, scoring, store
from app.matching.spec import normalize_job_order

log = get_logger(__name__)

#: Who an automatic run is recorded as having been started by.
AUTO_USER = {"id": None, "name": "Automatic matching"}

#: Not zero, so the indexes and the port are up first; not a full interval,
#: so a restart does not leave new candidates waiting five minutes for nothing.
FIRST_SWEEP_DELAY_SECONDS = 30


def enabled() -> bool:
    """Whether matching should start on its own in this process."""
    from app.api.routes import _under_test

    return bool(settings.jev_auto_match) and jev.is_configured() and not _under_test()


def active_orders() -> List[Dict[str, Any]]:
    """Open and in-progress orders, newest first."""
    from app.db.mongo import get_db

    orders = get_db()["job_orders"].find({}, {"_id": 0}).sort("_id", DESCENDING)
    return [order for order in orders if order.get("id") and normalize_job_order(order).is_active]


def catch_up(
    order: Dict[str, Any],
    *,
    user: Optional[Dict[str, Any]] = None,
    trigger: str = runner.TRIGGER_AUTO,
    rows: Optional[List[Dict[str, Any]]] = None,
    wait: bool = False,
) -> Dict[str, Any]:
    """Start a run for this order if anything in its pool is not current.

    Returns `{"pending": n, "run": run-or-None}`. Nothing pending, or a closed
    order, is `None` — no run document is written for a run with nothing to
    do. Raises what `runner.start_run` raises (`RunConflict` when a run is
    already active on the order).
    """
    from app.api import routes

    spec = normalize_job_order(order)
    if not spec.is_active:
        return {"pending": 0, "run": None}
    if rows is None:
        rows = prefilter.retrieve(routes.repo(), spec)
    work = runner.plan(spec, scoring.config_for(order), rows, auto=True)
    if not work.pending:
        return {"pending": 0, "run": None}
    run = runner.start_run(order, user or AUTO_USER, mode=runner.MODE_MATCH,
                           trigger=trigger, wait=wait)
    return {"pending": len(work.pending), "run": run}


def sweep() -> Dict[str, int]:
    """Bring every open order up to date, one order at a time."""
    from app.api import routes

    summary = {"orders": 0, "runs": 0, "busy": 0, "errors": 0}
    if not jev.is_configured():
        return summary
    orders = active_orders()
    summary["orders"] = len(orders)
    if not orders:
        return summary

    # One scan of the pool serves every order's plan. A long first backfill
    # can outlast it, so it is taken again once it is older than a sweep; the
    # run itself always re-reads the pool before it sends anything.
    refresh_after = max(60, int(settings.jev_auto_match_interval_seconds))
    rows = prefilter.retrieve(routes.repo())
    taken = time.monotonic()
    for order in orders:
        if time.monotonic() - taken > refresh_after:
            rows = prefilter.retrieve(routes.repo())
            taken = time.monotonic()
        try:
            outcome = catch_up(order, rows=rows, wait=True)
        except store.RunConflict:
            # Somebody pressed the button, or the order was just saved. That
            # run is doing this work already.
            summary["busy"] += 1
            continue
        except jev.JevNotConfigured:
            break
        except Exception:  # noqa: BLE001 — one order must not stop the sweep
            log.exception("Automatic matching failed for job order %s", order.get("id"))
            summary["errors"] += 1
            continue
        if outcome["run"]:
            summary["runs"] += 1
    return summary


def nudge(order_id: Optional[str]) -> None:
    """A job order was saved: bring it up to date now, off the request thread."""
    if not order_id or not enabled():
        return

    def work() -> None:
        try:
            catch_up(runner.load_order(order_id), wait=True)
        except (store.RunConflict, runner.OrderNotFound):
            pass  # already running, or deleted since; the next sweep settles it
        except Exception:  # noqa: BLE001
            log.exception("Automatic matching after saving job order %s failed", order_id)

    runner._spawn(work)


async def run_forever() -> None:
    """The timer. Cancelled at shutdown; one bad sweep never ends the loop."""
    interval = max(60, int(settings.jev_auto_match_interval_seconds))
    log.info("Automatic candidate matching started: open job orders every %ds", interval)
    delay = min(FIRST_SWEEP_DELAY_SECONDS, interval)
    while True:
        try:
            await asyncio.sleep(delay)
            delay = interval
            if not enabled():
                continue
            # On a worker thread: a sweep is Mongo reads and Jev calls, and the
            # event loop has to keep serving requests meanwhile.
            summary = await asyncio.to_thread(sweep)
            if summary["runs"] or summary["errors"]:
                log.info("Automatic matching: %d open orders, %d brought up to date, %d busy, %d errors",
                         summary["orders"], summary["runs"], summary["busy"], summary["errors"])
        except asyncio.CancelledError:
            log.info("Automatic candidate matching stopped")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Automatic matching sweep failed (%s); trying again next tick", exc)
