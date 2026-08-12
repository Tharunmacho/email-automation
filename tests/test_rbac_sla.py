"""Data isolation, the evaluation write path, and the 10-hour SLA sweep."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.models import utcnow
from app.tasks import sla_checker


# --------------------------------------------------------------------------- #
#  Repository-level isolation
# --------------------------------------------------------------------------- #
def test_scoped_query_narrows_to_one_staff_member():
    from app.db.repository import _scoped

    assert _scoped(None, "staff-1") == {"assigned_staff_id": "staff-1"}
    assert _scoped({"status": "ingested"}, "staff-1") == {
        "status": "ingested",
        "assigned_staff_id": "staff-1",
    }


def test_scoped_query_is_untouched_for_an_admin():
    """An admin sees everything, so the scope must add no filter at all."""
    from app.db.repository import _scoped

    assert _scoped({"status": "ingested"}, None) == {"status": "ingested"}
    assert _scoped(None, None) == {}


def test_scoped_does_not_mutate_the_caller_query():
    """The projection dicts in this module are shared constants; scoping one in
    place would leak a staff filter into every later request."""
    from app.db.repository import _scoped

    original = {"status": "ingested"}
    _scoped(original, "staff-1")
    assert original == {"status": "ingested"}


# --------------------------------------------------------------------------- #
#  SLA sweep
# --------------------------------------------------------------------------- #
class FakeAlerts:
    """Stand-in for the `sla_alerts` collection."""

    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None):
        status = (query or {}).get("status")
        return [d for d in self.docs if status is None or d.get("status") == status]

    def insert_many(self, docs):
        self.docs.extend(docs)

    def update_many(self, query, update):
        status = query.get("status")
        excluded = set(query.get("candidate_id", {}).get("$nin", []))
        changed = 0
        for doc in self.docs:
            if doc.get("status") == status and doc["candidate_id"] not in excluded:
                doc.update(update["$set"])
                changed += 1

        class Result:
            modified_count = changed

        return Result()


class FakeRepo:
    def __init__(self, rows):
        self._rows = rows

    def find_sla_breaches(self, cutoff):
        return [r for r in self._rows if r["assigned_at"] < cutoff]


def breach_row(cid, hours_ago, staff="s1", viewed=None, status="pending"):
    return {
        "_id": cid,
        "assigned_staff_id": staff,
        "assigned_staff_name": "Sam",
        "assigned_at": utcnow() - timedelta(hours=hours_ago),
        "viewed_at": viewed,
        "evaluation_status": status,
        "profile": {"full_name": f"Candidate {cid}"},
    }


@pytest.fixture
def sla_env(monkeypatch):
    """Wire the sweep to in-memory fakes and capture what it would publish.

    Patched at `app.api.websocket`, which is where the function lives. The sweep
    reaches it through `app.notifications` — a breach is recorded to the admins'
    feed as well as pushed, because a sweep on a beat timer fires at whatever
    hour the interval lands on and the modal only reaches whoever is watching.
    """
    alerts = FakeAlerts()
    published = []
    monkeypatch.setattr(sla_checker, "get_alerts_collection", lambda: alerts)
    monkeypatch.setattr(
        "app.api.websocket.publish_event", lambda e: published.append(e) or True
    )
    return alerts, published


def test_breach_is_recorded_and_pushed_once(sla_env, monkeypatch):
    alerts, published = sla_env
    repo = FakeRepo([breach_row("c1", hours_ago=12)])
    monkeypatch.setattr(sla_checker, "CandidateRepository", lambda: repo)

    result = sla_checker.scan(threshold_hours=10)

    assert result["in_breach"] == 1
    assert result["new_alerts"] == 1
    assert len(alerts.docs) == 1
    assert alerts.docs[0]["status"] == "active"
    assert len(published) == 1
    assert published[0]["type"] == "sla_alert"
    assert published[0]["channel"] == "admin"


def test_second_sweep_does_not_re_alert_the_same_profile(sla_env, monkeypatch):
    """The sweep runs every 15 minutes. Re-firing the same modal each time is
    what gets an alert channel muted, and it would inflate the audit log into
    saying a hundred separate things went wrong."""
    alerts, published = sla_env
    repo = FakeRepo([breach_row("c1", hours_ago=12)])
    monkeypatch.setattr(sla_checker, "CandidateRepository", lambda: repo)

    sla_checker.scan(threshold_hours=10)
    second = sla_checker.scan(threshold_hours=10)

    assert second["in_breach"] == 1
    assert second["new_alerts"] == 0
    assert len(alerts.docs) == 1
    assert len(published) == 1


def test_alert_resolves_once_the_profile_is_dealt_with(sla_env, monkeypatch):
    alerts, _ = sla_env
    rows = [breach_row("c1", hours_ago=12)]
    monkeypatch.setattr(sla_checker, "CandidateRepository", lambda: FakeRepo(rows))
    sla_checker.scan(threshold_hours=10)

    # The staff member opens and evaluates it, so it drops out of the breach set.
    rows.clear()
    result = sla_checker.scan(threshold_hours=10)

    assert result["resolved"] == 1
    assert alerts.docs[0]["status"] == "resolved"
    assert alerts.docs[0]["resolved_at"] is not None


def test_profile_inside_the_threshold_is_not_a_breach(sla_env, monkeypatch):
    alerts, published = sla_env
    monkeypatch.setattr(
        sla_checker, "CandidateRepository", lambda: FakeRepo([breach_row("c1", hours_ago=3)])
    )
    result = sla_checker.scan(threshold_hours=10)

    assert result["in_breach"] == 0
    assert alerts.docs == []
    assert published == []


def test_breach_reason_distinguishes_unviewed_from_unevaluated(monkeypatch):
    """The admin's first question is always "did they even look at it?"."""
    rows = [
        breach_row("never-opened", hours_ago=12, viewed=None),
        breach_row("opened-not-judged", hours_ago=12, viewed=utcnow()),
    ]
    monkeypatch.setattr(sla_checker, "CandidateRepository", lambda: FakeRepo(rows))

    by_id = {b["candidate_id"]: b for b in sla_checker.find_breaches(threshold_hours=10)}

    assert by_id["never-opened"]["reason"] == "unviewed"
    assert by_id["opened-not-judged"]["reason"] == "unevaluated"


