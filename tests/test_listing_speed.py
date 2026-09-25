"""The two server-side halves of a fast directory load.

Responses are gzip-compressed, and a client fetching pages in parallel can skip
the collection count on every page after the first.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user

ADMIN = {"id": "admin-1", "email": "a@x.com", "name": "Admin", "role": "admin",
         "pages": ["candidates"], "actions": []}


def _row(index: int) -> dict:
    return {"id": f"cand-{index}", "candidate_code": f"C{index}", "status": "processed",
            "profile": {"full_name": f"Candidate {index}", "skills": ["welding", "rigging"]}}


@pytest.fixture()
def api():
    repo = MagicMock()
    repo.list_summaries.return_value = [_row(i) for i in range(50)]
    repo.count.return_value = 5000
    app.dependency_overrides[current_user] = lambda: ADMIN
    with patch("app.api.routes.repo", return_value=repo):
        client = TestClient(app)
        client.repo = repo
        yield client
    app.dependency_overrides.pop(current_user, None)


def test_a_candidate_page_is_compressed(api):
    response = api.get("/candidates?limit=50&skip=50", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert len(response.json()["items"]) == 50


def test_later_pages_can_skip_the_count(api):
    body = api.get("/candidates?limit=50&skip=50&with_total=false").json()
    assert body["total"] is None
    api.repo.count.assert_not_called()


def test_the_count_is_still_the_default(api):
    body = api.get("/candidates?limit=50&skip=50").json()
    assert body["total"] == 5000
