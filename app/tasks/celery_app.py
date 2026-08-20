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
    include=["app.tasks.jobs", "app.tasks.reconciler"],
)

celery_app.conf.update(
    imports=["app.tasks.jobs", "app.tasks.reconciler"],
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
    # The reconciler sweep. Every OCR job the pipeline could not wait out is
    # sitting on an ingestion row with its job id; this is what goes back for
    # the answer. It is single-flighted on a Redis lock, so a tick landing on
    # top of a slow sweep is a no-op rather than a double submission.
    beat_schedule={
        "reconcile-ocr-jobs": {
            "task": "app.tasks.reconciler.reconcile_ocr_jobs",
            "schedule": float(settings.reconciler_interval_seconds),
        },
    },
)


