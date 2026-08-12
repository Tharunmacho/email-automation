"""A batch runs its emails in parallel; the Gmail transport must survive that.

The regression these cover: every pool worker reached through one shared
``httplib2.Http``, which is one TLS socket. Two workers fetching at the same
moment interleaved on it and came back with ``SSL: WRONG_VERSION_NUMBER``, so a
poll of two emails ingested one and logged the other as a failed message.
"""
from __future__ import annotations

import concurrent.futures
import threading

import pytest

from app.gmail import client as gmail_client_module
from app.ingestion import runner as runner_module


@pytest.fixture
def stub_transport(monkeypatch):
    """Count built services and credential loads, without touching Google."""
    built: list[object] = []
    creds_loads = {"n": 0}
    lock = threading.Lock()

    def fake_build(*_args, **_kwargs):
        service = object()
        with lock:
            built.append(service)
        return service

    def fake_credentials():
        with lock:
            creds_loads["n"] += 1
        return object()

    monkeypatch.setattr(gmail_client_module, "build", fake_build)
    monkeypatch.setattr(gmail_client_module, "get_credentials", fake_credentials)
    return built, creds_loads


def test_each_thread_gets_its_own_service(stub_transport):
    built, _ = stub_transport
    client = gmail_client_module.GmailClient()
    workers = 8
    # Hold every worker until all of them have arrived, so the pool really does
    # run them at once — a pool handed instant work otherwise serves all of it
    # on a single thread and the test proves nothing.
    barrier = threading.Barrier(workers)

    def touch(_i):
        barrier.wait(timeout=10)
        return client._service

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        services = list(pool.map(touch, range(workers)))

    # Compare identity, not id(): a freed object's id gets handed to the next.
    assert len({id(s): s for s in services}) == workers
    assert len(built) == workers + 1  # +1: the service built in __init__


def test_a_thread_reuses_its_own_service(stub_transport):
    _, _ = stub_transport
    client = gmail_client_module.GmailClient()
    assert client._service is client._service


def test_credentials_are_loaded_once_per_thread_not_per_request(stub_transport):
    _, creds_loads = stub_transport
    client = gmail_client_module.GmailClient()
    for _ in range(5):
        _ = client._service
    assert creds_loads["n"] == 1


class _FlakyGmail:
    """Fails the first `failures` fetches the way a dropped socket does."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def get_message(self, message_id: str):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
        return f"message:{message_id}"


def test_a_dropped_connection_is_retried_not_counted_as_a_bad_email(monkeypatch):
    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    gmail = _FlakyGmail(failures=2)

    assert runner_module._fetch_with_retry(gmail, "abc") == "message:abc"
    assert gmail.calls == 3


def test_a_persistently_unreachable_message_still_raises(monkeypatch):
    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    gmail = _FlakyGmail(failures=99)

    with pytest.raises(OSError):
        runner_module._fetch_with_retry(gmail, "abc")
    assert gmail.calls == runner_module._FETCH_ATTEMPTS
