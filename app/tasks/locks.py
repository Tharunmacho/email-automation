"""Redis-backed distributed locks, used to keep ingestion work from overlapping.

Two of them, at two grains:

* ``POLL_LOCK`` — one poller at a time. Held for a whole batch by
  ``run_poll_cycle``; held only for the Gmail search and the dispatch by the
  fan-out ``poll_gmail``.
* ``claim_message`` — one worker per email. This is the one that matters once
  work is fanned out, because the poll lock is released long before the
  messages it queued have been processed.

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
# One lock per email, so two workers never process the same message. Under the
# fan-out poller the poll lock covers only the dispatch, which means it is no
# longer what keeps the work from overlapping — this is.
MESSAGE_LOCK_PREFIX = "gmail-msg"

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
        _release(client, key, token, name, ttl_seconds)


def _release(client: "redis.Redis", key: str, token: str, name: str, ttl_seconds: int) -> None:
    try:
        client.eval(_RELEASE_SCRIPT, 1, key, token)
    except redis.RedisError:
        # Redis went away mid-cycle. The lock expires on its own, so the worst
        # case is a delayed next poll, not a stuck one.
        log.warning("Could not release lock %s; it expires within %ss", name, ttl_seconds)


@contextmanager
def claim_message(message_id: str, ttl_seconds: int | None = None) -> Iterator[bool]:
    """Reserve one email for the duration of the block.

    Yields True when the claim is ours and False when another worker already
    holds it — a claim being refused is the normal way a re-dispatched or
    re-fetched message is dropped, not an error, so it is reported rather than
    raised.

    Redis being unreachable yields True. A lock service that is down must not
    stop resumes being ingested: the ledger and the resume-hash unique index
    still keep the duplicate *record* out, so the cost of proceeding is repeated
    extraction, while the cost of refusing is an inbox that never drains.
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.message_lock_ttl_seconds
    name = f"{MESSAGE_LOCK_PREFIX}:{message_id}"
    key = f"lock:{name}"
    token = uuid.uuid4().hex

    try:
        client = get_redis()
        acquired = bool(client.set(key, token, nx=True, ex=ttl))
    except redis.RedisError as err:
        log.warning("Could not claim message %s (%s); processing it anyway", message_id, err)
        yield True
        return

    if not acquired:
        log.info("Message %s is already being processed by another worker", message_id)
        yield False
        return

    try:
        yield True
    finally:
        _release(client, key, token, name, ttl)
