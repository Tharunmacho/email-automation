"""Matching end to end: the routes, the runner, the store, against mongomock.

Jev is replaced by `FakeJev`, which answers each documented question type in
the documented shape and decides *how* to answer from a marker in the
candidate's résumé summary — the only way to tell candidates apart, since the
state deliberately carries no name or id. Background runs execute inline.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.config import settings
from app.core.models import CandidateProfile, CandidateRecord, SourceEmail, StoredResume, TradeLicense
from app.db.repository import CandidateRepository
from app.matching import jev, store
from app.matching.jev import JevError, JevResponse
from app.matching.questions import NOT_SATISFIED, REQUIREMENT_PREFIX, SATISFIED, UNKNOWN

ADMIN = {"id": "admin-1", "email": "admin@adira.test", "name": "Admin", "role": "admin"}
STAFF_NO_ORDERS = {"id": "s-9", "email": "s9@adira.test", "name": "Nadia", "role": "staff",
                   "pages": ["candidates"]}
MANAGER = {"id": "mgr-1", "email": "mgr@adira.test", "name": "Meera", "role": "manager"}

ORDER = {
    "id": "JO-7", "title": "Welder", "designation": "TIG Welder", "client": "Keppel Shipyard",
    "destinationCountry": "Singapore", "status": "OPEN", "headcount": 4,
    "skills": ["TIG Welding"], "salary": "", "dueDate": "2026-12-01",
    "minExperience": "3 years", "industry": "Fabrication",
}

DEFAULT = {
    "role_relevance": 4, "skills_match": 4, "experience_match": 3, "qualifications_match": 3,
    "document_readiness": 3, "evidence_quality": 4,
    "salary_fit": "not_stated", "destination_fit": "matches", "availability_fit": "not_stated",
    "equivalent_title": 0.9, "conflicting_evidence": 0.05, "requirement": SATISFIED,
}


def _answer(question: Dict[str, Any], value: Any) -> Dict[str, Any]:
    kind = question["type"]
    if kind == "score":
        levels = len(question["criteria"])
        return {"type": "score", "score": float(value), "confidence": 0.9,
                "legend": {str(i): text for i, text in enumerate(question["criteria"])},
                "probabilities": {str(i): (1.0 if i == value else 0.0) for i in range(levels)}}
    if kind == "choice":
        return {"type": "choice", "choice": value, "confidence": 0.95,
                "probabilities": {option: (0.95 if option == value else 0.05 / max(1, len(question["criteria"]) - 1))
                                  for option in question["criteria"]}}
    return {"type": "noul", "noul": float(value)}


class FakeJev:
    """Answers like Jev, decided per candidate by a marker in the summary."""

    def __init__(self, behaviours: Dict[str, Any] | None = None):
        self.behaviours = behaviours or {}
        self.calls = []

    def __call__(self, state, questions, **_kwargs):
        self.calls.append({"state": state, "questions": questions})
        summary = state["candidate"].get("resume_summary", "")
        behaviour = next((b for marker, b in self.behaviours.items() if marker in summary), {})
        if isinstance(behaviour, BaseException):
            raise behaviour
        if behaviour == "garbage":
            return JevResponse(model="jev-1.14.0", answers={"what": {"type": "box"}}, input_tokens=900)
        merged = {**DEFAULT, **behaviour}
        answers = {}
        for key, question in questions.items():
            if key.startswith(REQUIREMENT_PREFIX):
                answers[key] = _answer(question, merged.get(key, merged["requirement"]))
            else:
                answers[key] = _answer(question, merged[key])
        return JevResponse(model="jev-1.14.0", answers=answers, input_tokens=1500, output_tokens=40)


def make_record(candidate_id: str, *, designation: str, summary: str, skills=(), years=None,
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
    make_record("exact", designation="TIG Welder", summary="EXACT TIG welder, 6 years.",
                skills=["TIG Welding"], years=6),
    make_record("equiv", designation="Fabricator", summary="EQUIV fabricator who TIG welds pipe spools.",
                skills=["Argon arc welding"], years=5),
    make_record("short", designation="TIG Welder", summary="SHORT strong welder, one year.",
                skills=["TIG Welding"], years=1),
    make_record("unknown", designation="Welder", summary="UNKNOWN welder, no dates given.",
                skills=["TIG Welding"]),
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
    client.fake = FakeJev({
        "EXACT": {},
        "EQUIV": {"role_relevance": 4, "skills_match": 3},
        "SHORT": {"experience_match": 1, "requirement": NOT_SATISFIED},
        "UNKNOWN": {"experience_match": 0, "requirement": UNKNOWN},
    })
    client.act_as = lambda user: app.dependency_overrides.__setitem__(current_user, lambda: user)
    client.act_as(ADMIN)
    jev.reset_metrics()
    with patch("app.api.routes.repo", return_value=repository), \
         patch("app.db.mongo.get_db", return_value=database), \
         patch.object(settings, "jev_api_key", "test-key"), \
         patch("app.matching.jev.system_one", client.fake), \
         patch("app.matching.runner._spawn", side_effect=lambda work: work()):
        store.ensure_indexes()
        yield client
    app.dependency_overrides.clear()


def run_match(client, order_id="JO-7", **body):
    response = client.post(f"/job-orders/{order_id}/matching/runs", json=body)
    return response


def results(client, order_id="JO-7", **params):
    response = client.get(f"/job-orders/{order_id}/matching/results", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def by_id(payload):
    return {item["candidate_id"]: item for item in payload["items"]}


# --------------------------------------------------------------------------- #
def test_a_run_ranks_by_category_then_score(api):
    response = run_match(api)
    assert response.status_code == 202, response.text
    run = api.get(f"/job-orders/JO-7/matching/runs/{response.json()['id']}").json()
    assert run["status"] == "completed"
    assert run["retrieved"] == 4 and run["scored"] == 4 and run["failed"] == 0
    assert run["input_tokens"] == 4 * 1500
    assert run["cost_usd"] == pytest.approx(4 * 1500 / 1e6 * 0.042)

    items = results(api)["items"]
    order = [item["candidate_id"] for item in items]
    assert order[:2] == ["exact", "equiv"]
    assert order[-1] == "short"  # ineligible sorts last, whatever its score
    rows = by_id({"items": items})
    assert rows["exact"]["category"] == "strong_match"
    assert rows["exact"]["eligibility"] == "met"
    assert rows["exact"]["matched_skills"] == ["TIG Welding"]


def test_an_equivalent_title_is_matched_not_rejected(api):
    run_match(api)
    equiv = by_id(results(api))["equiv"]
    assert equiv["category"] in {"strong_match", "potential_match"}
    # Its own words were sent for Jev to judge, not filtered out beforehand.
    summaries = [call["state"]["candidate"].get("current_designation") for call in api.fake.calls]
    assert "Fabricator" in summaries


def test_strong_skills_but_too_little_mandatory_experience_is_ineligible(api):
    run_match(api)
    short = by_id(results(api))["short"]
    assert short["category"] == "ineligible"
    assert short["eligibility"] == "not_met"
    assert short["failed_requirements"] == ["At least 3 years of relevant experience"]


def test_unverifiable_experience_goes_to_manual_review(api):
    run_match(api)
    unknown = by_id(results(api))["unknown"]
    assert unknown["category"] == "manual_review"
    assert unknown["eligibility"] == "unverified"


def test_identifiers_are_never_sent(api):
    run_match(api)
    sent = repr([call["state"] for call in api.fake.calls])
    for value in ("Pexact", "+919800000000", "exact@example.com", "SECRET-NO", "Person exact"):
        assert value not in sent


def test_the_explanation_carries_evidence_and_versions(api):
    run_match(api)
    detail = api.get("/job-orders/JO-7/matching/results/exact").json()
    assert "Not a probability" in detail["score_label"]
    assert {c["key"] for c in detail["components"]} >= {"skills", "experience", "documents"}
    assert detail["requirements"][0]["status"] == SATISFIED
    excerpt = next(e for e in detail["evidence"] if e["requirement"] == "TIG Welding")
    assert excerpt["field"].startswith("profile.")
    assert detail["versions"]["schema"] == "jev-match-1"
    assert detail["versions"]["model"] == "jev-1.14.0"
    assert detail["baseline"]["score"] > 0


def test_a_repeat_run_reuses_results_and_spends_nothing(api):
    run_match(api)
    calls = len(api.fake.calls)
    second = run_match(api).json()
    run = api.get(f"/job-orders/JO-7/matching/runs/{second['id']}").json()
    assert run["cached"] == 4 and run["scored"] == 0
    assert len(api.fake.calls) == calls
    assert api.db[store.MATCHES].count_documents({}) == 4  # no duplicate results


def test_a_changed_candidate_is_re_evaluated_and_the_old_result_kept_as_history(api):
    run_match(api)
    api.db["candidates"].update_one({"_id": "exact"}, {"$set": {"profile.skills": ["TIG Welding", "Rigging"]}})
    assert by_id(results(api))["exact"]["stale"]["candidate_changed"] is True

    second = run_match(api).json()
    run = api.get(f"/job-orders/JO-7/matching/runs/{second['id']}").json()
    assert run["scored"] == 1 and run["cached"] == 3
    assert api.db[store.MATCHES].count_documents({"candidate_id": "exact"}) == 2
    assert api.db[store.MATCHES].count_documents({"candidate_id": "exact", "is_current": True}) == 1
    assert by_id(results(api))["exact"]["stale"]["candidate_changed"] is False


def test_an_edited_order_invalidates_its_results(api):
    run_match(api)
    api.db["job_orders"].update_one({"id": "JO-7"}, {"$set": {"minExperience": "5 years"}})
    assert all(item["stale"]["order_changed"] for item in results(api)["items"])
    run_match(api)
    assert not any(item["stale"]["order_changed"] for item in results(api)["items"])


def test_when_jev_fails_the_previous_result_stays_and_the_failure_is_shown(api):
    run_match(api)
    before = by_id(results(api))["exact"]["score"]
    api.fake.behaviours["EXACT"] = JevError("Jev returned 529: overloaded", status=529, retryable=True)
    second = run_match(api, force=True).json()
    run = api.get(f"/job-orders/JO-7/matching/runs/{second['id']}").json()
    assert run["status"] == "partial" and run["failed"] == 1
    assert "retried" in run["error"]

    exact = by_id(results(api))["exact"]
    assert exact["score"] == before  # never replaced by a fabricated value
    assert exact["failure"]["retryable"] is True and "529" in exact["failure"]["message"]


def test_a_candidate_never_scored_and_failing_shows_as_failed_not_zero(api):
    api.fake.behaviours["EXACT"] = JevError("Jev could not be reached", retryable=True)
    run_match(api)
    exact = by_id(results(api))["exact"]
    assert exact["status"] == "failed"
    assert exact["score"] is None and exact["category"] is None


def test_incomplete_model_output_is_a_failure_not_a_score(api):
    api.fake.behaviours["EXACT"] = "garbage"
    run_match(api)
    exact = by_id(results(api))["exact"]
    assert exact["status"] == "failed"
    assert "no usable answers" in exact["failure"]["message"]


def test_a_retry_only_resends_what_failed(api):
    api.fake.behaviours["EXACT"] = JevError("Jev returned 429: slow", status=429, retryable=True)
    first = run_match(api).json()
    api.fake.behaviours["EXACT"] = {}
    calls = len(api.fake.calls)
    retry = api.post(f"/job-orders/JO-7/matching/runs/{first['id']}/retry")
    assert retry.status_code == 202
    run = api.get(f"/job-orders/JO-7/matching/runs/{retry.json()['id']}").json()
    assert run["scored"] == 1 and run["cached"] == 3
    assert len(api.fake.calls) == calls + 1
    assert by_id(results(api))["exact"]["status"] == "scored"


def test_a_closed_order_is_refused_with_a_reason(api):
    api.db["job_orders"].update_one({"id": "JO-7"}, {"$set": {"status": "CLOSED"}})
    response = run_match(api)
    assert response.status_code == 409
    assert "historical rematch" in response.json()["detail"]
    assert api.fake.calls == []


def test_a_closed_order_can_be_analysed_through_the_explicit_rematch(api):
    api.db["job_orders"].update_one({"id": "JO-7"}, {"$set": {"status": "FILLED"}})
    response = run_match(api, mode="rematch")
    assert response.status_code == 202
    assert response.json()["historical"] is True
    assert all(item["historical"] for item in results(api)["items"])


def test_an_empty_candidate_pool_completes_with_nothing(api):
    api.db["candidates"].delete_many({})
    run = run_match(api).json()
    stored = api.get(f"/job-orders/JO-7/matching/runs/{run['id']}").json()
    assert stored["status"] == "completed" and stored["retrieved"] == 0
    assert results(api)["total"] == 0


def test_a_second_concurrent_run_on_the_same_order_is_refused(api):
    with patch("app.matching.runner._spawn"):  # first run never starts executing
        first = run_match(api)
    assert first.status_code == 202
    second = run_match(api)
    assert second.status_code == 409
    assert second.json()["detail"]["run"]["id"] == first.json()["id"]


def test_a_queued_run_can_be_cancelled(api):
    with patch("app.matching.runner._spawn"):
        run = run_match(api).json()
    cancelled = api.post(f"/job-orders/JO-7/matching/runs/{run['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["cancel_requested"] is True


def test_without_a_key_matching_reports_unconfigured(api):
    with patch.object(settings, "jev_api_key", ""):
        response = run_match(api)
        state = api.get("/job-orders/JO-7/matching").json()
    assert response.status_code == 503
    assert "JEV_API_KEY" in response.json()["detail"]
    assert state["configured"] is False
    assert api.fake.calls == []


def test_the_key_is_never_in_a_response(api):
    with patch.object(settings, "jev_api_key", "sk-live-should-not-leak"):
        run_match(api)
        bodies = [api.get("/job-orders/JO-7/matching").text, api.get("/job-orders/matching/health").text,
                  api.get("/job-orders/JO-7/matching/results").text]
    assert not any("sk-live-should-not-leak" in body for body in bodies)


def test_matching_is_behind_the_job_orders_grant(api):
    run_match(api)
    api.act_as(STAFF_NO_ORDERS)
    with patch("app.api.routes._has_page", return_value=False):
        assert api.get("/job-orders/JO-7/matching/results").status_code == 404
        assert run_match(api).status_code == 404
        assert api.put("/job-orders/JO-7/matching/results/exact/review",
                       json={"status": "shortlisted"}).status_code == 404


def test_health_is_admin_only(api):
    api.act_as(MANAGER)
    assert api.get("/job-orders/matching/health").status_code == 403
    api.act_as(ADMIN)
    health = api.get("/job-orders/matching/health").json()
    assert health["configured"] is True and "latency_p95_seconds" in health


def test_a_recruiter_decision_survives_rescoring(api):
    run_match(api)
    response = api.put("/job-orders/JO-7/matching/results/equiv/review",
                       json={"status": "shortlisted", "note": "Strong TIG on pipe"})
    assert response.status_code == 200
    assert response.json()["by_name"] == "Admin"

    api.db["job_orders"].update_one({"id": "JO-7"}, {"$set": {"skills": ["TIG Welding", "Rigging"]}})
    run_match(api)
    equiv = by_id(results(api))["equiv"]
    assert equiv["review"]["status"] == "shortlisted"
    assert equiv["review"]["note"] == "Strong TIG on pipe"
    assert results(api, review="shortlisted")["total"] == 1


def test_a_decision_needs_a_match_to_decide_on(api):
    response = api.put("/job-orders/JO-7/matching/results/exact/review", json={"status": "rejected"})
    assert response.status_code == 404


def test_results_filter_and_page(api):
    run_match(api)
    assert {i["candidate_id"] for i in results(api, category="ineligible")["items"]} == {"short"}
    assert {i["candidate_id"] for i in results(api, eligibility="unverified")["items"]} == {"unknown"}
    assert results(api, skill="TIG Welding")["total"] >= 1
    assert all((i["experience_years"] or 0) >= 5 for i in results(api, min_experience=5)["items"])
    page = results(api, page=2, page_size=3)
    assert page["total"] == 4 and len(page["items"]) == 1


def test_refresh_reevaluates_one_candidate(api):
    run_match(api)
    calls = len(api.fake.calls)
    response = api.post("/job-orders/JO-7/matching/results/exact/refresh", params={"force": "true"})
    assert response.status_code == 202
    run = api.get(f"/job-orders/JO-7/matching/runs/{response.json()['id']}").json()
    assert run["considered"] == 1 and run["scored"] == 1
    assert len(api.fake.calls) == calls + 1


def test_the_original_candidate_records_are_never_written(api):
    before = list(api.db["candidates"].find({}, sort=[("_id", 1)]))
    run_match(api)
    api.put("/job-orders/JO-7/matching/results/exact/review", json={"status": "rejected"})
    assert list(api.db["candidates"].find({}, sort=[("_id", 1)])) == before
    assert api.db["job_orders"].find_one({"id": "JO-7"}, {"_id": 0}) == ORDER


# --------------------------------------------------------------------------- #
#  Coverage: every eligible candidate is read by Jev unless a cap says otherwise
# --------------------------------------------------------------------------- #
#: Describes the work in words the order never uses. A keyword shortlist would
#: rank this person last; Jev must read them anyway.
NO_KEYWORDS = make_record(
    "nokw", designation="Boilermaker",
    summary="NOKW joins stainless plate with an argon-shielded tungsten arc.", years=7,
)


def test_by_default_every_eligible_candidate_is_sent_to_jev(api):
    assert settings.jev_max_candidates_per_run == 0
    api.db["candidates"].insert_one(NO_KEYWORDS.to_mongo())
    run = run_match(api).json()
    stored = api.get(f"/job-orders/JO-7/matching/runs/{run['id']}").json()
    assert stored["cap"] is None
    assert stored["retrieved"] == stored["considered"] == 5
    assert stored["skipped"] == 0
    sent = [call["state"]["candidate"].get("current_designation") for call in api.fake.calls]
    assert "Boilermaker" in sent  # last on keywords, still read


def test_a_configured_cap_is_reported_never_hidden(api):
    api.db["candidates"].insert_one(NO_KEYWORDS.to_mongo())
    with patch.object(settings, "jev_max_candidates_per_run", 3):
        run = run_match(api).json()
    stored = api.get(f"/job-orders/JO-7/matching/runs/{run['id']}").json()
    assert stored["cap"] == 3
    assert stored["considered"] == 3 and stored["skipped"] == 2
    sent = [call["state"]["candidate"].get("current_designation") for call in api.fake.calls]
    assert "Boilermaker" not in sent  # the lowest keyword score is what a cap drops

    coverage = api.get("/job-orders/JO-7/matching").json()["coverage"]
    assert coverage == {**coverage, "pool": 5, "evaluated": 3, "not_evaluated": 2,
                        "skipped_by_cap": 2, "cap": 3}


def test_coverage_counts_newcomers_and_ignores_people_who_left_the_pool(api):
    run_match(api)
    coverage = api.get("/job-orders/JO-7/matching").json()["coverage"]
    assert (coverage["pool"], coverage["evaluated"], coverage["not_evaluated"]) == (4, 4, 0)

    # One newcomer arrives and one scored candidate is placed elsewhere. The
    # two must not cancel out into "all checked".
    api.db["candidates"].insert_one(NO_KEYWORDS.to_mongo())
    api.db["candidates"].update_one({"_id": "exact"}, {"$set": {"placement_locked": True}})
    coverage = api.get("/job-orders/JO-7/matching").json()["coverage"]
    assert (coverage["pool"], coverage["evaluated"], coverage["not_evaluated"]) == (4, 3, 1)
    assert coverage["skipped_by_cap"] == 0

    # Checking them now sends only the newcomer; everyone else is reused.
    calls = len(api.fake.calls)
    run = run_match(api).json()
    stored = api.get(f"/job-orders/JO-7/matching/runs/{run['id']}").json()
    assert stored["scored"] == 1 and stored["cached"] == 3
    assert len(api.fake.calls) == calls + 1
    assert api.get("/job-orders/JO-7/matching").json()["coverage"]["not_evaluated"] == 0


def test_a_single_candidate_refresh_does_not_redefine_coverage(api):
    with patch.object(settings, "jev_max_candidates_per_run", 2):
        run_match(api)
    api.post("/job-orders/JO-7/matching/results/exact/refresh", params={"force": "true"})
    coverage = api.get("/job-orders/JO-7/matching").json()["coverage"]
    # Still the full run's story: two of four were left out by the cap.
    assert coverage["skipped_by_cap"] == 2 and coverage["pool"] == 4


# --------------------------------------------------------------------------- #
#  The pool: everyone except candidates already submitted
# --------------------------------------------------------------------------- #
def test_already_submitted_candidates_are_not_analysed_on_any_order(api):
    # Submitted to this very order, and out with a client on another one.
    api.db["candidates"].update_one({"_id": "short"}, {"$set": {
        "recruitment_status": "submitted_to_company", "job_order_id": "JO-7"}})
    api.db["candidates"].update_one({"_id": "unknown"}, {"$set": {
        "recruitment_status": "interviewing", "job_order_id": "JO-99"}})
    run = run_match(api).json()
    stored = api.get(f"/job-orders/JO-7/matching/runs/{run['id']}").json()
    assert stored["retrieved"] == 2
    summaries = [call["state"]["candidate"].get("resume_summary", "") for call in api.fake.calls]
    assert not any(s.startswith(("SHORT", "UNKNOWN")) for s in summaries)
    assert {item["candidate_id"] for item in results(api)["items"]} == {"exact", "equiv"}


def test_a_candidate_entered_without_a_cv_is_analysed(api):
    no_cv = make_record("nocv", designation="Welder", summary="NOCV entered by a recruiter, no CV.")
    api.db["candidates"].insert_one(no_cv.to_mongo())
    api.db["candidates"].update_one({"_id": "nocv"}, {"$set": {"profile.is_resume": False}})
    run_match(api)
    assert any(call["state"]["candidate"].get("resume_summary", "").startswith("NOCV")
               for call in api.fake.calls)
    assert "nocv" in by_id(results(api))


def test_a_candidate_submitted_after_scoring_leaves_the_shortlist(api):
    run_match(api)
    before = api.get("/job-orders/JO-7/matching").json()
    assert by_id(results(api))["exact"]["category"] == "strong_match"

    api.db["candidates"].update_one({"_id": "exact"}, {"$set": {
        "recruitment_status": "submitted_to_company", "job_order_id": "JO-7"}})
    after = api.get("/job-orders/JO-7/matching").json()
    assert "exact" not in by_id(results(api))
    assert after["counts"]["strong_match"] == before["counts"]["strong_match"] - 1
    assert after["coverage"]["pool"] == 3
    assert after["coverage"]["evaluated"] == 3 and after["coverage"]["not_evaluated"] == 0

    refresh = api.post("/job-orders/JO-7/matching/results/exact/refresh")
    assert refresh.status_code == 409
    assert "already been submitted" in refresh.json()["detail"]


def test_a_released_candidate_returns_to_the_pool(api):
    """Rejected by the client and back to `available`: analysed again."""
    api.db["candidates"].update_one({"_id": "exact"}, {"$set": {"recruitment_status": "submitted_to_company"}})
    run_match(api)
    assert "exact" not in by_id(results(api))
    api.db["candidates"].update_one({"_id": "exact"}, {"$set": {"recruitment_status": "available"}})
    run_match(api)
    assert "exact" in by_id(results(api))


def test_a_duplicate_copy_is_not_analysed_twice(api):
    copy = make_record("dup", designation="TIG Welder", summary="DUP second copy of exact.")
    api.db["candidates"].insert_one(copy.to_mongo())
    api.db["candidates"].update_one({"_id": "dup"}, {"$set": {"duplicate_of": "exact", "status": "duplicate"}})
    run_match(api)
    assert not any(call["state"]["candidate"].get("resume_summary", "").startswith("DUP")
                   for call in api.fake.calls)
    assert "exact" in by_id(results(api))
