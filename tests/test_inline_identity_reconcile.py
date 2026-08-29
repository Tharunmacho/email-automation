"""A passport whose extraction outlives the batch still reaches the record.

An identity job that takes longer than `identity_job_wait_seconds` is left
"pending" with its job id kept, and the beat reconciler collects it on a later
sweep. That is sound — except beat runs on a Celery worker, and the inline poll
exists precisely because there is no worker. Nothing swept, so the extraction
succeeded at the service and was never written down.

From a real run: an eighteen-page passport submitted at 12:45:19, given up on at
12:46:02 as "still running", and then collected by nothing at all.
"""
from __future__ import annotations

import pytest

from app.api import routes
from app.config import settings


@pytest.fixture(autouse=True)
def quick_budget(monkeypatch):
    """The real budget is five minutes; the behaviour is identical at one second."""
    monkeypatch.setattr(settings, "inline_reconcile_budget_seconds", 1.0)
    monkeypatch.setattr(settings, "inline_reconcile_interval_seconds", 0.01)


def _sweeps(monkeypatch, reports):
    """Install a reconciler that returns `reports` in order, then repeats the last."""
    calls = []

    def fake_reconcile(limit=None):
        calls.append(limit)
        return reports[min(len(calls) - 1, len(reports) - 1)]

    monkeypatch.setattr("app.tasks.reconciler.reconcile_once", fake_reconcile)
    return calls


def test_it_sweeps_until_nothing_is_still_running(monkeypatch):
    calls = _sweeps(monkeypatch, [
        {"scanned": 1, "still_running": 1, "completed": 0},
        {"scanned": 1, "still_running": 0, "completed": 1},
    ])

    routes._collect_pending_identity_jobs()

    assert len(calls) == 2, "it stopped before the job finished, or kept going after"


def test_it_stops_as_soon_as_the_queue_is_clear(monkeypatch):
    """The common case — nothing pending — must not cost the batch a wait.

    Swept first and waited after, so a cycle with no pending job pays one cheap
    query and no delay at all.
    """
    import time as _time

    calls = _sweeps(monkeypatch, [{"scanned": 0, "still_running": 0}])
    monkeypatch.setattr(settings, "inline_reconcile_interval_seconds", 30.0)

    started = _time.monotonic()
    routes._collect_pending_identity_jobs()
    elapsed = _time.monotonic() - started

    assert len(calls) == 1
    assert elapsed < 1.0, f"an idle sweep cost {elapsed:.1f}s"


def test_a_job_that_never_finishes_gives_up_on_its_budget(monkeypatch):
    """It must not sweep forever: the row keeps its job id and a later sweep,
    or an operator, deals with it."""
    calls = _sweeps(monkeypatch, [{"scanned": 1, "still_running": 1}])

    routes._collect_pending_identity_jobs()

    assert calls, "it never swept at all"
    # Bounded by the budget rather than by the number of passes.
    assert len(calls) < 50


def test_a_failing_sweep_does_not_take_the_batch_with_it(monkeypatch):
    """This runs after a completed cycle whose candidates are already stored."""
    def boom(limit=None):
        raise RuntimeError("mongo blinked")

    monkeypatch.setattr("app.tasks.reconciler.reconcile_once", boom)

    routes._collect_pending_identity_jobs()  # must not raise


def test_the_inline_cycle_sweeps_before_reporting_success(monkeypatch):
    """Order matters: the summary must not say "done" while a passport
    extraction is still uncollected."""
    order = []

    class Runner:
        def run_once(self, query=None):
            order.append("batch")
            return "summary"

    monkeypatch.setattr("app.ingestion.runner.IngestionRunner", Runner)
    monkeypatch.setattr("app.tasks.jobs.summary_to_dict", lambda _s: {"processed": 1})
    monkeypatch.setattr(routes, "_collect_pending_identity_jobs",
                        lambda: order.append("reconcile"))

    claim = routes._start_inline_poll(None)

    import time
    for _ in range(100):
        status = routes._inline_task_get(claim["task_id"])
        if status and status.get("ready"):
            break
        time.sleep(0.05)

    assert order == ["batch", "reconcile"], f"ran in the wrong order: {order}"
    assert status["state"] == "SUCCESS"
