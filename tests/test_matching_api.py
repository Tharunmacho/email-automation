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
         patch.object(settings, "jev_auto_match_active_days", 0), \
         patch.object(settings, "jev_auto_match_edit_delay_seconds", 0), \
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
    # "short" has one year on record against a three-year minimum: the free
    # check marks them ineligible and Jev is not paid to say the same.
    assert run["retrieved"] == 4 and run["scored"] == 3 and run["screened"] == 1
    assert run["failed"] == 0
    assert run["input_tokens"] == 3 * 1500
    assert run["cost_usd"] == pytest.approx(3 * 1500 / 1e6 * 0.042)

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
    assert short["status"] == "screened" and short["score"] is None
    assert "1 years on record" in short["screen"]["reason"]


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


# --------------------------------------------------------------------------- #
#  Preloading: every open order kept current without anybody pressing a button
# --------------------------------------------------------------------------- #
from datetime import datetime, timedelta  # noqa: E402

from app.matching import autorun, runner  # noqa: E402


def add_order(client, **overrides):
    order = {**ORDER, **overrides}
    client.db["job_orders"].insert_one(dict(order))
    return order


def runs_on(client, order_id="JO-7"):
    return list(client.db[store.RUNS].find({"job_order_id": order_id}, {"_id": 0}))


def test_a_sweep_analyses_every_open_order_and_no_closed_one(api):
    add_order(api, id="JO-8", title="Pipe fitter", designation="Pipe Fitter", skills=["Pipe fitting"])
    add_order(api, id="JO-9", status="CLOSED")
    summary = autorun.sweep()
    assert summary["orders"] == 2 and summary["runs"] == 2
    assert len(results(api, "JO-7")["items"]) == 4
    assert len(results(api, "JO-8")["items"]) == 4
    assert runs_on(api, "JO-9") == []
    assert {run["trigger"] for run in runs_on(api, "JO-7") + runs_on(api, "JO-8")} == {"auto"}


def test_a_sweep_with_nothing_new_makes_no_run_and_no_call(api):
    autorun.sweep()
    calls, runs = len(api.fake.calls), len(runs_on(api))
    assert autorun.sweep()["runs"] == 0
    assert len(api.fake.calls) == calls and len(runs_on(api)) == runs


def test_a_sweep_sends_only_the_new_candidate(api):
    autorun.sweep()
    calls = len(api.fake.calls)
    api.db["candidates"].insert_one(make_record(
        "fresh", designation="TIG Welder", summary="FRESH welder, 4 years.", skills=["TIG Welding"], years=4,
    ).to_mongo())
    assert autorun.sweep()["runs"] == 1
    assert len(api.fake.calls) == calls + 1
    run = max(runs_on(api), key=lambda item: item["created_at"])
    assert run["scored"] == 1 and run["cached"] == 4 and run["processed"] == 5
    assert "fresh" in by_id(results(api))


def test_a_sweep_picks_up_a_changed_cv(api):
    autorun.sweep()
    api.db["candidates"].update_one({"_id": "equiv"}, {"$set": {"profile.skills": ["TIG Welding", "MIG"]}})
    calls = len(api.fake.calls)
    autorun.sweep()
    assert len(api.fake.calls) == calls + 1


def test_saving_an_order_matches_it_straight_away(api):
    with patch.object(autorun, "enabled", return_value=True):
        response = api.put("/job-orders/JO-7", json={**ORDER, "skills": ["TIG Welding", "Pipe fitting"]})
    assert response.status_code == 200
    assert [run["trigger"] for run in runs_on(api)] == ["auto"]
    stale = [item for item in results(api)["items"] if item["stale"]["order_changed"]]
    assert len(results(api)["items"]) == 4 and stale == []


def test_saving_a_closed_order_does_not_match_it(api):
    with patch.object(autorun, "enabled", return_value=True):
        api.put("/job-orders/JO-7", json={**ORDER, "status": "CLOSED"})
    assert runs_on(api) == [] and api.fake.calls == []


def test_an_automatic_run_holds_back_a_recent_failure(api):
    api.fake.behaviours["EXACT"] = JevError("Jev returned 529: overloaded", status=529, retryable=True)
    autorun.sweep()
    calls = len(api.fake.calls)
    # Within the hour: nothing else is pending, so no run and no call at all.
    assert autorun.sweep()["runs"] == 0 and len(api.fake.calls) == calls
    state = api.get("/job-orders/JO-7/matching").json()
    assert state["coverage"]["failed"] == 1 and state["coverage"]["not_evaluated"] == 0

    # After the hold-back window, the sweep tries again by itself.
    api.fake.behaviours["EXACT"] = {}
    with patch.object(runner, "AUTO_RETRY_FAILED_AFTER", timedelta(0)):
        assert autorun.sweep()["runs"] == 1
    assert by_id(results(api))["exact"]["status"] == "scored"


