"""Celery application — background-job seam.

Run a worker:
    celery -A app.tasks.celery_app worker --loglevel=INFO --pool=solo
Run the periodic poller (beat):
    celery -A app.tasks.celery_app beat --loglevel=INFO

The pipeline runs fine synchronously via the CLI; Celery is here so that, as
volume grows, per-message processing (OCR + LLM) can be fanned out across workers
without re-architecting anything.
"""
from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "resume_ingest",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.jobs"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
)

# Poll Gmail every 2 minutes by default.
celery_app.conf.beat_schedule = {
    "poll-gmail": {
        "task": "app.tasks.jobs.poll_gmail",
        "schedule": 120.0,
    }
}