def test_naive_assigned_at_does_not_crash_the_sweep(monkeypatch):
    """Documents written before the driver returned tz-aware datetimes come
    back naive, and subtracting one from an aware `now` raises TypeError."""
    row = breach_row("c1", hours_ago=12)
    row["assigned_at"] = row["assigned_at"].replace(tzinfo=None)
    monkeypatch.setattr(sla_checker, "CandidateRepository", lambda: FakeRepo([row]))

    # The repository's own query is what filters by cutoff; the fake compares
    # directly, so only the hours_overdue arithmetic is under test here.
    breaches = sla_checker._row_to_alert(row, utcnow())
    assert breaches["hours_overdue"] == pytest.approx(12.0, abs=0.2)


# --------------------------------------------------------------------------- #
#  Event payloads
# --------------------------------------------------------------------------- #
def test_assignment_event_is_addressed_to_one_staff_member():
    from app.api.websocket import candidate_assigned_event

    event = candidate_assigned_event("staff-7", {"id": "c1", "full_name": "Ada Lovelace"})

    assert event["channel"] == "staff"
    assert event["staff_id"] == "staff-7"
    # The specified toast copy, verbatim.
    assert event["message"] == "🔔 New Candidate Profile Ingested & Assigned: Ada Lovelace"


def test_assignment_toast_falls_back_to_the_email_address():
    """A résumé the parser could not pull a name from still has to be
    identifiable, or the staff member cannot tell what just arrived."""
    from app.api.websocket import candidate_assigned_event

    event = candidate_assigned_event("staff-7", {"id": "c1", "email": "ada@example.com"})
    assert "ada@example.com" in event["message"]


def test_single_sla_breach_names_the_staff_member_and_the_candidate():
    from app.api.websocket import sla_alert_event

    event = sla_alert_event(
        [{"assigned_staff_name": "Sarah Chen", "full_name": "Ada Lovelace", "hours_overdue": 11}],
        10,
    )
    assert event["message"] == (
        "⚠️ SLA Breach: Staff Sarah Chen has not viewed/evaluated "
        "candidate Ada Lovelace for >10 hours."
    )


def test_batched_sla_breaches_are_summarised_rather_than_listed():
    """One line naming ten candidates is unreadable; the modal lists them."""
    from app.api.websocket import sla_alert_event

    many = sla_alert_event([{"full_name": f"c{i}"} for i in range(4)], 10)
    assert many["count"] == 4
    assert many["message"] == (
        "⚠️ SLA Breach: 4 assigned profiles have not been viewed/evaluated for >10 hours."
    )