def test_a_non_retryable_failure_is_not_resent_automatically_but_a_recruiter_can(api):
    api.fake.behaviours["EXACT"] = JevError("Jev returned 400: bad request", status=400, retryable=False)
    autorun.sweep()
    calls = len(api.fake.calls)
    with patch.object(runner, "AUTO_RETRY_FAILED_AFTER", timedelta(0)):
        autorun.sweep()
    assert len(api.fake.calls) == calls

    api.fake.behaviours["EXACT"] = {}
    run = run_match(api).json()
    stored = api.get(f"/job-orders/JO-7/matching/runs/{run['id']}").json()
    assert stored["scored"] == 1 and stored["deferred"] == 0
    assert by_id(results(api))["exact"]["status"] == "scored"


def test_opening_the_panel_runs_only_what_is_not_current(api):
    first = api.post("/job-orders/JO-7/matching/preload")
    assert first.status_code == 200
    body = first.json()
    assert body["pending"] == 4 and body["run"]["trigger"] == "open"
    assert body["run"]["created_by"]["id"] == ADMIN["id"]

    calls = len(api.fake.calls)
    again = api.post("/job-orders/JO-7/matching/preload").json()
    assert again == {"pending": 0, "run": None}
    assert len(api.fake.calls) == calls and len(runs_on(api)) == 1


def test_opening_a_closed_order_starts_nothing(api):
    api.db["job_orders"].update_one({"id": "JO-7"}, {"$set": {"status": "FILLED"}})
    assert api.post("/job-orders/JO-7/matching/preload").json() == {"pending": 0, "run": None}
    assert runs_on(api) == []


def test_opening_the_panel_during_a_run_returns_that_run(api):
    with patch("app.matching.runner._spawn"):  # the first run never starts executing
        started = run_match(api).json()
    response = api.post("/job-orders/JO-7/matching/preload")
    assert response.status_code == 200
    assert response.json()["run"]["id"] == started["id"]
    assert "active_lock" not in response.json()["run"]


def test_opening_the_panel_without_a_key_is_unconfigured(api):
    with patch.object(settings, "jev_api_key", ""):
        assert api.post("/job-orders/JO-7/matching/preload").status_code == 503


def test_the_latest_run_is_readable_for_a_screen_left_open(api):
    assert api.get("/job-orders/JO-7/matching/latest-run").json() == {"run": None}
    autorun.sweep()
    latest = api.get("/job-orders/JO-7/matching/latest-run").json()["run"]
    assert latest["status"] == "completed" and latest["trigger"] == "auto"
    assert "active_lock" not in latest
    assert api.get("/job-orders/JO-404/matching/latest-run").status_code == 404


def test_automatic_matching_never_starts_inside_the_test_suite(api):
    assert autorun.enabled() is False
    api.put("/job-orders/JO-7", json={**ORDER, "skills": ["Pipe fitting"]})
    assert runs_on(api) == [] and api.fake.calls == []


def test_a_repeat_run_writes_nothing_for_current_results(api):
    run_match(api)
    before = {doc["candidate_id"]: doc["updated_at"]
              for doc in api.db[store.MATCHES].find({"job_order_id": "JO-7"})}
    run_match(api)
    after = {doc["candidate_id"]: doc["updated_at"]
             for doc in api.db[store.MATCHES].find({"job_order_id": "JO-7"})}
    assert after == before


# --------------------------------------------------------------------------- #
#  Cost: the free check, smaller requests, sweeping only orders in use
# --------------------------------------------------------------------------- #
from app.matching import questions as question_schema  # noqa: E402
from app.matching.spec import normalize_job_order  # noqa: E402

COOK = make_record("cook", designation="Commis Chef", summary="COOK tandoori and grill, 8 years.",
                   skills=["Tandoor", "Food hygiene"], years=8)


def sent_summaries(client):
    return [call["state"]["candidate"].get("resume_summary", "") for call in client.fake.calls]


def test_a_clearly_different_trade_is_not_sent_to_jev(api):
    api.db["candidates"].insert_one(COOK.to_mongo())
    run = run_match(api).json()
    stored = api.get(f"/job-orders/JO-7/matching/runs/{run['id']}").json()
    assert stored["screened"] == 2  # the cook, and "short" below the minimum
    assert not any(summary.startswith("COOK") for summary in sent_summaries(api))

    cook = by_id(results(api))["cook"]
    assert cook["status"] == "screened" and cook["category"] == "low_match"
    assert "different trade from Welder" in cook["screen"]["reason"]
    coverage = api.get("/job-orders/JO-7/matching").json()["coverage"]
    assert coverage["screened"] == 2 and coverage["not_evaluated"] == 0


