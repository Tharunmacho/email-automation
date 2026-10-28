"""Mapped Candidates: the gated steps after a candidate is mapped to a job order.

    resume -> interview -> selection -> offer -> visa -> travel

Each step opens only once the one before it is done. Managers and admins may
skip a step; staff may not, and staff see and move only the candidates
allocated to them. Interview, selection and offer answers go through the same
transitions as the recruitment pipeline, so the two screens agree.
"""
from __future__ import annotations

from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.core.models import CandidateProfile, CandidateRecord, SourceEmail, StoredResume
from app.db.repository import CandidateRepository
from app.db.users import pages_for

ADMIN = {"id": "admin-1", "email": "admin@adira.test", "name": "Admin", "role": "admin"}
MANAGER = {"id": "mgr-1", "email": "mgr@adira.test", "name": "Meera", "role": "manager"}
STAFF = {"id": "staff-1", "email": "s1@adira.test", "name": "Sam", "role": "staff"}
OTHER_STAFF = {"id": "staff-2", "email": "s2@adira.test", "name": "Priya", "role": "staff"}

ORDER = {
    "id": "JO-9", "title": "Welder", "client": "Keppel Shipyard",
    "destinationCountry": "Singapore", "status": "OPEN", "headcount": 4,
    "skills": ["TIG"], "salary": "", "dueDate": "2026-12-01",
}


def make_record(candidate_id: str, owner: str = "staff-1") -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id,
        profile=CandidateProfile(
            is_resume=True, confidence=0.9, full_name=f"Person {candidate_id}",
            email=f"{candidate_id}@example.com", destination_country="Singapore",
        ),
        resume=StoredResume(
            original_filename=f"{candidate_id}.pdf", mime_type="application/pdf",
            size=1024, sha256=f"hash-{candidate_id}",
            storage_backend="local", storage_key=f"k/{candidate_id}",
        ),
        source_email=SourceEmail(
            message_id=f"m-{candidate_id}", thread_id=f"t-{candidate_id}",
            from_addr=f"{candidate_id}@example.com",
        ),
        status="ingested",
        assigned_staff_id=owner,
        evaluation_status="shortlisted",
    )


@pytest.fixture()
def api():
    database = mongomock.MongoClient()["recruitment"]
    repository = CandidateRepository(collection=database["candidates"])
    for candidate_id, owner in (("cand-1", "staff-1"), ("cand-2", "staff-2"), ("cand-3", "staff-1")):
        database["candidates"].insert_one(make_record(candidate_id, owner).to_mongo())
    database["job_orders"].insert_one(dict(ORDER))

    client = TestClient(app)
    client.repository = repository
    client.act_as = lambda user: app.dependency_overrides.__setitem__(current_user, lambda: user)
    client.act_as(ADMIN)
    with patch("app.api.routes.repo", return_value=repository), \
         patch("app.db.mongo.get_db", return_value=database):
        yield client
    app.dependency_overrides.clear()


def map_to_order(client, candidate_id="cand-1", order_id="JO-9"):
    """What Job Orders does when it submits a candidate against an order."""
    response = client.post(
        f"/candidates/{candidate_id}/submit",
        json={"target_type": "company", "target_name": "Keppel Shipyard", "job_order_id": order_id},
    )
    assert response.status_code == 200, response.text
    return response


def stage(client, name, candidate_id="cand-1", **body):
    return client.post(f"/mapping/candidates/{candidate_id}/stages/{name}", json=body)


def states(row):
    return {step["key"]: step["state"] for step in row["mapping"]["stages"]}


def rows(client):
    response = client.get("/mapping/candidates")
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["items"]}


