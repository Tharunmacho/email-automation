"""One inline poll cycle across every server, not just inside one process.

The guard used to be a bare `threading.Lock`. It did its job — two overlapping
requests in one process could not both run a batch — but it could not see other
processes at all. Two API containers behind a load balancer therefore both
drained the same mailbox, both downloaded the same attachments and both paid
for the same OCR and LLM extraction. The ledger and the resume-hash unique index
keep the duplicate *record* out, but only after the money has been spent.

Redis being unreachable has to keep working, and has to keep working *well*:
a deployment with no Redis is almost always a single server, and the in-process
lock covers a single server completely.
"""
from __future__ import annotations

import threading

import pytest

import redis

from app.tasks import locks


class FakeRedis:
    """Just enough Redis for SET NX EX and the release script."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def eval(self, _script, _numkeys, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


@pytest.fixture
def shared_redis(monkeypatch):
    """One Redis, handed to every caller — i.e. several servers, one lock."""
    fake = FakeRedis()
    monkeypatch.setattr(locks, "get_redis", lambda: fake)
    # The in-process lock is module state and leaks between tests otherwise.
    monkeypatch.setattr(locks, "_local_inline_lock", threading.Lock())
    return fake


@pytest.fixture
def redis_down(monkeypatch):
    def boom():
        raise redis.RedisError("connection refused")

    monkeypatch.setattr(locks, "get_redis", boom)
    monkeypatch.setattr(locks, "_local_inline_lock", threading.Lock())


# --------------------------------------------------------------------------- #
#  With Redis: the lock spans servers
# --------------------------------------------------------------------------- #
def test_a_second_server_cannot_start_a_cycle(shared_redis):
    """The case a threading.Lock could not cover."""
    first = locks.claim_inline_poll()

    assert first is not None
    assert first.distributed is True, "this should be the Redis-backed claim"
    assert locks.claim_inline_poll() is None, (
        "a second server started a cycle over the same mailbox"
    )


def test_the_claim_is_handed_back(shared_redis):
    first = locks.claim_inline_poll()
    first.release()

    second = locks.claim_inline_poll()
    assert second is not None, "the lock was never released"
    second.release()
    assert not shared_redis.store, "the key outlived the cycle"


def test_releasing_twice_is_harmless(shared_redis):
    """The release runs in a thread's `finally`, which can be reached twice on
    an interpreter shutdown path. It must not then free a later cycle's lock."""
    claim = locks.claim_inline_poll()
    claim.release()

    other = locks.claim_inline_poll()
    claim.release()  # the stale handle, again

    assert locks.claim_inline_poll() is None, (
        "a stale release freed a lock a live cycle was holding"
    )
    other.release()


def test_a_release_that_fails_does_not_take_the_batch_with_it(shared_redis, monkeypatch):
    """Releasing runs after a completed batch. A Redis blip there must not raise
    into the caller and lose a summary that was already paid for."""
    claim = locks.claim_inline_poll()

    def boom(*_a, **_k):
        raise redis.RedisError("gone")

    monkeypatch.setattr(shared_redis, "eval", boom)
    claim.release()  # must not raise


# --------------------------------------------------------------------------- #
#  Without Redis: still single-flight on this server
# --------------------------------------------------------------------------- #
def test_no_redis_falls_back_rather_than_refusing(redis_down):
    """A lock service being down must not stop the mailbox draining."""
    claim = locks.claim_inline_poll()

    assert claim is not None, "a missing Redis blocked ingestion entirely"
    assert claim.distributed is False


def test_the_fallback_is_still_single_flight(redis_down):
    first = locks.claim_inline_poll()

    assert locks.claim_inline_poll() is None, (
        "two cycles ran at once on the same server"
    )

    first.release()
    again = locks.claim_inline_poll()
    assert again is not None
    again.release()


def test_the_fallback_survives_many_threads(redis_down):
    """Whatever the race, exactly one caller may hold it."""
    granted = []
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        claim = locks.claim_inline_poll()
        if claim is not None:
            granted.append(claim)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 1, f"{len(granted)} threads all held the lock at once"
    granted[0].release()
