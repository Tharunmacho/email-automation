"""The rules matching rests on, tested without a database or a network.

    spec       nothing invented; "Any" is no requirement; closed is inactive
    evidence   identifiers never leave; blank is not zero; excerpts are on file
    questions  "not stated" is not a low mark; a hesitant yes is unknown
    scoring    a failed mandatory requirement outranks any score
"""
from __future__ import annotations

import pytest

from app.matching import scoring
from app.matching.evidence import (
    FORBIDDEN_FIELDS,
    assert_no_identifiers,
    candidate_state,
    evidence_fingerprint,
    excerpt_for,
    experience_years,
)
from app.matching.prefilter import baseline, retrieval_query
from app.matching.questions import (
    NOT_SATISFIED,
    REQUIREMENT_PREFIX,
    SATISFIED,
    UNKNOWN,
    build_questions,
    build_state,
    read_answers,
)
from app.matching.spec import MANDATORY, PREFERRED, normalize_job_order, spec_fingerprint

ORDER = {
    "id": "JO-1", "title": "Welder", "designation": "TIG Welder", "client": "Keppel",
    "destinationCountry": "Singapore", "status": "OPEN", "headcount": 4,
    "skills": ["TIG Welding", "Blueprint Reading"], "salary": "", "dueDate": "2026-12-01",
    "minExperience": "3 years", "industry": "Fabrication",
}

ROW = {
    "id": "cand-1",
    "candidate_code": "C-1",
    "profile": {
        "full_name": "Arjun Kumar",
        "email": "arjun@example.com",
        "phone": "+919876543210",
        "phone_numbers": ["+919876543210"],
        "passport_number": "Z1234567",
        "passport_expiry": "2030-04-01",
        "linkedin_url": "https://linkedin.com/in/arjun",
        "current_designation": "Fabricator",
        "skills": ["TIG Welding", "MIG"],
        "trade_skills": ["Blueprint reading"],
        "total_experience_years": 6,
        "licenses": [{"name": "Gas welding licence", "number": "GW-998", "issuer": "TN Boiler Board"}],
        "work_experience": [{"designation": "Fabricator", "company": "L&T",
                             "description": "TIG welding of stainless pipe spools"}],
        "destination_country": "Singapore",
        "resume_summary": "Fabricator with six years of TIG welding. Reads blueprints.",
    },
    "resume_available": True,
}


# --------------------------------------------------------------------------- #
#  Spec
# --------------------------------------------------------------------------- #
def test_only_stated_requirements_exist():
    spec = normalize_job_order(ORDER)
    mandatory = [item.key for item in spec.mandatory_requirements]
    assert mandatory == ["min-experience"]
    assert spec.min_experience_years == 3
    # Skills are scored, never a gate: a CV that words them differently must
    # not be thrown out on vocabulary.
    assert {item.kind for item in spec.requirements if item.source == "skills"} == {PREFERRED}
    assert spec.required_licences == [] and spec.required_certifications == []


@pytest.mark.parametrize("text", ["Any", "", None, "Fresher", "0", "n/a"])
def test_no_experience_floor_means_no_experience_requirement(text):
    spec = normalize_job_order({**ORDER, "minExperience": text})
    assert spec.min_experience_years is None
    assert not any(item.key == "min-experience" for item in spec.requirements)


def test_explicit_licences_and_eligibility_are_mandatory():
    spec = normalize_job_order({**ORDER, "requiredLicences": ["Gulf welding licence"],
                                "workEligibility": "Singapore work permit"})
    kinds = {item.key: item.kind for item in spec.requirements}
    assert kinds["licence:gulf welding licence"] == MANDATORY
    assert kinds["work-eligibility"] == MANDATORY


def test_an_age_band_is_a_requirement_the_record_cannot_answer():
    spec = normalize_job_order({**ORDER, "minAge": 21, "maxAge": 45})
    age = next(item for item in spec.requirements if item.key == "age-band")
    assert age.kind == MANDATORY


@pytest.mark.parametrize("status,active", [("OPEN", True), ("IN PROGRESS", True),
                                           ("FILLED", False), ("CLOSED", False)])
def test_only_open_orders_are_active(status, active):
    assert normalize_job_order({**ORDER, "status": status}).is_active is active


def test_the_fingerprint_follows_requirements_not_the_client_name():
    base = spec_fingerprint(normalize_job_order(ORDER))
    assert spec_fingerprint(normalize_job_order({**ORDER, "client": "Someone else"})) == base
    assert spec_fingerprint(normalize_job_order({**ORDER, "skills": ["TIG Welding"]})) != base
    assert spec_fingerprint(normalize_job_order({**ORDER, "minExperience": "5"})) != base


# --------------------------------------------------------------------------- #
#  Evidence
# --------------------------------------------------------------------------- #
def test_no_identifier_reaches_the_state():
    state = candidate_state(ROW)
    flat = repr(state)
    for value in ("Z1234567", "+919876543210", "arjun@example.com", "linkedin", "GW-998", "Arjun Kumar"):
        assert value not in flat
    assert_no_identifiers(state)
    # Readiness survives without the number.
    assert state["documents"]["passport_on_file"] is True
    assert state["documents"]["passport_expiry"] == "2030-04-01"


