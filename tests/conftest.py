"""Keep the test suite off the real database.

`MONGO_URI` in .env points at the production Atlas cluster, and a test that
forgets to patch a collaborator will happily write to it — that is how a
`candidate-alice` tombstone ended up in the live `ingest_ledger`. Every code
path to Mongo goes through `app.db.mongo.get_client`, so refusing there turns a
silent production write into a loud, local failure.

A test that genuinely needs a database should pass its own fake collection
(`IngestLedger(collection=...)`) or patch the repository, as the existing tests
do — never reach for the real one.
"""
from __future__ import annotations

import pytest


class RealDatabaseUsedInTests(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch, request):
    """Fail any test that opens a Mongo connection.

    Opt out with `@pytest.mark.real_database` if a test is ever meant to run
    against a live cluster — nothing does today.
    """
    if "real_database" in request.keywords:
        return

    from app.db import mongo

    # The client is cached per process; a connection opened by an earlier test
    # would otherwise stay usable for every test after it. Hold the real one:
    # by teardown, `mongo.get_client` is still the refusing stub.
    real_get_client = mongo.get_client
    real_get_client.cache_clear()

    def _refuse(*_args, **_kwargs):
        raise RealDatabaseUsedInTests(
            "This test reached the real MongoDB. Patch the repository, ledger, or "
            "collection it uses (see tests/conftest.py)."
        )

    monkeypatch.setattr(mongo, "get_client", _refuse)
    yield
    real_get_client.cache_clear()
