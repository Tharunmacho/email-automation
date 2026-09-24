"""The CRM recruitment pipeline, as the HTTP API actually enforces it.

    Job order match -> shortlisted -> review -> submitted -> interview
        -> selected / on hold / rejected / offer declined
    selected -> offer issued -> accepted  => locked out of new matching
    rejected / offer declined             => back in the available pool

Four statuses describe a candidate and the tests below exist mostly to keep them
apart: `evaluation_status` is our reviewer's verdict, `recruitment_status` is
where the candidate stands with the outside world, and `interview_status` and
`offer_status` are facts about one engagement. Conflating them is what put
"selected" and "offer_declined" — values `evaluation_status` has never defined —
into the database.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.core.models import (
    EVALUATION_STATUSES,
    RECRUITMENT_STATUSES,
    CandidateProfile,
    CandidateRecord,
    SourceEmail,
    StoredResume,
)
from app.db.repository import CandidateRepository

ADMIN = {"id": "admin-1", "email": "admin@adira.test", "name": "Admin", "role": "admin"}


def make_record(candidate_id: str = "cand-1", name: str = "Ravi Kumar") -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id,
        profile=CandidateProfile(
            is_resume=True, confidence=0.9, full_name=name,
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
        assigned_staff_id="staff-1",
        evaluation_status="shortlisted",
    )


@pytest.fixture()
def api():
    """A client wired to a real `CandidateRepository` over an in-memory Mongo.

    The repository is the thing under test as much as the routes are — the
    placement-lock guard lives in `record_recruitment_event` — so it is the real
    one, not a stand-in.
    """
    collection = mongomock.MongoClient()["recruitment"]["candidates"]
    repository = CandidateRepository(collection=collection)
    collection.insert_one(make_record().to_mongo())

    app.dependency_overrides[current_user] = lambda: ADMIN
    with patch("app.api.routes.repo", return_value=repository):
        client = TestClient(app)
        client.repository = repository
        yield client
    app.dependency_overrides.clear()


def current(client) -> CandidateRecord:
    return client.repository.get("cand-1")


def submit(client, target_type="company", target_name="Keppel Shipyard"):
    return client.post(
        "/candidates/cand-1/submit",
        json={"target_type": target_type, "target_name": target_name,
              "job_order_id": "JO-9", "notes": "Strong fit"},
    )


def interview(client, status="completed"):
    return client.post(
        "/candidates/cand-1/interview",
        json={"status": status, "interview_at": "2026-10-01T04:30:00+00:00",
              "notes": "Technical round"},
    )


def outcome(client, status):
    return client.post(
        "/candidates/cand-1/outcome", json={"status": status, "notes": f"Client said {status}"},
    )


def offer(client, status):
    return client.post(
        "/candidates/cand-1/offer", json={"status": status, "notes": f"Offer {status}"},
    )


# --------------------------------------------------------------------------- #
#  Submission
# --------------------------------------------------------------------------- #
def test_submitting_to_a_company_records_target_and_date(api):
    assert submit(api).status_code == 200
    record = current(api)
    assert record.recruitment_status == "submitted_to_company"
    assert record.submission_target_type == "company"
    assert record.submission_target_name == "Keppel Shipyard"
    assert record.submission_date is not None
    assert record.job_order_id == "JO-9"
    # The interview clock starts pending the moment they go out.
    assert record.interview_status == "pending"


def test_submitting_to_an_associate_is_a_distinct_status(api):
    assert submit(api, "associate", "Gulf Associates").status_code == 200
    assert current(api).recruitment_status == "submitted_to_associate"


def test_the_same_submission_twice_is_refused(api):
    assert submit(api).status_code == 200
    duplicate = submit(api)
    assert duplicate.status_code == 409
    assert "already submitted" in duplicate.json()["detail"].lower()
    # One submission, one history entry.
    assert sum(e["type"] == "submitted" for e in current(api).recruitment_history) == 1


def test_a_different_target_is_not_a_duplicate(api):
    assert submit(api, "company", "Keppel Shipyard").status_code == 200
    assert submit(api, "associate", "Gulf Associates").status_code == 200
    assert current(api).submission_target_name == "Gulf Associates"


# --------------------------------------------------------------------------- #
#  Interview
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status,expected",
    [
        ("scheduled", "interviewing"),
        ("pending", "interviewing"),
        ("unavailable", "interviewing"),
        ("completed", "interview_completed"),
    ],
)
def test_interview_status_drives_the_pipeline_stage(api, status, expected):
    submit(api)
    assert interview(api, status).status_code == 200
    record = current(api)
    assert record.interview_status == status
    assert record.recruitment_status == expected


def test_the_interview_time_is_stored_not_just_noted(api):
    submit(api)
    interview(api, "scheduled")
    stored = current(api).interview_at
    assert stored is not None
    # mongomock hands back naive datetimes where the configured driver is
    # tz-aware, so compare the instant rather than the tzinfo. See `_public` in
    # the attendance repository for the same read-side normalisation.
    naive = stored.replace(tzinfo=None) if stored.tzinfo is None else stored.astimezone(timezone.utc).replace(tzinfo=None)
    assert naive == datetime(2026, 10, 1, 4, 30)


# --------------------------------------------------------------------------- #
#  Outcome — requirement 12: keep the four statuses apart
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["selected", "on_hold", "rejected", "offer_declined"])
def test_no_outcome_ever_writes_an_undefined_status(api, status):
    submit(api)
    interview(api)
    assert outcome(api, status).status_code == 200

    record = current(api)
    assert record.evaluation_status in EVALUATION_STATUSES, (
        f"{status} put {record.evaluation_status!r} into evaluation_status"
    )
    assert record.recruitment_status in RECRUITMENT_STATUSES
    # Whatever the pipeline says, the decision itself is always recoverable.
    assert record.last_outcome == status
    assert record.last_outcome_at is not None


def test_selected_advances_the_pipeline_without_touching_the_review_verdict(api):
    submit(api)
    interview(api)
    before = current(api).evaluation_status
    outcome(api, "selected")

    record = current(api)
    assert record.recruitment_status == "selected"
    # "selected" is a client decision, not a verdict on the profile, and
    # `evaluation_status` has never defined it.
    assert record.evaluation_status == before


def test_on_hold_maps_onto_the_verdict_that_means_the_same_thing(api):
    submit(api)
    interview(api)
    outcome(api, "on_hold")
    record = current(api)
    assert record.recruitment_status == "on_hold"
    assert record.evaluation_status == "on_hold"


# --------------------------------------------------------------------------- #
#  Requirement 14 — returning to the pool
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["rejected", "offer_declined"])
def test_a_released_candidate_returns_to_the_available_pool(api, status):
    submit(api)
    interview(api)
    assert outcome(api, status).status_code == 200

    record = current(api)
    assert record.recruitment_status == "available"
    assert record.placement_locked is False
    assert record.last_outcome == status
    # Nothing may still point at the company that passed on them.
    assert record.submission_target_name is None
    assert record.submission_date is None
    assert record.interview_status is None
    assert record.job_order_id is None


@pytest.mark.parametrize("status", ["rejected", "offer_declined"])
def test_a_released_candidate_is_matchable_again(api, status):
    submit(api)
    interview(api)
    outcome(api, status)
    pool = api.get("/job-orders/candidate-pool")
    assert pool.status_code == 200
    assert "cand-1" in {row["id"] for row in pool.json()["items"]}


def test_a_released_candidate_can_be_submitted_somewhere_new(api):
    submit(api, "company", "Keppel Shipyard")
    interview(api)
    outcome(api, "rejected")
    # Not a duplicate: the previous engagement is over.
    assert submit(api, "company", "Keppel Shipyard").status_code == 200
    assert current(api).recruitment_status == "submitted_to_company"


# --------------------------------------------------------------------------- #
#  Offer and the placement lock — requirement 13
# --------------------------------------------------------------------------- #
def test_an_issued_offer_does_not_lock_anything_yet(api):
    submit(api)
    interview(api)
    outcome(api, "selected")
    assert offer(api, "issued").status_code == 200
    record = current(api)
    assert record.offer_status == "issued"
    assert record.recruitment_status == "offer_issued"
    assert record.placement_locked is False


def test_accepting_an_offer_locks_the_candidate_out_of_new_matching(api):
    submit(api)
    interview(api)
    outcome(api, "selected")
    offer(api, "issued")
    assert offer(api, "accepted").status_code == 200

    record = current(api)
    assert record.placement_locked is True
    assert record.recruitment_status == "offer_accepted"
    assert record.evaluation_status == "hired"

    pool = api.get("/job-orders/candidate-pool")
    assert "cand-1" not in {row["id"] for row in pool.json()["items"]}


def test_declining_an_offer_releases_the_candidate_to_the_pool(api):
    submit(api)
    interview(api)
    outcome(api, "selected")
    offer(api, "issued")
    assert offer(api, "declined").status_code == 200

    record = current(api)
    assert record.placement_locked is False
    assert record.recruitment_status == "available"
    assert record.last_outcome == "offer_declined"
    pool = api.get("/job-orders/candidate-pool")
    assert "cand-1" in {row["id"] for row in pool.json()["items"]}


@pytest.mark.parametrize("status", ["selected", "on_hold", "rejected", "offer_declined"])
def test_an_outcome_can_never_unlock_an_accepted_placement(api, status):
    """The corruption requirement 13 calls out by name.

    A placed candidate must not be quietly returned to the pool by somebody
    recording a late outcome against them.
    """
    submit(api)
    interview(api)
    outcome(api, "selected")
    offer(api, "accepted")

    refused = outcome(api, status)
    assert refused.status_code == 409
    assert "locked" in refused.json()["detail"].lower()

    record = current(api)
    assert record.placement_locked is True
    assert record.recruitment_status == "offer_accepted"


def test_the_lock_survives_an_outcome_that_reaches_the_repository_directly(api):
    """Belt and braces: the guard is in the repository, not only the route."""
    submit(api)
    interview(api)
    outcome(api, "selected")
    offer(api, "accepted")

    api.repository.record_recruitment_event(
        "cand-1",
        {"type": "outcome", "status": "rejected"},
        {"recruitment_status": "available", "placement_locked": False},
    )
    assert current(api).placement_locked is True


def test_a_placed_candidate_is_not_submitted_or_interviewed_again(api):
    submit(api)
    interview(api)
    outcome(api, "selected")
    offer(api, "accepted")

    assert submit(api, "company", "Other Yard").status_code == 409
    assert interview(api, "scheduled").status_code == 409
    assert offer(api, "issued").status_code == 409
    # But the placement falling through is a decision that is allowed to be made.
    assert offer(api, "declined").status_code == 200
    assert current(api).placement_locked is False


# --------------------------------------------------------------------------- #
#  History
# --------------------------------------------------------------------------- #
def test_every_movement_is_recorded_in_order_with_its_actor(api):
    submit(api)
    interview(api, "scheduled")
    interview(api, "completed")
    outcome(api, "selected")
    offer(api, "issued")
    offer(api, "accepted")

    history = current(api).recruitment_history
    assert [entry["type"] for entry in history] == [
        "submitted", "interview", "interview", "outcome", "offer", "offer",
    ]
    assert all(entry["at"] is not None for entry in history)
    assert all(entry["actor_id"] == "admin-1" for entry in history)
    assert history[0]["target_name"] == "Keppel Shipyard"
    assert history[-1]["status"] == "accepted"


def test_history_is_appended_to_and_never_replaced(api):
    submit(api)
    # A caller trying to assign the history away must not be able to.
    api.repository.record_recruitment_event(
        "cand-1", {"type": "note"}, {"recruitment_history": []},
    )
    assert len(current(api).recruitment_history) == 2


def test_the_full_successful_journey_ends_locked(api):
    """Workflow E, end to end."""
    assert submit(api).status_code == 200
    assert interview(api, "scheduled").status_code == 200
    assert interview(api, "completed").status_code == 200
    assert outcome(api, "selected").status_code == 200
    assert offer(api, "issued").status_code == 200
    assert offer(api, "accepted").status_code == 200

    record = current(api)
    assert record.recruitment_status == "offer_accepted"
    assert record.placement_locked is True
    assert "cand-1" not in {
        row["id"] for row in api.get("/job-orders/candidate-pool").json()["items"]
    }