def walk_to(client, last, candidate_id="cand-1"):
    """Answer every step up to and including `last` with its passing answer."""
    answers = {
        "resume": {"answer": "submitted"},
        "interview": {"answer": "scheduled", "interview_at": "2026-11-02T05:30:00+00:00"},
        "selection": {"answer": "selected"},
        "offer": {"answer": "accepted"},
        "visa": {"stamping": "completed", "medical": "fit"},
        "travel": {"answer": "travelled", "travel_date": "2026-12-04",
                   "travel_time": "22:15", "flight_number": "sq 423"},
    }
    for name, body in answers.items():
        response = stage(client, name, candidate_id, **body)
        assert response.status_code == 200, f"{name}: {response.text}"
        if name == last:
            return response.json()
    raise AssertionError(last)


# --------------------------------------------------------------------------- #
#  Listing and scope
# --------------------------------------------------------------------------- #
def test_only_mapped_candidates_are_listed_with_their_job_order(api):
    map_to_order(api)
    listed = rows(api)
    assert set(listed) == {"cand-1"}
    row = listed["cand-1"]
    assert row["job_order"]["title"] == "Welder"
    assert row["job_order"]["client"] == "Keppel Shipyard"
    assert row["mapping"]["status"] == "in_progress"
    assert row["mapping"]["current_stage"] == "resume"
    assert states(row) == {
        "resume": "current", "interview": "locked", "selection": "locked",
        "offer": "locked", "visa": "locked", "travel": "locked",
    }
    # The stored pipeline is summarised into `mapping`, not shipped raw.
    assert "mapping_pipeline" not in row


def test_staff_see_only_their_allocated_mapped_candidates(api):
    map_to_order(api, "cand-1")
    map_to_order(api, "cand-2")
    api.act_as(STAFF)
    assert set(rows(api)) == {"cand-1"}
    api.act_as(OTHER_STAFF)
    assert set(rows(api)) == {"cand-2"}
    api.act_as(MANAGER)
    assert set(rows(api)) == {"cand-1", "cand-2"}


def test_staff_cannot_move_a_candidate_allocated_to_someone_else(api):
    map_to_order(api, "cand-2")
    api.act_as(STAFF)
    assert stage(api, "resume", "cand-2", answer="submitted").status_code == 404


def test_staff_reach_mapping_candidates_without_a_grant():
    assert "mapping-candidates" in pages_for("staff")
    # It does not drag Job Orders in with it.
    assert "job-orders" not in pages_for("staff")


def test_a_deleted_job_order_still_names_itself(api):
    map_to_order(api, order_id="JO-404")
    row = rows(api)["cand-1"]
    assert row["job_order"] == {"id": "JO-404", "missing": True}


# --------------------------------------------------------------------------- #
#  Gating
# --------------------------------------------------------------------------- #
def test_a_step_cannot_be_answered_before_the_one_before_it(api):
    map_to_order(api)
    refused = stage(api, "interview", answer="scheduled", interview_at="2026-11-02T05:30:00Z")
    assert refused.status_code == 409
    assert "Resume submitted" in refused.json()["detail"]


def test_not_yet_keeps_the_step_open_and_the_next_one_locked(api):
    map_to_order(api)
    row = stage(api, "resume", answer="not_submitted").json()
    assert states(row)["resume"] == "current"
    assert states(row)["interview"] == "locked"
    assert row["mapping"]["stages"][0]["entry"]["answer"] == "not_submitted"

    row = stage(api, "resume", answer="submitted").json()
    assert states(row)["resume"] == "passed"
    assert states(row)["interview"] == "current"


def test_an_earlier_step_is_frozen_once_a_later_one_is_recorded(api):
    map_to_order(api)
    walk_to(api, "interview")
    refused = stage(api, "resume", answer="not_submitted")
    assert refused.status_code == 409
    assert "Interview scheduled" in refused.json()["detail"]


def test_the_latest_answer_can_still_be_corrected(api):
    map_to_order(api)
    stage(api, "resume", answer="submitted")
    corrected = stage(api, "resume", answer="not_submitted")
    assert corrected.status_code == 200
    assert states(corrected.json())["interview"] == "locked"


