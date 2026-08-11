"""Celery application — background-job seam.

Run a worker:
    celery -A app.tasks.celery_app worker --loglevel=INFO --concurrency=4
Run the periodic poller (beat):
    celery -A app.tasks.celery_app beat --loglevel=INFO

Concurrency is the point: beat queues one `process_message` task per email, so
four resumes arriving together are extracted in parallel instead of one after
another. `--pool=solo` runs a single task at a time and undoes that — use it
only for debugging. On Windows, where prefork is unsupported, use
`--pool=threads --concurrency=4`; the work is I/O-bound (Gmail, OCR, the LLM),
so threads lose almost nothing.
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
    imports=["app.tasks.jobs"],
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

# Poll Gmail every 2 minutes by default, fanning out: `poll_gmail` searches the
# mailbox and queues one `process_message` per email, then releases the poll
# lock. The scheduled tick therefore holds the lock for about as long as a Gmail
# search takes, rather than for the whole batch — which is what used to make
# every second tick report "another cycle is already running" while the first
# one ground through OCR. The messages it queued stay safe from a concurrent
# poller through their own per-message claims, not through the poll lock.
celery_app.conf.beat_schedule = {
    "poll-gmail": {
        "task": "app.tasks.jobs.poll_gmail",
        "schedule": 120.0,
    }
}
