"""Matching end to end: the routes, the matcher, the store, against mongomock.

No network anywhere: the ranking is computed in-process from the candidate
records, which is the whole point of the design.
"""
from __future__ import annotations

import importlib.util
from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.core.models import CandidateProfile, CandidateRecord, SourceEmail, StoredResume, TradeLicense
from app.db.repository import CandidateRepository
from app.matching import matcher, store

ADMIN = {"id": "admin-1", "email": "admin@adira.test", "name": "Admin", "role": "admin"}
STAFF_NO_ORDERS = {"id": "s-9", "email": "s9@adira.test", "name": "Nadia", "role": "staff",
                   "pages": ["candidates"]}

ORDER = {
    "id": "JO-7", "title": "Welder", "designation": "TIG Welder", "client": "Keppel Shipyard",
    "destinationCountry": "Singapore", "status": "OPEN", "headcount": 4,
    "skills": ["TIG Welding"], "salary": "", "dueDate": "2026-12-01",
    "minExperience": "3 years", "industry": "Fabrication",
}


def make_record(candidate_id: str, *, designation: str, summary: str = "", skills=(), years=None,
                licences=(), **profile_extra) -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id,
        profile=CandidateProfile(
            is_resume=True, confidence=0.9, full_name=f"Person {candidate_id}",
            email=f"{candidate_id}@example.com", phone="+919800000000",
            passport_number=f"P{candidate_id}", current_designation=designation,
            skills=list(skills), total_experience_years=years, resume_summary=summary,
            licenses=[TradeLicense(name=name, number="SECRET-NO") for name in licences],
            destination_country="Singapore", **profile_extra,
        ),
        resume=StoredResume(
            original_filename=f"{candidate_id}.pdf", mime_type="application/pdf", size=1024,
            sha256=f"hash-{candidate_id}", storage_backend="local", storage_key=f"k/{candidate_id}",
        ),
        source_email=SourceEmail(message_id=f"m-{candidate_id}", thread_id=f"t-{candidate_id}",
                                 from_addr=f"{candidate_id}@example.com"),
        status="ingested",
        assigned_staff_id="staff-1",
    )


CANDIDATES = [
    make_record("exact", designation="TIG Welder", summary="TIG welder, 6 years on pipe spools.",
                skills=["TIG Welding"], years=6),
    # Never says "welder": the trade and the process are worded differently.
    make_record("equiv", designation="Fabricator", summary="Fabricator who joins pipe with GTAW.",
                skills=["Argon arc welding"], years=5),
    make_record("short", designation="TIG Welder", summary="Strong welder, one year.",
                skills=["TIG Welding"], years=1),
    make_record("unknown", designation="Welder", summary="Welder, no dates given.", skills=["TIG Welding"]),
    make_record("cook", designation="Commis Chef", summary="Tandoori cook.", skills=["Tandoor"], years=8),
]


@pytest.fixture()
def api():
    database = mongomock.MongoClient()["recruitment"]
    repository = CandidateRepository(collection=database["candidates"])
    for record in CANDIDATES:
        database["candidates"].insert_one(record.to_mongo())
    database["job_orders"].insert_one(dict(ORDER))

    client = TestClient(app)
    client.db = database
    client.act_as = lambda user: app.dependency_overrides.__setitem__(current_user, lambda: user)
    client.act_as(ADMIN)
    matcher.reset()
    with patch("app.api.routes.repo", return_value=repository), \
         patch("app.db.mongo.get_db", return_value=database):
        store.ensure_indexes()
        yield client
    matcher.reset()
    app.dependency_overrides.clear()


