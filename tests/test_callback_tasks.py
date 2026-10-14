"""The callback task list behind the Tasks screen.

Three things are worth pinning down and nothing else is: when an unanswered
call becomes due and then overdue, that a staff member is shown only their own
desk, and that an admin is shown everybody's.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.core.models import utcnow


class FakeCursor:
    """Just enough of a Mongo cursor for the endpoint's `find().sort().limit()`."""

    def __init__(self, rows):
        self._rows = rows

    def sort(self, key, direction=1):
        self._rows = sorted(
            self._rows,
            key=lambda row: (row.get(key) is None, row.get(key)),
            reverse=direction < 0,
        )
        return self

    def limit(self, _count):
        return self

    def __iter__(self):
        return iter(self._rows)


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, _projection=None):
        def matches(row):
            return all(row.get(field) == value for field, value in query.items())

        return FakeCursor([row for row in self.rows if matches(row)])


class FakeRepo:
    def __init__(self, rows):
        self._coll = FakeCollection(rows)


def _row(candidate_id, name, hours_ago, staff_id, staff_name):
    return {
        "_id": candidate_id,
        "candidate_code": candidate_id.upper(),
        "evaluation_status": "callback",
        "evaluated_at": utcnow() - timedelta(hours=hours_ago),
        "evaluation_notes": f"Rang {name}, no answer.",
        "assigned_staff_id": staff_id,
        "assigned_staff_name": staff_name,
        "profile": {"full_name": name, "phone": "+91 90000 00000"},
    }


ROWS = [
    # Two hours old: the staff member is not late, there is nothing to chase.
    _row("c-fresh", "Fresh Call", 2, "staff-1", "Riya"),
    # Over a day: due for the second call.
    _row("c-due", "Due Call", 30, "staff-1", "Riya"),
    # Over two days: the point the screen calls overdue.
    _row("c-overdue", "Overdue Call", 70, "staff-1", "Riya"),
    # Somebody else's desk entirely.
    _row("c-other", "Other Desk", 60, "staff-2", "Arun"),
]


@pytest.fixture(autouse=True)
def _no_index_build():
    with patch("app.api.routes.ensure_indexes"):
        yield


def _client(user):
    app.dependency_overrides[current_user] = lambda: user
    try:
        with patch("app.api.routes.repo", return_value=FakeRepo(ROWS)):
            yield TestClient(app)
    finally:
        app.dependency_overrides.pop(current_user, None)


@pytest.fixture
def admin_client():
    yield from _client({
        "id": "admin-1",
        "email": "admin@example.com",
        "name": "Admin",
        "role": "admin",
    })


@pytest.fixture
def staff_client():
    yield from _client({
        "id": "staff-1",
        "email": "riya@example.com",
        "name": "Riya",
        "role": "staff",
        "pages": ["candidates", "tasks"],
    })


def test_admin_sees_every_staff_members_callbacks(admin_client):
    body = admin_client.get("/tasks").json()

    assert body["scope"] == "all"
    assert body["counts"] == {"total": 4, "overdue": 2, "due": 1, "scheduled": 1}
    assert {item["staff_name"] for item in body["items"]} == {"Riya", "Arun"}


def test_staff_see_only_the_callbacks_on_their_own_candidates(staff_client):
    body = staff_client.get("/tasks").json()

    assert body["scope"] == "own"
    assert [item["candidate_id"] for item in sorted(body["items"], key=lambda i: i["candidate_id"])] == [
        "c-due",
        "c-fresh",
        "c-overdue",
    ]
    assert body["counts"]["total"] == 3


def test_a_callback_turns_overdue_at_forty_eight_hours(staff_client):
    body = staff_client.get("/tasks").json()
    states = {item["candidate_id"]: item["state"] for item in body["items"]}

    assert states == {"c-fresh": "scheduled", "c-due": "due", "c-overdue": "overdue"}
    assert body["due_after_hours"] == 24
    assert body["overdue_after_hours"] == 48


def test_the_longest_waiting_call_is_listed_first(staff_client):
    body = staff_client.get("/tasks").json()

    assert [item["candidate_id"] for item in body["items"]] == ["c-overdue", "c-due", "c-fresh"]


def test_the_remark_that_explains_the_callback_is_carried_through(staff_client):
    body = staff_client.get("/tasks").json()
    overdue = next(item for item in body["items"] if item["candidate_id"] == "c-overdue")

    assert overdue["remarks"] == "Rang Overdue Call, no answer."
    assert overdue["phone"] == "+91 90000 00000"
    # The screen draws a countdown from these rather than recomputing the
    # thresholds itself.
    assert overdue["due_at"] is not None
    assert overdue["overdue_at"] is not None
