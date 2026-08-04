"""Redis-backed distributed lock, used to keep poll cycles from overlapping.

Why this exists
---------------
Two poll cycles running at once both search Gmail before either has marked
anything read, so they see the same unread messages and both push every
attachment through OCR and the LLM. The ledger and the resume-hash unique index
stop the duplicate *record* from being written, but only after the extraction
has already been paid for. The lock makes the overlap impossible instead of
cleaning up after it.

The lock lives on the same Redis the Celery broker uses — if the worker can run,
this can too.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

import redis

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

POLL_LOCK = "gmail-poll"

# Release has to be atomic: read the token and delete under one evaluation.
# Doing it as a separate GET then DEL lets a holder whose lock already expired
# delete a lock that a different worker has since acquired.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class LockNotAcquired(RuntimeError):
    """Someone else holds the lock. Not an error condition — just back off."""


def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_socket_timeout,
        socket_timeout=settings.redis_socket_timeout,
    )


@contextmanager
def redis_lock(name: str, ttl_seconds: int) -> Iterator[str]:
    """Hold `name` for at most `ttl_seconds`, or raise LockNotAcquired.

    The TTL is a hard deadline rather than an estimate: if the holder is killed
    mid-cycle nothing runs the release, so only expiry can free the lock. Set it
    above the worst-case cycle time — too short and a second cycle starts on top
    of a live one, which is the exact thing being prevented.
    """
    client = get_redis()
    key = f"lock:{name}"
    token = uuid.uuid4().hex

    if not client.set(key, token, nx=True, ex=ttl_seconds):
        raise LockNotAcquired(f"{name} is already held")

    log.debug("Acquired lock %s (ttl=%ss)", name, ttl_seconds)
    try:
        yield token
    finally:
        try:
            client.eval(_RELEASE_SCRIPT, 1, key, token)
        except redis.RedisError:
            # Redis went away mid-cycle. The lock expires on its own, so the
            # worst case is a delayed next poll, not a stuck one.
            log.warning(
                "Could not release lock %s; it expires within %ss", name, ttl_seconds
            )
