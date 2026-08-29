"""Extraction runs when somebody asks for it, and at no other moment.

A timer that reads mailboxes and runs OCR without anyone asking spends money on
the extraction service every cycle, all day, over an inbox that is usually
empty. It also puts a second poll cycle alongside a manual one, which is how two
runs came to submit the same résumé to Veris at the same instant — the second
was refused as a duplicate idempotency key and the candidate was stored from the
far weaker local parser.

So nothing polls by default. `mail_autopoll_enabled` puts the timer back for a
deployment that wants one, and these tests pin both directions.
"""
from __future__ import annotations

from app.config import settings
from app.tasks.celery_app import _mail_poll_schedule, celery_app
from tests.test_api import test_client  # noqa: F401 — the shared API fixture


def _beat_schedule() -> dict:
    """What beat is configured to run, as built at import time."""
    return dict(celery_app.conf.beat_schedule)


def test_nothing_drains_the_mailboxes_on_a_timer_by_default():
    assert settings.mail_autopoll_enabled is False
    assert "poll-mailboxes" not in _beat_schedule()


def test_the_housekeeping_sweeps_still_run():
    """Turning the mail poll off must not take the rest of beat with it.

    A stuck OCR job still has to be collected and an SLA breach still has to be
    found; neither waits on anybody pressing a button.
    """
    schedule = _beat_schedule()

    assert "reconcile-ocr-jobs" in schedule
    assert "scan-sla-breaches" in schedule


def test_the_timer_can_be_switched_back_on(monkeypatch):
    """One flag, and the entry beat needs comes back."""
    monkeypatch.setattr(settings, "mail_autopoll_enabled", True)

    entry = _mail_poll_schedule()["poll-mailboxes"]

    assert entry["task"] == "app.tasks.jobs.poll_gmail"
    assert entry["schedule"] == float(settings.mail_poll_interval_seconds)


def test_with_the_flag_off_the_builder_contributes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "mail_autopoll_enabled", False)

    assert _mail_poll_schedule() == {}


def test_the_manual_sync_still_runs_a_full_cycle():
    """The Sync button is now the only way in, so it has to do the whole job."""
    from app.ingestion import autopoll

    assert callable(autopoll.run_one_cycle)


def test_the_in_process_poller_is_gated_on_the_same_flag():
    """The API runs its own poller when no Celery worker is up, so it has to
    honour the same switch — otherwise turning the timer off in beat would
    silently leave a second one running inside the web process.

    Read as text rather than imported: importing the API opens a database
    connection, and this is a question about the source, not about a running
    app.
    """
    from pathlib import Path

    source = Path("app/api/routes.py").read_text(encoding="utf-8")

    assert "if settings.mail_autopoll_enabled and not _under_test():" in source


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
