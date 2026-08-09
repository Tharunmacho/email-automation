"""Only one inline poll cycle may run at a time.

Without this, two overlapping POSTs to /ingest/poll each ran a full batch over
the same messages. Both passed the dedup checks before either had inserted, one
ingested, and the slower one reported that same candidate as a pre-existing
duplicate — so the UI showed `Ingested=0` for a poll that had just added a
profile, while the candidate count went up.
"""
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.ingestion.runner import BatchSummary


@pytest.fixture
def client():
    app.dependency_overrides[current_user] = lambda: {
        "id": "u", "email": "u@x.com", "name": "U", "role": "admin",
    }
    with patch("app.api.routes.ensure_indexes"):
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.pop(current_user, None)


def test_a_second_poll_is_declined_while_the_first_is_running(client):
    started = threading.Event()
    release = threading.Event()

    class SlowRunner:
        def run_once(self, query=None):
            started.set()
            release.wait(timeout=5)
            return BatchSummary(fetched=1, processed=1, ingested_candidates=1)

    with patch("app.ingestion.runner.IngestionRunner", SlowRunner):
        first: dict = {}
        worker = threading.Thread(
            target=lambda: first.update(client.post("/ingest/poll").json())
        )
        worker.start()
        assert started.wait(timeout=5), "the first poll never started"

        # Second request arrives mid-batch, exactly as a double-click does.
        second = client.post("/ingest/poll")
        release.set()
        worker.join(timeout=5)

    assert second.status_code == 200
    assert second.json()["skipped_reason"] == "Another poll cycle is already running."
    assert second.json()["ingested_candidates"] == 0
    # The real batch still reports its own work truthfully.
    assert first["ingested_candidates"] == 1


def test_the_lock_is_released_so_the_next_poll_runs(client):
    class Runner:
        def run_once(self, query=None):
            return BatchSummary(fetched=2, processed=1, ingested_candidates=1)

    with patch("app.ingestion.runner.IngestionRunner", Runner):
        first = client.post("/ingest/poll").json()
        second = client.post("/ingest/poll").json()

    assert first["ingested_candidates"] == 1
    assert second["ingested_candidates"] == 1
    assert "skipped_reason" not in second


def test_the_lock_is_released_when_the_batch_raises(client):
    class Exploding:
        def run_once(self, query=None):
            raise RuntimeError("Gmail unreachable")

    with patch("app.ingestion.runner.IngestionRunner", Exploding):
        with pytest.raises(RuntimeError):
            client.post("/ingest/poll")

    class Runner:
        def run_once(self, query=None):
            return BatchSummary(fetched=1, processed=1, ingested_candidates=1)

    with patch("app.ingestion.runner.IngestionRunner", Runner):
        assert client.post("/ingest/poll").json()["ingested_candidates"] == 1
