"""Deliberate cross-country / office reassignment.

A candidate assigned to Singapore who can no longer go there, and asks for
Europe instead, must be movable — to another country, another office and
another desk — without anyone having to edit the destination on their profile
until the desk eligibility check happens to pass.

Ordinary allocation keeps its guard rail: `POST /candidates/{id}/assign` still
refuses a cross-desk placement. The reassignment route is the supported way
through, and it records both sides of every move.
"""
from __future__ import annotations

from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.core.models import CandidateProfile, CandidateRecord, SourceEmail, StoredResume
from app.db.repository import CandidateRepository

ADMIN = {"id": "admin-1", "email": "admin@adira.test", "name": "Admin", "role": "admin"}

COUNTRIES = [
    {"id": "singapore", "name": "Singapore", "active": True},
    {"id": "germany", "name": "Germany", "active": True},
]
OFFICES = {
    "mount_road": {"id": "mount_road", "name": "Mount Road", "country": "", "active": True},
    "closed_office": {"id": "closed_office", "name": "Old Branch", "active": False},
}


class FakeUser:
    def __init__(self, user_id, name, email, role="staff", active=True):
        self.id, self.name, self.email = user_id, name, email
        self.role, self.active = role, active


# `sreya.adira@gmail.com` is on the Singapore/Malaysia desk list in the
# balancer; the Europe recruiter deliberately is not.
STAFF = {
    "sg-1": FakeUser("sg-1", "Sreya", "sreya.adira@gmail.com"),
    "eu-1": FakeUser("eu-1", "Anand", "anand.europe@adira.test"),
    "gone": FakeUser("gone", "Former", "former@adira.test", active=False),
}


def make_record() -> CandidateRecord:
    return CandidateRecord(
        id="cand-1",
        profile=CandidateProfile(
            is_resume=True, confidence=0.9, full_name="Ravi Kumar",
            email="ravi@example.com", destination_country="Singapore",
        ),
        resume=StoredResume(
            original_filename="r.pdf", mime_type="application/pdf", size=10,
            sha256="hash-1", storage_backend="local", storage_key="k/1",
        ),
        source_email=SourceEmail(message_id="m1", thread_id="t1", from_addr="ravi@example.com"),
        status="ingested",
        assigned_staff_id="sg-1",
        assigned_staff_name="Sreya",
        evaluation_status="shortlisted",
        evaluation_score=4,
    )


@pytest.fixture()
def api():
    collection = mongomock.MongoClient()["reassign"]["candidates"]
    collection.insert_one(make_record().to_mongo())
    repository = CandidateRepository(collection=collection)

    app.dependency_overrides[current_user] = lambda: ADMIN
    with patch("app.api.routes.repo", return_value=repository), \
         patch("app.db.taxonomy.list_countries", return_value=COUNTRIES), \
         patch("app.db.taxonomy.get_office", side_effect=OFFICES.get), \
         patch.object(type(__import__("app.api.routes", fromlist=["users"]).users), "get",
                      lambda _self, uid: STAFF.get(uid)):
        client = TestClient(app)
        client.repository = repository
        yield client
    app.dependency_overrides.clear()


def reassign(client, **overrides):
    body = {
        "destination_country": "Germany",
        "office_id": "mount_road",
        "staff_id": "eu-1",
        "reason": "Candidate withdrew from Singapore and requested Europe",
    }
    body.update(overrides)
    return client.post("/candidates/cand-1/reassign", json=body)


def current(client) -> CandidateRecord:
    return client.repository.get("cand-1")


def test_a_candidate_moves_country_office_and_desk_in_one_action(api):
    response = reassign(api)
    assert response.status_code == 200, response.text

    record = current(api)
    assert record.profile.destination_country == "Germany"
    assert record.office_id == "mount_road"
    assert record.office_name == "Mount Road"
    assert record.assigned_staff_id == "eu-1"


