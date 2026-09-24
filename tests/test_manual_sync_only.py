"""The mailboxes are drained 24/7 by one timer, in the API process.

The Sync button is gone, so the timer is the only way mail gets read. It is on
by default and it is the *only* scheduler: beat does not poll mail. Two
schedulers over the same mailboxes is how two runs once submitted the same
résumé to Veris at the same instant, and deferring to beat whenever a worker
was online left the pm2 deployment — a worker and no beat — polling nothing.

The manual endpoints still exist (and share the timer's claim), so the tests
for what they return are kept below.
"""
from __future__ import annotations

import asyncio

from app.config import settings
from app.tasks.celery_app import _mail_poll_schedule, celery_app
from tests.test_api import test_client  # noqa: F401 — the shared API fixture


def _beat_schedule() -> dict:
    """What beat is configured to run, as built at import time."""
    return dict(celery_app.conf.beat_schedule)


def test_the_mailboxes_are_polled_automatically_by_default():
    assert settings.mail_autopoll_enabled is True


def test_beat_never_polls_the_mailboxes(monkeypatch):
    """The API timer owns the poll; a beat entry would be a second scheduler."""
    assert "poll-mailboxes" not in _beat_schedule()
    monkeypatch.setattr(settings, "mail_autopoll_enabled", True)
    assert _mail_poll_schedule() == {}


def test_the_housekeeping_sweeps_still_run():
    """A stuck OCR job still has to be collected and an SLA breach found."""
    schedule = _beat_schedule()

    assert "reconcile-ocr-jobs" in schedule
    assert "scan-sla-breaches" in schedule


def test_the_in_process_poller_is_gated_on_the_flag():
    """Read as text: importing the API opens a database connection."""
    from pathlib import Path

    source = Path("app/api/routes.py").read_text(encoding="utf-8")

    assert "if settings.mail_autopoll_enabled and not _under_test():" in source


# --------------------------------------------------------------------------- #
#  What one timed cycle does
# --------------------------------------------------------------------------- #
def _install_cycle(monkeypatch, batches, order):
    class Runner:
        calls = 0

        def run_once(self, query=None):
            spec = batches[min(Runner.calls, len(batches) - 1)]
            Runner.calls += 1
            order.append("batch")
            return spec

    monkeypatch.setattr("app.ingestion.runner.IngestionRunner", Runner)
    monkeypatch.setattr("app.tasks.jobs.summary_to_dict", lambda s: dict(s))
    monkeypatch.setattr("app.api.routes._collect_pending_identity_jobs",
                        lambda: order.append("identity"))
    monkeypatch.setattr("app.ingestion.pipeline.flush_pending_auto_replies",
                        lambda: order.append("replies"))
    return Runner


def test_a_timed_cycle_drains_the_mailbox_then_runs_both_sweeps(monkeypatch):
    """Exactly what one press of Sync used to do — nothing less."""
    from app.ingestion import autopoll

    order: list[str] = []
    _install_cycle(monkeypatch, [
        {"fetched": 25, "processed": 25, "backlog": 25},
        {"fetched": 25, "processed": 25, "backlog": 0},
    ], order)

    summary = autopoll.run_one_cycle()

    assert order == ["batch", "batch", "identity", "replies"], order
    assert summary["processed"] == 50


def test_a_timed_cycle_is_declined_while_another_is_running(monkeypatch):
    """Shares the manual endpoints' claim, so two cycles never overlap."""
    from app.ingestion import autopoll

    monkeypatch.setattr("app.tasks.locks.claim_inline_poll", lambda *a, **k: None)
    order: list[str] = []
    _install_cycle(monkeypatch, [{"fetched": 1, "processed": 1, "backlog": 0}], order)

    assert autopoll.run_one_cycle() is None
    assert order == []


def test_the_timer_polls_even_when_a_worker_is_online(monkeypatch):
    """pm2 runs a worker and no beat: standing down for the worker meant
    nothing polled at all."""
    from app.ingestion import autopoll

    monkeypatch.setattr("app.tasks.health.workers_online", lambda: True)
    monkeypatch.setattr(autopoll, "FIRST_TICK_DELAY_SECONDS", 0)
    ran = []

    def fake_cycle():
        ran.append(1)
        raise asyncio.CancelledError  # one tick is enough

    monkeypatch.setattr(autopoll, "run_one_cycle", fake_cycle)

    try:
        asyncio.run(autopoll.run_forever())
    except asyncio.CancelledError:
        pass

    assert ran == [1], "the timer skipped its tick because a worker was online"


