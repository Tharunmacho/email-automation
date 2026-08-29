"""`REDIS_URL` and `CELERY_BROKER_URL` address the same Redis.

They are separate settings carrying the same localhost default, and a real
deployment set the broker to its managed Redis while leaving `REDIS_URL` as
`.env.example` ships it. The result: a Celery worker that connected fine, and a
lock that did not —

    Redis lock fallback: Error 111 connecting to localhost:6379

Inside a container `localhost` is that container. The poll lock then degrades to
the in-process fallback, which cannot see the other replicas, so two API
containers both drain the same mailbox and both pay for the same extraction.
Nothing surfaces that except the bill.

So a `REDIS_URL` that is absent, or still holding the shipped default while the
broker points somewhere real, adopts the broker. An explicitly different value
is left alone, because keeping them apart is a legitimate if unusual choice.
"""
from __future__ import annotations

import pytest

from app.config import Settings, _LOCAL_REDIS

REMOTE = "redis://default:secret@redis-host:6379/0"


def build(**overrides) -> Settings:
    """Settings with the developer's own .env kept out of it."""
    return Settings(_env_file=None, **overrides)


def test_the_shipped_default_next_to_a_real_broker_adopts_the_broker():
    """The deployment that failed."""
    settings = build(celery_broker_url=REMOTE, redis_url=_LOCAL_REDIS)

    assert settings.redis_url == REMOTE


def test_an_absent_redis_url_adopts_the_broker():
    settings = build(celery_broker_url=REMOTE)

    assert settings.redis_url == REMOTE


def test_it_says_so_rather_than_doing_it_silently(caplog):
    """A setting that quietly means something other than what it says is worse
    than the bug. Overriding an explicit value has to be visible."""
    with caplog.at_level("WARNING"):
        build(celery_broker_url=REMOTE, redis_url=_LOCAL_REDIS)

    assert any("REDIS_URL" in r.message for r in caplog.records)


def test_a_deliberately_different_redis_is_left_alone():
    """Two separate Redis servers is unusual, not wrong."""
    apart = "redis://locks-only:6379/3"
    settings = build(celery_broker_url=REMOTE, redis_url=apart)

    assert settings.redis_url == apart


def test_local_development_is_untouched():
    """Nothing set: both stay on localhost, which is correct on a laptop."""
    assert build().redis_url == _LOCAL_REDIS


def test_a_localhost_broker_does_not_trigger_it():
    """Running Celery locally must not produce a spurious warning or change."""
    settings = build(celery_broker_url=_LOCAL_REDIS, redis_url=_LOCAL_REDIS)

    assert settings.redis_url == _LOCAL_REDIS


def test_an_empty_broker_is_not_adopted():
    """A blank value is not a destination."""
    settings = build(celery_broker_url="", redis_url=_LOCAL_REDIS)

    assert settings.redis_url == _LOCAL_REDIS


def test_the_locks_read_the_resolved_value(monkeypatch):
    """The setting only matters because `locks.get_redis` reads it — if that
    ever stops being true, this rescue is decoration."""
    import inspect

    from app.tasks import locks

    assert "redis_url" in inspect.getsource(locks.get_redis)
