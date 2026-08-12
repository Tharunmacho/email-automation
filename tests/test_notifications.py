"""The feed behind the bell — the half of the pop-up that outlives the moment.

The push was never the problem: a socket that is open when an event fires does
receive it. The problem was everything else. Résumés arrive on a Gmail poll, the
staff member they are allocated to is usually not watching that second, and an
event delivered to nobody left no trace — so the profile turned up in the queue
having never announced itself, and "I never saw a notification" was accurate.

These cover what fixes that: the record is written first, it is scoped to one
person, and neither a dead store nor a dead socket can take the other down.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.db.notifications import NotificationRepository


class FakeNotifications:
    """A stand-in for the collection, with the two queries the repository makes."""

    def __init__(self):
        self.docs: list[dict] = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def find(self, query):
        rows = [d for d in self.docs if self._matches(d, query)]
        return _Cursor(rows)

    def count_documents(self, query):
        return len([d for d in self.docs if self._matches(d, query)])

    def update_many(self, query, update):
        changed = 0
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update["$set"])
                changed += 1
        return _Result(changed)

    @staticmethod
    def _matches(doc, query):
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, key, direction):
        self._rows = sorted(self._rows, key=lambda d: d.get(key) or 0, reverse=direction < 0)
        return self

    def limit(self, count):
        return iter(self._rows[:count])


class _Result:
    def __init__(self, modified_count):
        self.modified_count = modified_count


@pytest.fixture
def notifications():
    return NotificationRepository(collection=FakeNotifications())


# --------------------------------------------------------------------------- #
#  The store
# --------------------------------------------------------------------------- #
def test_a_recorded_notification_is_unread_and_readable_back(notifications):
    notifications.record(
        "staff-1",
        type="candidate_assigned",
        title="New candidate assigned",
        message="Rajesh Kumar was allocated to you.",
        candidate_id="cand-1",
        candidate_name="Rajesh Kumar",
    )

    feed = notifications.list_for("staff-1")
    assert len(feed) == 1
    assert feed[0]["read"] is False
    assert feed[0]["candidate_id"] == "cand-1"
    assert notifications.unread_count("staff-1") == 1


def test_one_persons_feed_is_not_anothers(notifications):
    notifications.record("staff-1", type="candidate_assigned", title="a", message="a")
    notifications.record("staff-2", type="candidate_assigned", title="b", message="b")

    assert len(notifications.list_for("staff-1")) == 1
    assert notifications.unread_count("staff-2") == 1


def test_marking_read_needs_the_row_to_be_yours(notifications):
    """The id alone finds the row, which is exactly why it must not update it —
    a notification id names someone else's feed just as well as your own."""
    note = notifications.record("staff-1", type="candidate_assigned", title="a", message="a")

    assert notifications.mark_read("staff-2", [note.id]) == 0
    assert notifications.unread_count("staff-1") == 1

    assert notifications.mark_read("staff-1", [note.id]) == 1
    assert notifications.unread_count("staff-1") == 0


def test_mark_all_read_clears_only_the_callers_rows(notifications):
    notifications.record("staff-1", type="candidate_assigned", title="a", message="a")
    notifications.record("staff-1", type="candidate_assigned", title="b", message="b")
    notifications.record("staff-2", type="candidate_assigned", title="c", message="c")

    assert notifications.mark_all_read("staff-1") == 2
    assert notifications.unread_count("staff-1") == 0
    assert notifications.unread_count("staff-2") == 1


def test_unread_only_narrows_the_feed(notifications):
    first = notifications.record("staff-1", type="candidate_assigned", title="a", message="a")
    notifications.record("staff-1", type="candidate_assigned", title="b", message="b")
    notifications.mark_read("staff-1", [first.id])

    assert len(notifications.list_for("staff-1")) == 2
    assert len(notifications.list_for("staff-1", unread_only=True)) == 1


# --------------------------------------------------------------------------- #
#  The service: stored first, pushed second, neither able to break the other
# --------------------------------------------------------------------------- #
class FakeUsers:
    def __init__(self, admins):
        self._admins = admins

    def list_admins(self, include_inactive=False):
        return self._admins


