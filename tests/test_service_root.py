"""`/` says what this process is, even when everything behind it is down.

A deployed container answered `GET / -> 404`, and that 404 carried no
information: not whether the API was healthy, not where the UI had gone, not
why ingestion was running inline instead of on a worker. All three had to be
dug out of container logs.

The route exists to answer those three questions at a glance, which means it
must not depend on the things that break. `/health` counts candidates, so it
fails exactly when the database does — precisely the moment someone opens the
service root to find out what is wrong.
"""
from __future__ import annotations

import pytest

from tests.test_api import test_client  # noqa: F401 — the shared API fixture


def test_the_root_answers_at_all(test_client):
    """It used to be a 404."""
    response = test_client.get("/")

    assert response.status_code == 200


def test_it_names_the_service_and_where_to_go_next(test_client):
    body = test_client.get("/").json()

    assert body["service"]
    assert body["version"]
    assert body["status"] == "ok"
    assert body["health"] == "/health"
    assert body["docs"] == "/docs"


def test_it_says_whether_ingestion_is_queued_or_inline(test_client, monkeypatch):
    """The question that cost the most to answer from logs."""
    monkeypatch.setattr("app.tasks.health.workers_online", lambda force=False: False)
    assert "inline" in test_client.get("/").json()["ingestion"]

    monkeypatch.setattr("app.tasks.health.workers_online", lambda force=False: True)
    assert "worker" in test_client.get("/").json()["ingestion"]


def test_a_broker_that_will_not_answer_is_not_an_error(test_client, monkeypatch):
    """A refused connection *is* the answer — it means no worker."""
    def boom(force=False):
        raise RuntimeError("Error 111 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr("app.tasks.health.workers_online", boom)

    response = test_client.get("/")

    assert response.status_code == 200
    assert "inline" in response.json()["ingestion"]


def test_it_answers_with_the_database_down(test_client, monkeypatch):
    """The whole point. `/health` counts candidates and dies with Mongo; this
    must survive it, because it is what someone opens to find out why."""
    from app.api import routes

    def dead(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(routes, "repo", dead)

    assert test_client.get("/").status_code == 200


def test_it_needs_no_credentials(test_client):
    """Diagnosing a broken deployment must not require logging in to it."""
    response = test_client.get("/", headers={})

    assert response.status_code == 200


def test_it_stays_out_of_the_api_schema(test_client):
    """It is a signpost for humans, not part of the API surface."""
    paths = test_client.get("/openapi.json").json().get("paths", {})

    assert "/" not in paths