@pytest.mark.parametrize(
    "name,body,message",
    [
        ("resume", {"answer": "maybe"}, "Choose an answer"),
        ("interview", {"answer": "scheduled"}, "date and time"),
    ],
)
def test_incomplete_answers_are_refused(api, name, body, message):
    map_to_order(api)
    if name == "interview":
        walk_to(api, "resume")
    refused = stage(api, name, **body)
    assert refused.status_code == 400
    assert message in refused.json()["detail"]


def test_an_unknown_step_is_not_found(api):
    map_to_order(api)
    assert stage(api, "medical", answer="fit").status_code == 404


# --------------------------------------------------------------------------- #
#  Skipping
# --------------------------------------------------------------------------- #
def test_staff_cannot_skip_a_step(api):
    map_to_order(api)
    api.act_as(STAFF)
    refused = stage(api, "resume", skip=True)
    assert refused.status_code == 403
    assert "managers and admins" in refused.json()["detail"]


@pytest.mark.parametrize("user", [MANAGER, ADMIN])
def test_managers_and_admins_skip_to_open_the_next_step(api, user):
    map_to_order(api)
    api.act_as(user)
    row = stage(api, "resume", skip=True, notes="Sent by the client's agent").json()
    assert states(row)["resume"] == "skipped"
    assert states(row)["interview"] == "current"
    entry = row["mapping"]["stages"][0]["entry"]
    assert entry["skipped"] is True
    assert entry["actor_name"] == user["name"]


def test_skipping_the_offer_records_no_offer(api):
    """A skip opens the next step; it does not invent an acceptance."""
    map_to_order(api)
    walk_to(api, "selection")
    api.act_as(MANAGER)
    row = stage(api, "offer", skip=True).json()
    assert states(row)["visa"] == "current"
    record = api.repository.get("cand-1")
    assert record.placement_locked is False
    assert record.offer_status is None


# --------------------------------------------------------------------------- #
#  The recruitment pipeline moves with it
# --------------------------------------------------------------------------- #
def test_a_scheduled_interview_is_recorded_on_the_candidate(api):
    map_to_order(api)
    walk_to(api, "interview")
    record = api.repository.get("cand-1")
    assert record.interview_status == "scheduled"
    assert record.interview_at is not None
    assert record.recruitment_status == "interviewing"


def test_accepting_the_offer_locks_the_placement_and_visa_opens(api):
    map_to_order(api)
    row = walk_to(api, "offer")
    record = api.repository.get("cand-1")
    assert record.placement_locked is True
    assert record.recruitment_status == "offer_accepted"
    assert states(row)["visa"] == "current"
    # Still mapped: an accepted placement keeps its job order.
    assert rows(api)["cand-1"]["mapping"]["job_order_id"] == "JO-9"


@pytest.mark.parametrize(
    "last,name,body,closed_reason,last_outcome",
    [
        ("interview", "selection", {"answer": "rejected"}, "rejected", "rejected"),
        ("selection", "offer", {"answer": "rejected"}, "offer_rejected", "offer_declined"),
    ],
)
def test_a_rejection_closes_the_mapping_and_returns_the_candidate_to_the_pool(
    api, last, name, body, closed_reason, last_outcome,
):
    map_to_order(api)
    walk_to(api, last)
    row = stage(api, name, **body).json()

    assert row["mapping"]["status"] == "closed"
    assert row["mapping"]["closed"]["stage"] == name
    assert row["mapping"]["closed"]["reason"] == closed_reason
    assert states(row)[name] == "failed"

    record = api.repository.get("cand-1")
    assert record.recruitment_status == "available"
    assert record.job_order_id is None
    assert record.placement_locked is False
    assert record.last_outcome == last_outcome

    # Still listed, under the order it was closed against.
    listed = rows(api)["cand-1"]
    assert listed["job_order"]["id"] == "JO-9"
    assert listed["mapping"]["status"] == "closed"

    refused = stage(api, name, answer="selected" if name == "selection" else "accepted")
    assert refused.status_code == 409
    assert "closed" in refused.json()["detail"]