def test_a_related_trade_is_still_sent_to_jev(api):
    api.db["candidates"].insert_one(NO_KEYWORDS.to_mongo())  # a boilermaker
    run_match(api)
    assert any(summary.startswith("NOKW") for summary in sent_summaries(api))
    assert by_id(results(api))["nokw"]["status"] == "scored"


def test_check_with_jev_sends_the_skipped_candidates(api):
    api.db["candidates"].insert_one(COOK.to_mongo())
    run_match(api)
    calls = len(api.fake.calls)
    run = run_match(api, screen=False).json()
    stored = api.get(f"/job-orders/JO-7/matching/runs/{run['id']}").json()
    assert stored["scored"] == 2 and stored["screened"] == 0 and stored["cached"] == 3
    assert len(api.fake.calls) == calls + 2
    assert by_id(results(api))["cook"]["status"] == "scored"

    # And a later automatic pass leaves Jev's answer in place.
    calls = len(api.fake.calls)
    assert autorun.sweep()["runs"] == 0 and len(api.fake.calls) == calls


def test_re_checking_one_skipped_candidate_asks_jev(api):
    run_match(api)
    calls = len(api.fake.calls)
    response = api.post("/job-orders/JO-7/matching/results/short/refresh", params={"force": "true"})
    assert response.status_code == 202
    assert len(api.fake.calls) == calls + 1
    assert by_id(results(api))["short"]["status"] == "scored"


def test_unused_questions_are_not_sent():
    spec = normalize_job_order(ORDER)  # skills, minimum and destination; no salary or licence
    asked = set(question_schema.build_questions(spec))
    assert {"salary_fit", "equivalent_title", "qualifications_match"}.isdisjoint(asked)
    assert {"role_relevance", "skills_match", "experience_match", "destination_fit",
            "availability_fit", "document_readiness"} <= asked

    bare = normalize_job_order({"id": "JO-1", "title": "Helper", "status": "OPEN"})
    asked = set(question_schema.build_questions(bare))
    assert {"skills_match", "destination_fit", "availability_fit"}.isdisjoint(asked)

    licensed = normalize_job_order({**ORDER, "requiredLicences": ["MOM welder"]})
    assert "qualifications_match" in question_schema.build_questions(licensed)


def test_empty_order_fields_are_not_sent(api):
    run_match(api)
    order_state = api.fake.calls[0]["state"]["job_order"]
    assert "salary_offered" not in order_state and "required_licences" not in order_state
    assert order_state["required_skills"] == ["TIG Welding"]


def test_results_do_not_expire_by_default(api):
    run_match(api)
    api.db[store.MATCHES].update_many({}, {"$set": {"evaluated_at": datetime(2020, 1, 1)}})
    calls = len(api.fake.calls)
    run_match(api)
    assert len(api.fake.calls) == calls


def test_the_sweep_skips_orders_nobody_has_opened(api):
    add_order(api, id="JO-8", title="Pipe fitter", designation="Pipe Fitter", skills=["Pipe fitting"])
    with patch.object(settings, "jev_auto_match_active_days", 7):
        assert autorun.sweep()["orders"] == 0
        api.post("/job-orders/JO-8/matching/preload")  # somebody opens JO-8
        calls = len(api.fake.calls)
        summary = autorun.sweep()
    assert summary["orders"] == 1 and summary["runs"] == 0  # JO-8 was matched as it opened
    assert len(api.fake.calls) == calls and runs_on(api, "JO-7") == []


def test_edits_settle_before_an_order_is_rematched(api):
    with patch.object(autorun, "enabled", return_value=True), \
         patch.object(settings, "jev_auto_match_edit_delay_seconds", 600):
        for skills in (["TIG Welding"], ["TIG Welding", "Pipe fitting"], ["Pipe fitting"]):
            api.put("/job-orders/JO-7", json={**ORDER, "skills": skills})
        assert runs_on(api) == []  # nothing yet: still inside the settle window
        assert autorun.sweep()["orders"] == 0  # and the sweep leaves it alone too
        assert list(autorun._pending_saves) == ["JO-7"]  # one pending run, not three
        timer = autorun._pending_saves["JO-7"]
        timer.cancel()
        timer.function()  # the window closes
    assert len(runs_on(api)) == 1