def test_the_guard_catches_an_identifier_anywhere():
    for field_name in ("passport_number", "phone", "email"):
        assert field_name in FORBIDDEN_FIELDS
        with pytest.raises(ValueError):
            assert_no_identifiers({"candidate": {"nested": [{field_name: "x"}]}})


def test_blank_experience_is_unknown_not_zero():
    assert experience_years({"profile": {}}) is None
    assert experience_years({"profile": {"total_experience_years": 0}}) == 0.0
    state = candidate_state({"profile": {"total_experience_band": "2-5 years"}})
    assert "total_experience_years" not in state
    assert state["total_experience_band"] == "2-5 years"


def test_excerpts_come_from_the_record_and_name_their_field():
    found = excerpt_for(ROW, ["blueprint"])
    assert found["field"] == "profile.trade_skills"
    assert "Blueprint reading" in found["excerpt"]
    assert excerpt_for(ROW, ["crane operation"]) is None


def test_the_evidence_fingerprint_ignores_contact_details():
    before = evidence_fingerprint(ROW)
    moved = {**ROW, "profile": {**ROW["profile"], "phone": "+910000000000"}}
    assert evidence_fingerprint(moved) == before
    upskilled = {**ROW, "profile": {**ROW["profile"], "skills": ["TIG Welding", "MIG", "Rigging"]}}
    assert evidence_fingerprint(upskilled) != before


# --------------------------------------------------------------------------- #
#  Questions and answers
# --------------------------------------------------------------------------- #
def _score(level: int, confidence: float = 0.9, levels: int = 5) -> dict:
    return {"type": "score", "score": float(level), "confidence": confidence,
            "legend": {str(i): f"level {i}" for i in range(levels)},
            "probabilities": {str(i): (1.0 if i == level else 0.0) for i in range(levels)}}


def _choice(option: str, confidence: float = 0.95) -> dict:
    return {"type": "choice", "choice": option, "confidence": confidence,
            "probabilities": {option: confidence}}


def test_questions_respect_documented_limits():
    spec = normalize_job_order({**ORDER, "requiredLicences": ["A", "B"]})
    questions = build_questions(spec)
    for question in questions.values():
        assert question["type"] in {"score", "choice", "noul"}
        if question["type"] == "score":
            assert 1 <= len(question["criteria"]) <= 10
        if question["type"] == "choice":
            assert 1 <= len(question["criteria"]) <= 255
    requirement_questions = [key for key in questions if key.startswith(REQUIREMENT_PREFIX)]
    assert len(requirement_questions) == 3  # experience + two licences
    for key in requirement_questions:
        assert set(questions[key]["criteria"]) == {SATISFIED, NOT_SATISFIED, UNKNOWN}


def test_the_state_carries_both_sides_and_no_identifiers():
    state = build_state(normalize_job_order(ORDER), candidate_state(ROW))
    assert state["job_order"]["required_skills"] == ["TIG Welding", "Blueprint Reading"]
    assert_no_identifiers(state)


def test_not_stated_is_not_a_low_mark():
    spec = normalize_job_order(ORDER)
    evaluation = read_answers({"skills_match": _score(0), "experience_match": _score(1)}, spec)
    assert evaluation.dimensions["skills_match"].stated is False
    # Level 1 ("none of the skills") is the bottom of the *stated* scale.
    assert evaluation.dimensions["experience_match"].value == 0.0
    assert evaluation.dimensions["experience_match"].stated is True
    assert read_answers({"skills_match": _score(4)}, spec).dimensions["skills_match"].value == 1.0


def test_a_hesitant_yes_on_a_mandatory_requirement_is_unknown():
    spec = normalize_job_order(ORDER)
    sure = read_answers({f"{REQUIREMENT_PREFIX}0": _choice(SATISFIED, 0.95)}, spec)
    unsure = read_answers({f"{REQUIREMENT_PREFIX}0": _choice(SATISFIED, 0.4)}, spec)
    assert sure.verdicts[0].status == SATISFIED
    assert unsure.verdicts[0].status == UNKNOWN


def test_a_missing_or_malformed_answer_is_unknown_never_a_pass():
    spec = normalize_job_order(ORDER)
    assert read_answers({}, spec).verdicts[0].status == UNKNOWN
    wrong_type = {f"{REQUIREMENT_PREFIX}0": {"type": "noul", "noul": 1.0}, "skills_match": _choice("x")}
    evaluation = read_answers(wrong_type, spec)
    assert evaluation.verdicts[0].status == UNKNOWN
    assert "skills_match" not in evaluation.dimensions
    invented = {f"{REQUIREMENT_PREFIX}0": _choice("probably", 0.99)}
    assert read_answers(invented, spec).verdicts[0].status == UNKNOWN