class _Admin:
    def __init__(self, user_id):
        self.id = user_id
        self.role = "admin"
        self.active = True


def test_the_staff_member_and_every_admin_get_a_row():
    from app.notifications import notify_candidate_assigned

    repo = NotificationRepository(collection=FakeNotifications())
    users = FakeUsers([_Admin("admin-1"), _Admin("admin-2")])

    with patch("app.api.websocket.publish_event", return_value=True):
        notified = notify_candidate_assigned(
            "staff-1",
            {"id": "cand-1", "full_name": "Rajesh Kumar"},
            staff_name="Sarah Chen",
            repo=repo,
            users=users,
        )

    assert notified == 3
    assert repo.unread_count("staff-1") == 1
    assert repo.unread_count("admin-1") == 1
    assert repo.unread_count("admin-2") == 1
    # The admin's copy names where it went; the staff member's does not need to.
    assert "Sarah Chen" in repo.list_for("admin-1")[0]["message"]


def test_a_push_failure_still_leaves_the_record_behind():
    """Redis being down is the normal case in a single-process dev run, and the
    whole reason the feed exists. It must not cost the notification."""
    from app.notifications import notify_candidate_assigned

    repo = NotificationRepository(collection=FakeNotifications())

    with patch("app.api.websocket.publish_event", side_effect=RuntimeError("redis down")):
        notify_candidate_assigned(
            "staff-1",
            {"id": "cand-1", "full_name": "Rajesh Kumar"},
            repo=repo,
            users=FakeUsers([]),
        )

    assert repo.unread_count("staff-1") == 1


def test_a_store_failure_still_pushes():
    from app.notifications import notify_candidate_assigned

    class Broken:
        def insert_one(self, doc):
            raise RuntimeError("mongo down")

    published = []
    with patch("app.api.websocket.publish_event", lambda e: published.append(e) or True):
        notify_candidate_assigned(
            "staff-1",
            {"id": "cand-1", "full_name": "Rajesh Kumar"},
            repo=NotificationRepository(collection=Broken()),
            users=FakeUsers([]),
        )

    assert [event["type"] for event in published] == [
        "candidate_assigned",
        "candidate_ingested",
    ]


# --------------------------------------------------------------------------- #
#  The endpoints
# --------------------------------------------------------------------------- #
@pytest.fixture
def api():
    collection = FakeNotifications()

    def sign_in_as(role: str, user_id: str):
        app.dependency_overrides[current_user] = lambda: {
            "id": user_id,
            "email": f"{user_id}@x.com",
            "name": user_id,
            "role": role,
        }

    with patch(
        "app.api.routes.NotificationRepository",
        lambda *a, **kw: NotificationRepository(collection=collection),
    ):
        client = TestClient(app)
        client.sign_in_as = sign_in_as  # type: ignore[attr-defined]
        client.collection = collection  # type: ignore[attr-defined]
        try:
            yield client
        finally:
            app.dependency_overrides.pop(current_user, None)


def test_the_feed_is_scoped_to_the_token_not_a_parameter(api):
    """There is no user id to pass, which is the point: no id to substitute."""
    store = NotificationRepository(collection=api.collection)
    store.record("staff-1", type="candidate_assigned", title="mine", message="mine")
    store.record("staff-2", type="candidate_assigned", title="theirs", message="theirs")

    api.sign_in_as("staff", "staff-1")
    body = api.get("/notifications").json()

    assert [row["title"] for row in body["items"]] == ["mine"]
    assert body["unread"] == 1


def test_marking_all_read_over_http(api):
    store = NotificationRepository(collection=api.collection)
    store.record("staff-1", type="candidate_assigned", title="a", message="a")
    store.record("staff-1", type="candidate_assigned", title="b", message="b")

    api.sign_in_as("staff", "staff-1")
    body = api.post("/notifications/read", json={"all": True}).json()

    assert body == {"updated": 2, "unread": 0}


def test_a_staff_member_may_read_the_sla_threshold(api):
    """The queue's countdowns are drawn from it, and /sla/* is admin-only."""
    api.sign_in_as("staff", "staff-1")
    body = api.get("/config").json()

    assert body["sla_threshold_hours"] > 0