# --------------------------------------------------------------------------- #
#  What the Sync button gets back
# --------------------------------------------------------------------------- #
def test_a_sync_with_no_worker_does_not_hold_the_request_open(test_client, monkeypatch):
    """With nothing to queue on, the cycle runs on a thread in this process and
    the reply is a task id to follow — the same shape a worker would give.

    It used to run the whole batch inside the request: IMAP, the attachment
    download, OCR of every page, two Veris round trips and the LLM, with the
    browser blocked on one request for all of it — close to three minutes on a
    thirty-page bundle. The work still costs what it costs; it must not cost it
    in front of the user.

    The one shape that must never come back is a queued-looking reply with no
    task id. That is what once had the client asking after
    `/ingest/tasks/undefined` for ten minutes while the batch it was waiting for
    had already finished.
    """
    import threading
    import time

    monkeypatch.setattr("app.tasks.health.workers_online", lambda: False)

    started, release = threading.Event(), threading.Event()

    class BlockingRunner:
        def run_once(self, query=None):
            started.set()
            release.wait(10)
            return "summary"

    monkeypatch.setattr("app.ingestion.runner.IngestionRunner", BlockingRunner)
    monkeypatch.setattr(
        "app.tasks.jobs.summary_to_dict",
        lambda _summary: {"fetched": 2, "processed": 1, "skipped": 1, "suppressed": 0,
                          "errors": 0, "ingested_candidates": 1, "results": []},
    )

    try:
        body = test_client.post("/ingest/poll/async").json()

        # The batch is still inside `run_once` — so the POST plainly did not
        # wait for it, which is the whole point of the change.
        assert started.wait(5), "the cycle never started"
        assert body["task_id"], "the client needs something to ask after"
        assert body["state"] == "PENDING"
        assert "result" not in body, "nothing has finished yet"

        pending = test_client.get(f"/ingest/tasks/{body['task_id']}").json()
        assert pending["ready"] is False

        release.set()
        for _ in range(200):
            status = test_client.get(f"/ingest/tasks/{body['task_id']}").json()
            if status["ready"]:
                break
            time.sleep(0.05)

        assert status["state"] == "SUCCESS"
        assert status["result"]["ingested_candidates"] == 1
    finally:
        release.set()


def test_a_second_sync_cannot_start_while_one_is_running(test_client, monkeypatch):
    """Overlapping inline cycles would run the same messages twice.

    Reported as a finished cycle that did nothing rather than as a failure: the
    client answers a FAILURE by running the batch inline itself, which is the
    one thing that must not happen while a batch is already in flight.
    """
    import threading

    monkeypatch.setattr("app.tasks.health.workers_online", lambda: False)

    started, release = threading.Event(), threading.Event()

    class BlockingRunner:
        def run_once(self, query=None):
            started.set()
            release.wait(10)
            return "summary"

    monkeypatch.setattr("app.ingestion.runner.IngestionRunner", BlockingRunner)
    monkeypatch.setattr("app.tasks.jobs.summary_to_dict", lambda _s: {})

    try:
        test_client.post("/ingest/poll/async")
        assert started.wait(5)

        second = test_client.post("/ingest/poll/async").json()
        assert second["state"] == "SUCCESS", "a decline must not read as a failure"
        assert "already running" in second["result"]["skipped_reason"]
    finally:
        release.set()


def test_a_sync_with_a_worker_hands_back_a_task_to_follow(test_client, monkeypatch):
    """The other shape, unchanged: something to poll for."""
    from app.api import routes

    monkeypatch.setattr("app.tasks.health.workers_online", lambda: True)

    class _Queued:
        id = "task-123"

    monkeypatch.setattr("app.tasks.jobs.run_poll_cycle.delay", lambda _q: _Queued())

    body = test_client.post("/ingest/poll/async").json()

    assert body["task_id"] == "task-123"
    assert body["state"] == "PENDING"
    assert "result" not in body, "nothing has run yet, so there is nothing to report"
    assert routes  # the module under test