def test_the_move_crosses_a_desk_that_ordinary_assignment_refuses(api):
    """The block this route exists to get past, and the proof it is still there."""
    blocked = api.post(
        "/candidates/cand-1/assign", json={"staff_id": "eu-1", "remarks": "move"},
    )
    assert blocked.status_code == 400
    assert "desk" in blocked.json()["detail"].lower()
    # The deliberate route goes through.
    assert reassign(api).status_code == 200


def test_both_sides_of_every_move_are_recorded_with_actor_and_reason(api):
    reassign(api)
    history = current(api).assignment_history
    entry = next(row for row in history if row.get("type") == "reassignment")

    assert entry["from_country"] == "Singapore"
    assert entry["to_country"] == "Germany"
    assert entry["from_office_id"] is None
    assert entry["to_office_name"] == "Mount Road"
    assert entry["from_desk"] == "singapore_malaysia"
    assert entry["to_desk"] == "general"
    assert entry["from_staff_id"] == "sg-1"
    assert entry["to_staff_id"] == "eu-1"
    assert entry["by_user_id"] == "admin-1"
    assert entry["reason"].startswith("Candidate withdrew")
    assert entry["at"] is not None
    # The candidate moved between desks...
    assert entry["desk_changed"] is True
    # ...but the new owner is the right desk for the new destination, so nothing
    # was actually overridden. See the test below for a move that does override.
    assert entry["desk_override"] is False


def test_a_reassignment_restarts_the_review_for_the_new_owner(api):
    reassign(api)
    record = current(api)
    assert record.evaluation_status == "pending"
    assert record.evaluation_score is None
    assert record.viewed_at is None
    assert record.latest_assignment_remark.startswith("Candidate withdrew")


def test_a_reason_is_required(api):
    assert reassign(api, reason="").status_code == 422


def test_the_destination_must_be_an_active_country(api):
    response = reassign(api, destination_country="Atlantis")
    assert response.status_code == 422
    assert "country" in response.json()["detail"].lower()
    assert current(api).profile.destination_country == "Singapore"


def test_a_retired_office_is_refused(api):
    response = reassign(api, office_id="closed_office")
    assert response.status_code == 422
    assert "office" in response.json()["detail"].lower()


def test_an_inactive_target_staff_member_is_refused(api):
    assert reassign(api, staff_id="gone").status_code == 400
    assert current(api).assigned_staff_id == "sg-1"


def test_the_country_can_move_without_naming_a_new_owner(api):
    """Moving the destination and leaving allocation for later is allowed."""
    assert reassign(api, staff_id=None).status_code == 200
    record = current(api)
    assert record.profile.destination_country == "Germany"
    # Ownership and the existing review are untouched.
    assert record.assigned_staff_id == "sg-1"
    assert record.evaluation_status == "shortlisted"


def test_reassignment_requires_the_reallocation_permission(api):
    app.dependency_overrides[current_user] = lambda: {
        "id": "staff-9", "email": "s@x.test", "name": "Staff", "role": "staff",
    }
    try:
        assert reassign(api).status_code in (403, 404)
        assert current(api).profile.destination_country == "Singapore"
    finally:
        app.dependency_overrides[current_user] = lambda: ADMIN


def test_an_unknown_candidate_is_a_404(api):
    response = api.post(
        "/candidates/nope/reassign",
        json={"destination_country": "Germany", "reason": "x"},
    )
    assert response.status_code == 404


def test_an_owner_who_is_wrong_for_the_new_destination_is_flagged_as_an_override(api):
    """Keeping a candidate on Singapore but handing them to a general-desk
    recruiter is exactly the rule ordinary allocation refuses to break, so the
    record says so."""
    response = reassign(api, destination_country="Singapore", staff_id="eu-1")
    assert response.status_code == 200

    entry = next(
        row for row in current(api).assignment_history if row.get("type") == "reassignment"
    )
    assert entry["to_country"] == "Singapore"
    assert entry["desk_changed"] is True
    assert entry["desk_override"] is True