def test_a_rejection_recorded_on_the_profile_also_closes_the_mapping(api):
    map_to_order(api)
    walk_to(api, "interview")
    api.post("/candidates/cand-1/outcome", json={"status": "rejected", "notes": "Client passed"})
    row = rows(api)["cand-1"]
    assert row["mapping"]["status"] == "closed"
    assert row["mapping"]["closed"]["stage"] == "selection"
    # The answers given before the rejection are kept.
    assert states(row)["resume"] == "passed"


def test_an_interview_recorded_on_the_profile_shows_here(api):
    map_to_order(api)
    api.post(
        "/candidates/cand-1/interview",
        json={"status": "scheduled", "interview_at": "2026-11-02T05:30:00Z", "notes": "Round 1"},
    )
    row = rows(api)["cand-1"]
    interview = row["mapping"]["stages"][1]
    assert interview["state"] == "passed"
    assert interview["entry"]["derived"] is True
    # The resume step is still asked: nothing else records it.
    assert row["mapping"]["current_stage"] == "resume"


def test_unsubmitting_removes_the_mapping(api):
    map_to_order(api)
    walk_to(api, "resume")
    api.post("/candidates/cand-1/unsubmit", json={"job_order_id": "JO-9"})
    assert "cand-1" not in rows(api)
    assert api.repository.get("cand-1").mapping_pipeline is None


def test_mapping_to_a_new_order_starts_afresh(api):
    map_to_order(api)
    walk_to(api, "interview")
    stage(api, "selection", answer="rejected")
    map_to_order(api, order_id="JO-10")
    row = rows(api)["cand-1"]
    assert row["mapping"]["job_order_id"] == "JO-10"
    assert row["mapping"]["current_stage"] == "resume"
    assert states(row)["resume"] == "current"


# --------------------------------------------------------------------------- #
#  Visa and travel
# --------------------------------------------------------------------------- #
def test_medically_unfit_holds_the_visa_step_open(api):
    map_to_order(api)
    walk_to(api, "offer")
    row = stage(api, "visa", stamping="completed", medical="unfit").json()
    assert states(row)["visa"] == "current"
    assert states(row)["travel"] == "locked"
    refused = stage(api, "travel", answer="travelled", travel_date="2026-12-04",
                    travel_time="22:15", flight_number="SQ 423")
    assert refused.status_code == 409


def test_visa_needs_both_answers(api):
    map_to_order(api)
    walk_to(api, "offer")
    refused = stage(api, "visa", stamping="completed")
    assert refused.status_code == 400
    assert "medical" in refused.json()["detail"]


@pytest.mark.parametrize(
    "body,message",
    [
        ({"travel_time": "22:15", "flight_number": "SQ 423"}, "travel date"),
        ({"travel_date": "2026-12-04", "flight_number": "SQ 423"}, "departure time"),
        ({"travel_date": "2026-12-04", "travel_time": "22:15"}, "flight number"),
        ({"travel_date": "2026-12-04", "travel_time": "25:00", "flight_number": "SQ 423"}, "departure time"),
    ],
)
def test_travelled_needs_date_time_and_flight(api, body, message):
    map_to_order(api)
    walk_to(api, "visa")
    refused = stage(api, "travel", answer="travelled", **body)
    assert refused.status_code == 400
    assert message in refused.json()["detail"]


def test_the_full_journey_ends_travelled_with_flight_details(api):
    map_to_order(api)
    row = walk_to(api, "travel")
    assert row["mapping"]["status"] == "completed"
    assert row["mapping"]["current_stage"] is None
    assert set(states(row).values()) == {"passed"}
    travel = row["mapping"]["stages"][-1]["entry"]
    assert travel["travel_date"] == "2026-12-04"
    assert travel["travel_time"] == "22:15"
    assert travel["flight_number"] == "SQ 423"

    history = [event for event in api.repository.get("cand-1").recruitment_history
               if event["type"] == "mapping"]
    assert [event["stage"] for event in history] == [
        "resume", "interview", "selection", "offer", "visa", "travel",
    ]
    assert all(event["actor_id"] == "admin-1" for event in history)
