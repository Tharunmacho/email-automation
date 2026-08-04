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
    # The API probes for a live worker inside a request, so a down broker has
    # to fail in seconds rather than hang on the default connect timeout.
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    result_backend_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
)

# Poll Gmail every 2 minutes by default. `run_poll_cycle` rather than the
# fan-out `poll_gmail`: it runs the same code path the CLI and API use, so
# there is one batch behaviour to reason about. Both hold the same lock, so a
# tick landing on a manual sync is skipped rather than doubling the work.
celery_app.conf.beat_schedule = {
    "poll-gmail": {
        "task": "app.tasks.jobs.run_poll_cycle",
        "schedule": 120.0,
    }
}
