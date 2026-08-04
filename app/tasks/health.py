"""Is there a live worker to hand ingestion off to?

The API asks this before every queued poll, so it has to be cheap and it has to
fail fast. A naive `celery_app.control.ping()` against a down broker takes ~5s,
because kombu retries the connection before giving up — paid on every sync click
on any machine that never started Redis, which is the common case in local dev.

So: check Redis with our own short-timeout client first (a refused connection on
localhost is instant), and only spend the ping timeout once the broker is known
to be reachable. Then memoise, because neither answer changes between two clicks
a second apart.
"""
from __future__ import annotations

import time

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Asymmetric on purpose. A "yes" is cheap to re-check and worth re-checking
# often, since a worker that died should be noticed quickly. A "no" costs the
# full connect timeout and is a stable state — nobody starts a worker between
# two clicks — so it is held for far longer.
_TTL_ONLINE_SECONDS = 10.0
_TTL_OFFLINE_SECONDS = 60.0

_cached_answer: bool | None = None
_cached_at: float = 0.0


def _probe() -> bool:
    try:
        import redis

        from app.tasks.locks import get_redis

        try:
            get_redis().ping()
        except redis.RedisError:
            # No broker means no worker, and there is nothing to ping over.
            return False

        from app.tasks.celery_app import celery_app

        return bool(celery_app.control.ping(timeout=1.0))
    except Exception:  # noqa: BLE001
        # Celery missing, misconfigured, or anything else — all of it means the
        # same thing to the caller: run the poll inline instead.
        return False


def workers_online(force: bool = False) -> bool:
    """True when at least one Celery worker answers. Cached for a few seconds."""
    global _cached_answer, _cached_at

    now = time.monotonic()
    if not force and _cached_answer is not None:
        ttl = _TTL_ONLINE_SECONDS if _cached_answer else _TTL_OFFLINE_SECONDS
        if now - _cached_at < ttl:
            return _cached_answer

    _cached_answer = _probe()
    _cached_at = now
    return _cached_answer


def reset_cache() -> None:
    """Drop the memo. For tests, and for right after a dispatch fails."""
    global _cached_answer, _cached_at
    _cached_answer = None
    _cached_at = 0.0
