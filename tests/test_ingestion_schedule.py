"""Periodic mailbox ingestion must be wired into Celery beat."""

from app.config import settings
from app.tasks.celery_app import celery_app

TASK_NAME = "app.tasks.jobs.poll_gmail"


def test_mailbox_poll_is_registered_as_a_task():
    import app.tasks.jobs  # noqa: F401

    assert TASK_NAME in celery_app.tasks


def test_mailbox_poll_is_on_the_beat_schedule():
    entry = celery_app.conf.beat_schedule.get("poll-gmail")
    assert entry, "automatic Gmail polling is not scheduled"
    assert entry["task"] == TASK_NAME
    assert entry["schedule"] == float(settings.gmail_poll_interval_seconds)


def test_mailbox_poll_interval_is_positive():
    assert settings.gmail_poll_interval_seconds > 0
