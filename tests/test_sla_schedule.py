"""The SLA sweep has to be on the beat schedule, not merely importable.

This is a regression test for a real gap: `scan_sla_breaches` existed, was
documented as "the Celery task wrapper", and was neither decorated as a task nor
listed in `beat_schedule`. The only way a breach was ever found was an admin
pressing Scan — which is the one case an unattended-work alert is not needed for,
because somebody is already looking.

Nothing here runs a sweep. It pins the wiring, because the wiring is what was
missing and nothing else would have noticed.
"""
from app.config import settings
from app.tasks.celery_app import celery_app

TASK_NAME = "app.tasks.sla_checker.scan_sla_breaches"


def test_the_sweep_is_registered_as_a_task():
    # Importing the module is what registers it, and the worker only imports it
    # because `celery_app` lists it — hence both halves of this file.
    import app.tasks.sla_checker  # noqa: F401

    assert TASK_NAME in celery_app.tasks


def test_the_worker_is_told_to_import_the_module():
    """A registered task in a module nobody imports is not registered anywhere
    it matters: the beat entry would raise `NotRegistered` on the worker."""
    for key in ("include", "imports"):
        modules = list(celery_app.conf.get(key) or [])
        assert "app.tasks.sla_checker" in modules, f"missing from {key}"


def test_the_sweep_is_on_the_beat_schedule():
    entry = celery_app.conf.beat_schedule.get("scan-sla-breaches")
    assert entry, "the SLA sweep is not scheduled"
    assert entry["task"] == TASK_NAME
    assert entry["schedule"] == float(settings.sla_scan_interval_seconds)


def test_the_window_is_two_days_and_the_sweep_is_well_inside_it():
    """A sweep interval near the window would report a breach up to a whole
    interval late, and the alert's "51 hours" would be off by that much."""
    assert settings.sla_threshold_hours == 48
    assert settings.sla_scan_interval_seconds < settings.sla_threshold_hours * 3600 / 10