def results(client, order_id="JO-7", **params):
    response = client.get(f"/job-orders/{order_id}/matching/results", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def by_id(payload):
    return {item["candidate_id"]: item for item in payload["items"]}


def state(client, order_id="JO-7", **params):
    response = client.get(f"/job-orders/{order_id}/matching", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
#  Ranking
# --------------------------------------------------------------------------- #
def test_it_ranks_by_category_then_score_with_nothing_to_run(api):
    items = results(api)["items"]
    order = [item["candidate_id"] for item in items]
    assert order[0] == "exact"
    assert order[-1] == "short"  # ineligible sorts last, whatever its score
    rows = by_id({"items": items})
    assert rows["exact"]["category"] == "strong_match"
    assert rows["exact"]["eligibility"] == "met"
    assert rows["exact"]["matched_skills"] == ["TIG Welding"]
    assert rows["cook"]["category"] == "low_match"


def test_the_same_trade_in_other_words_is_matched(api):
    equiv = by_id(results(api))["equiv"]
    assert equiv["matched_skills"] == ["TIG Welding"]  # argon arc = GTAW = TIG
    assert equiv["category"] in {"strong_match", "potential_match"}


def test_too_little_mandatory_experience_is_ineligible(api):
    short = by_id(results(api))["short"]
    assert short["category"] == "ineligible"
    assert short["eligibility"] == "not_met"
    assert short["failed_requirements"] == ["At least 3 years of relevant experience"]


def test_unknown_experience_goes_to_manual_review(api):
    unknown = by_id(results(api))["unknown"]
    assert unknown["category"] == "manual_review"
    assert unknown["eligibility"] == "unverified"


def test_state_counts_every_category(api):
    body = state(api)
    assert body["pool"] == 5 and body["active"] is True
    assert sum(body["counts"].values()) == 5
    assert body["counts"]["ineligible"] == 1 and body["counts"]["manual_review"] == 1
    assert "Not a probability" not in str(body)  # the label lives on the explanation


def test_the_explanation_shows_its_working_and_quotes_the_record(api):
    detail = api.get("/job-orders/JO-7/matching/results/equiv").json()
    assert "Not a probability" in detail["score_label"]
    keys = {c["key"]: c for c in detail["components"]}
    assert {"role", "skills", "experience", "relevance", "documents"} <= set(keys)
    assert keys["qualifications"]["applicable"] is False
    assert keys["skills"]["detail"].startswith("1 of 1 skills")
    assert detail["requirements"][0]["status"] == "satisfied"
    excerpt = next(e for e in detail["evidence"] if e["requirement"] == "TIG Welding")
    assert excerpt["field"].startswith("profile.") and "Argon" in excerpt["excerpt"]


def test_identifiers_are_not_in_any_response(api):
    payload = str(results(api)) + str(api.get("/job-orders/JO-7/matching/results/exact").json())
    for value in ("Pexact", "+919800000000", "exact@example.com", "SECRET-NO"):
        assert value not in payload


def test_results_filter_and_page(api):
    assert {item["candidate_id"] for item in results(api, category="ineligible")["items"]} == {"short"}
    assert results(api, eligibility="unverified")["total"] == 1
    assert results(api, skill="TIG Welding")["total"] == 4
    assert results(api, min_experience=5)["total"] == 3  # exact 6, equiv 5, cook 8
    paged = results(api, page=2, page_size=2)
    assert paged["page"] == 2 and len(paged["items"]) == 2 and paged["unfiltered_total"] == 5


def test_a_closed_order_is_ranked_for_reference(api):
    api.db["job_orders"].update_one({"id": "JO-7"}, {"$set": {"status": "FILLED"}})
    body = state(api)
    assert body["active"] is False and "for reference" in body["inactive_reason"]
    assert results(api)["total"] == 5


def test_an_edited_order_is_ranked_against_its_new_requirements(api):
    assert by_id(results(api))["equiv"]["category"] != "ineligible"
    api.db["job_orders"].update_one({"id": "JO-7"}, {"$set": {"minExperience": "6 years"}})
    assert by_id(results(api))["equiv"]["category"] == "ineligible"  # 5 years < 6


# --------------------------------------------------------------------------- #
#  The pool
# --------------------------------------------------------------------------- #
def test_submitted_candidates_are_not_ranked_on_any_order(api):
    api.db["candidates"].update_one({"_id": "short"}, {"$set": {
        "recruitment_status": "submitted_to_company", "job_order_id": "JO-7"}})
    api.db["candidates"].update_one({"_id": "cook"}, {"$set": {"placement_locked": True}})
    rows = by_id(results(api))
    assert "short" not in rows and "cook" not in rows
    assert state(api)["pool"] == 3


def test_a_released_candidate_returns(api):
    api.db["candidates"].update_one({"_id": "exact"}, {"$set": {"recruitment_status": "submitted_to_company"}})
    assert "exact" not in by_id(results(api))
    api.db["candidates"].update_one({"_id": "exact"}, {"$set": {"recruitment_status": "available"}})
    assert "exact" not in by_id(results(api))  # the pool read a moment ago is reused...
    assert state(api, fresh=True)["pool"] == 5  # ...until it is re-read
    assert "exact" in by_id(results(api))


def test_a_duplicate_copy_is_not_ranked_twice(api):
    api.db["candidates"].insert_one(make_record("dup", designation="TIG Welder").to_mongo())
    api.db["candidates"].update_one({"_id": "dup"}, {"$set": {"duplicate_of": "exact"}})
    matcher.reset()
    assert "dup" not in by_id(results(api, page_size=50))


def test_a_candidate_entered_without_a_cv_is_ranked(api):
    bot = make_record("bot", designation="Welder")
    doc = bot.to_mongo()
    doc["profile"]["is_resume"] = False
    api.db["candidates"].insert_one(doc)
    matcher.reset()
    row = by_id(results(api))["bot"]
    assert row["category"] == "manual_review"  # title fits, nothing else to go on


def test_new_candidates_appear_on_refresh(api):
    assert state(api)["pool"] == 5
    api.db["candidates"].insert_one(make_record(
        "fresh", designation="TIG Welder", skills=["TIG Welding"], years=4).to_mongo())
    assert state(api)["pool"] == 5  # read a moment ago and reused
    assert state(api, fresh=True)["pool"] == 6
    assert "fresh" in by_id(results(api))


# --------------------------------------------------------------------------- #
#  Decisions, access, and what is never touched
# --------------------------------------------------------------------------- #
def test_a_recruiter_decision_is_kept_across_re_ranking(api):
    response = api.put("/job-orders/JO-7/matching/results/equiv/review",
                       json={"status": "shortlisted", "note": "Good welds"})
    assert response.status_code == 200
    assert response.json()["by_name"] == "Admin"
    api.db["job_orders"].update_one({"id": "JO-7"}, {"$set": {"skills": ["TIG Welding", "Rigging"]}})
    row = by_id(results(api))["equiv"]
    assert row["review"]["status"] == "shortlisted" and row["review"]["note"] == "Good welds"
    assert results(api, review="shortlisted")["total"] == 1
    history = api.get("/job-orders/JO-7/matching/results/equiv").json()["review_history"]
    assert history[0]["status"] == "shortlisted"


def test_a_decision_needs_a_candidate_in_the_pool(api):
    response = api.put("/job-orders/JO-7/matching/results/nobody/review", json={"status": "rejected"})
    assert response.status_code == 404
    assert api.get("/job-orders/JO-7/matching/results/nobody").status_code == 404
    assert api.get("/job-orders/JO-404/matching").status_code == 404


def test_matching_is_behind_the_job_orders_grant(api):
    api.act_as(STAFF_NO_ORDERS)
    for path in ("/job-orders/JO-7/matching", "/job-orders/JO-7/matching/results",
                 "/job-orders/JO-7/matching/results/exact"):
        assert api.get(path).status_code == 404


def test_the_original_records_are_never_written(api):
    before = list(api.db["candidates"].find({}, sort=[("_id", 1)]))
    results(api)
    api.get("/job-orders/JO-7/matching/results/exact")
    api.put("/job-orders/JO-7/matching/results/exact/review", json={"status": "rejected"})
    assert list(api.db["candidates"].find({}, sort=[("_id", 1)])) == before
    assert api.db["job_orders"].find_one({"id": "JO-7"}, {"_id": 0}) == ORDER


def test_no_external_matching_service_remains():
    for module in ("app.matching.jev", "app.matching.runner", "app.matching.autorun", "app.matching.questions"):
        assert importlib.util.find_spec(module) is None