# --------------------------------------------------------------------------- #
#  Scoring
# --------------------------------------------------------------------------- #
def _answers(level=4, requirement=SATISFIED, **overrides):
    answers = {key: _score(level) for key in (
        "role_relevance", "skills_match", "experience_match", "qualifications_match",
        "document_readiness", "evidence_quality")}
    answers.update({"salary_fit": _choice("not_stated"), "destination_fit": _choice("matches"),
                    "availability_fit": _choice("available"),
                    f"{REQUIREMENT_PREFIX}0": _choice(requirement)})
    answers.update(overrides)
    return answers


def test_a_failed_mandatory_requirement_overrides_any_score():
    spec = normalize_job_order(ORDER)
    outcome = scoring.score(spec, read_answers(_answers(4, NOT_SATISFIED), spec))
    assert outcome.score >= 90
    assert outcome.category == scoring.INELIGIBLE
    # Evidence quality kept high so this is a weak match, not a thin record.
    weak = scoring.score(spec, read_answers(_answers(1, evidence_quality=_score(4)), spec))
    assert weak.category == scoring.LOW
    # Ranked below a low match whatever the numbers say.
    assert scoring.rank_key(outcome.category, outcome.score) > scoring.rank_key(weak.category, weak.score)


def test_strong_skills_with_insufficient_mandatory_experience_is_ineligible():
    spec = normalize_job_order(ORDER)
    answers = _answers(4, NOT_SATISFIED, experience_match=_score(1))
    outcome = scoring.score(spec, read_answers(answers, spec))
    assert outcome.category == scoring.INELIGIBLE
    assert outcome.failed == ["At least 3 years of relevant experience"]


def test_an_unverified_licence_goes_to_manual_review():
    spec = normalize_job_order({**ORDER, "minExperience": "Any", "requiredLicences": ["Gulf licence"]})
    outcome = scoring.score(spec, read_answers(_answers(4, UNKNOWN), spec))
    assert outcome.category == scoring.REVIEW
    assert outcome.unverified == ["Licence: Gulf licence"]


def test_a_sparse_record_goes_to_manual_review():
    spec = normalize_job_order(ORDER)
    outcome = scoring.score(spec, read_answers(_answers(4, evidence_quality=_score(1)), spec))
    assert outcome.category == scoring.REVIEW


def test_a_self_contradicting_record_goes_to_manual_review():
    spec = normalize_job_order(ORDER)
    answers = _answers(4, conflicting_evidence={"type": "noul", "noul": 0.8})
    assert scoring.score(spec, read_answers(answers, spec)).category == scoring.REVIEW


def test_salary_only_counts_when_the_order_states_one():
    no_salary = normalize_job_order(ORDER)
    with_salary = normalize_job_order({**ORDER, "salary": "SGD 2,000"})
    compatible = scoring.score(with_salary, read_answers(_answers(4, salary_fit=_choice("compatible")), with_salary))
    incompatible = scoring.score(with_salary, read_answers(_answers(4, salary_fit=_choice("incompatible")), with_salary))
    unstated = scoring.score(no_salary, read_answers(_answers(4), no_salary))
    salary = next(c for c in unstated.components if c.key == "salary")
    assert salary.value is None  # not applicable, not zero
    assert compatible.score == 100.0 and unstated.score == 100.0
    # Qualifications is not applicable (the order names none), so the applicable
    # weights sum to 0.85 and losing salary's 0.10 leaves 0.75 / 0.85.
    assert incompatible.score == pytest.approx(round(100 * 0.75 / 0.85, 1))


def test_an_applicable_dimension_with_no_evidence_scores_zero_and_says_so():
    spec = normalize_job_order(ORDER)
    outcome = scoring.score(spec, read_answers(_answers(4, document_readiness=_score(0)), spec))
    documents = next(c for c in outcome.components if c.key == "documents")
    assert documents.unknown and documents.value == 0.0
    assert "No evidence on file for documents" in outcome.reasons


def test_weights_are_configurable_per_order_and_change_the_version():
    default = scoring.config_for(ORDER)
    custom = scoring.config_for({**ORDER, "matchingWeights": {"skills": 0.6, "nonsense": 9, "experience": "x"}})
    assert custom.weights["skills"] == 0.6
    assert "nonsense" not in custom.weights
    assert custom.weights["experience"] == scoring.DEFAULT_WEIGHTS["experience"]
    assert custom.version != default.version


# --------------------------------------------------------------------------- #
#  Prefilter
# --------------------------------------------------------------------------- #
def test_the_baseline_credits_equivalent_terminology_without_rejecting_it():
    spec = normalize_job_order(ORDER)
    result = baseline(spec, ROW)
    assert result.role_hit  # "Welder" appears in "TIG welding" history
    assert result.skills_found == ["TIG Welding", "Blueprint Reading"]
    assert result.experience_met is True
    stranger = baseline(spec, {"id": "x", "profile": {"current_designation": "Accountant"}})
    assert stranger.score < result.score
    assert stranger.score >= 0  # ranked low, not excluded


def test_retrieval_excludes_only_by_explicit_rule():
    query = retrieval_query(normalize_job_order(ORDER))
    assert query["placement_locked"] == {"$ne": True}
    assert query["profile.is_resume"] == {"$ne": False}
    assert "profile.current_designation" not in repr(query)
