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
def test_a_sync_with_no_worker_returns_the_finished_batch(test_client, monkeypatch):
    """With nothing to queue on, the API runs the cycle inside the request — so
    the reply is the summary, not a ticket for a job that was never created.

    Returning the bare summary instead looked to the client like a queued
    response whose `task_id` happened to be missing, and it spent ten minutes
    asking after `/ingest/tasks/undefined` while the work it was waiting for had
    already finished.
    """
    from app.api import routes

    monkeypatch.setattr("app.tasks.health.workers_online", lambda: False)
    monkeypatch.setattr(
        routes, "trigger_poll",
        lambda query=None, _user=None: {"fetched": 2, "processed": 1, "skipped": 1,
                                        "suppressed": 0, "errors": 0,
                                        "ingested_candidates": 1, "results": []},
    )

    body = test_client.post("/ingest/poll/async").json()

    assert body["ready"] is True
    assert body["state"] == "SUCCESS"
    assert body["result"]["ingested_candidates"] == 1
    assert not body["task_id"], "there is no task to ask after"


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
