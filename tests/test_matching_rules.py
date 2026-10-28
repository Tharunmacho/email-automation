"""The rules matching rests on, tested without a database.

    spec        nothing invented; "Any" is no requirement; closed is inactive
    vocabulary  word forms, synonyms and trades meet; typos are absorbed
    evidence    blank is not zero; a band is a range; excerpts are on file
    scoring     a failed mandatory requirement outranks any score
"""
from __future__ import annotations

import pytest

from app.matching import scoring, vocabulary
from app.matching.evidence import band_range, excerpt_for, experience_range, experience_years, history_years
from app.matching.matcher import PoolIndex, build_query, match_one
from app.matching.pool import retrieval_query
from app.matching.scoring import NOT_SATISFIED, SATISFIED, UNKNOWN, Component, Verdict
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
#  Vocabulary
# --------------------------------------------------------------------------- #
def test_word_forms_meet():
    assert vocabulary.terms("Welders welding welder") == ["weld", "weld", "weld"]
    assert vocabulary.normalise("Pipe Fitter") == vocabulary.normalise("pipefitting")
    assert vocabulary.normalise("Fabrication") == vocabulary.normalise("fabricators")


def test_synonyms_are_rewritten_to_one_word():
    assert vocabulary.terms("GTAW") == vocabulary.terms("argon arc") == vocabulary.terms("TIG")
    assert vocabulary.terms("Light Motor Vehicle licence")[0] == "lmv"
    # Rewriting never swallows the word next to it.
    assert vocabulary.terms("TIG Welder") == ["tig", "weld"]


def test_trades_and_their_relatives():
    assert vocabulary.trades_in("Pipe Fabricator who TIG welds spools") == {"fabricator", "welder"}
    assert vocabulary.trades_in("Commis chef") == {"cook"}
    assert vocabulary.trades_in("Electrical Engineer") == {"electrical_engineer"}
    assert "fabricator" in vocabulary.related_to({"welder"})
    assert "welder" in vocabulary.related_to({"fabricator"})  # symmetric


def test_stop_words_carry_no_weight():
    assert vocabulary.terms("Senior Welder required urgently, 3 years experience") == ["weld"]


# --------------------------------------------------------------------------- #
#  Evidence
# --------------------------------------------------------------------------- #
def test_blank_experience_is_unknown_not_zero():
    assert experience_years({"profile": {}}) is None
    assert experience_years({"profile": {"total_experience_years": 0}}) == 0.0
    assert experience_range({"profile": {}}) is None


@pytest.mark.parametrize("band,expected", [
    ("2-5 years", (2.0, 5.0)), ("3_5", (3.0, 5.0)), ("10+ years", (10.0, float("inf"))),
    ("Fresher", (0.0, 0.0)), ("less than 1 year", (0.0, 1.0)), ("", None), ("some", None),
])
def test_a_band_is_a_range_never_a_figure(band, expected):
    assert band_range(band) == expected


def test_dated_history_widens_a_stated_figure_it_contradicts():
    row = {"profile": {"total_experience_years": 1, "work_experience": [
        {"designation": "Welder", "start_date": "2015", "end_date": "2022"}]}}
    assert history_years(row) == 7.0
    low, high, _how = experience_range(row)
    assert (low, high) == (1.0, 7.0)


def test_excerpts_come_from_the_record_and_name_their_field():
    found = excerpt_for(ROW, ["tig"])
    assert found["field"].startswith("profile.") and "TIG" in found["excerpt"]
    assert excerpt_for(ROW, ["underwater basket weaving"]) is None


def test_the_pool_is_everyone_not_yet_submitted():
    query = retrieval_query()
    assert set(query) == {"recruitment_status", "placement_locked", "duplicate_of"}
    assert None in query["recruitment_status"]["$in"] and "available" in query["recruitment_status"]["$in"]


# --------------------------------------------------------------------------- #
#  The matcher on single candidates
# --------------------------------------------------------------------------- #
def ranked(order, *rows):
    index = PoolIndex(list(rows))
    query = build_query(normalize_job_order(order))
    config = scoring.config_for(order)
    return {doc.id: match_one(query, doc, index, config) for doc in index.docs}


def person(cid, designation="", *, skills=(), years=None, summary="", **profile):
    body = {"current_designation": designation, "skills": list(skills), "resume_summary": summary, **profile}
    if years is not None:
        body["total_experience_years"] = years
    return {"id": cid, "profile": body}


def test_the_same_trade_in_other_words_is_credited():
    found = ranked(ORDER, person("fab", "Fabricator", skills=["Argon arc welding", "Blueprint reading"], years=6),
                   person("cook", "Commis Chef", skills=["Tandoor"], years=8))
    assert found["fab"].matched_skills == ["TIG Welding", "Blueprint Reading"]
    assert found["fab"].category == scoring.STRONG
    assert found["cook"].category == scoring.LOW and found["cook"].score < 20


def test_a_misspelling_still_counts():
    order = {**ORDER, "title": "Carpenter", "designation": "Shuttering Carpenter", "skills": ["Shuttering"]}
    found = ranked(order, person("typo", "Carpenter", skills=["Shutering"], years=5),
                   person("other", "Mason", skills=["Plastering"], years=5))
    assert found["typo"].matched_skills == ["Shuttering"]


def test_experience_in_an_unrelated_trade_is_not_counted():
    found = ranked(ORDER, person("cook", "Commis Chef", years=20))
    experience = next(c for c in found["cook"].outcome.components if c.key == "experience")
    assert experience.value == 0.0 and "not counted" in experience.detail


def test_below_the_minimum_is_ineligible_whatever_the_score():
    found = ranked(ORDER, person("short", "TIG Welder", skills=["TIG Welding", "Blueprint reading"], years=1))
    assert found["short"].category == scoring.INELIGIBLE
    assert found["short"].outcome.failed == ["At least 3 years of relevant experience"]


def test_a_band_that_straddles_the_minimum_needs_a_call():
    found = ranked(ORDER, person("band", "TIG Welder", skills=["TIG Welding"], total_experience_band="2-5 years"))
    assert found["band"].category == scoring.REVIEW
    assert found["band"].outcome.unverified == ["At least 3 years of relevant experience"]


def test_a_thin_record_whose_title_fits_goes_to_a_person():
    found = ranked({**ORDER, "minExperience": ""}, person("bot", "Welder"))
    assert found["bot"].category == scoring.REVIEW
    assert "too thin" in found["bot"].outcome.reasons[0]


def test_an_expired_licence_is_not_a_held_one():
    order = {**ORDER, "requiredLicences": ["Gas welding licence"]}
    expired = person("old", "TIG Welder", skills=["TIG Welding"], years=6,
                     licenses=[{"name": "Gas welding licence", "expiry_date": "2020-01-01"}])
    valid = person("new", "TIG Welder", skills=["TIG Welding"], years=6,
                   licenses=[{"name": "Gas welding licence", "expiry_date": "2035-01-01"}])
    found = ranked(order, expired, valid)
    assert found["old"].category == scoring.INELIGIBLE
    assert found["new"].outcome.matched == ["At least 3 years of relevant experience", "Licence: Gas welding licence"]


# --------------------------------------------------------------------------- #
#  Scoring
# --------------------------------------------------------------------------- #
def components(value):
    return [Component("role", 0.3, value), Component("skills", 0.3, value), Component("documents", 0.1, None)]


def test_a_failed_mandatory_requirement_overrides_any_score():
    outcome = scoring.combine(components(1.0), [Verdict("min-experience", "3 years", NOT_SATISFIED)],
                              role=1.0, sparse=False)
    assert outcome.score == 100.0 and outcome.category == scoring.INELIGIBLE
    assert scoring.rank_key(outcome.category, 100) > scoring.rank_key(scoring.LOW, 1)


def test_unverified_sends_a_plausible_candidate_to_a_person_and_leaves_the_rest_low():
    unknown = [Verdict("age-band", "Age 21–45", UNKNOWN)]
    assert scoring.combine(components(0.9), unknown, role=0.9, sparse=False).category == scoring.REVIEW
    assert scoring.combine(components(0.1), unknown, role=0.1, sparse=False).category == scoring.LOW


def test_inapplicable_components_are_reweighted_not_zeroed():
    outcome = scoring.combine(components(0.8), [Verdict("x", "x", SATISFIED)], role=0.8, sparse=False)
    assert outcome.score == 80.0 and outcome.category == scoring.STRONG


def test_weights_are_configurable_per_order_and_change_the_version():
    default = scoring.config_for(ORDER)
    custom = scoring.config_for({**ORDER, "matchingWeights": {"role": 0.9, "nonsense": 4, "skills": "x"}})
    assert custom.weights["role"] == 0.9 and "nonsense" not in custom.weights
    assert custom.weights["skills"] == scoring.DEFAULT_WEIGHTS["skills"]
    assert custom.version != default.version
