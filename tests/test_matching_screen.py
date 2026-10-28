"""The free check before Jev: it may only skip somebody Jev could not shortlist."""
from __future__ import annotations

from app.matching import screen
from app.matching.prefilter import baseline
from app.matching.spec import normalize_job_order

WELDER_ORDER = normalize_job_order({
    "id": "JO-1", "title": "Welder", "designation": "TIG Welder", "status": "OPEN",
    "skills": ["TIG Welding"], "minExperience": "3 years",
})


def row(designation="", *, summary="", skills=(), years=None, band=None, history=(), **extra):
    profile = {"current_designation": designation, "resume_summary": summary,
               "skills": list(skills), "work_experience": list(history), **extra}
    if years is not None:
        profile["total_experience_years"] = years
    if band is not None:
        profile["total_experience_band"] = band
    return {"id": designation or "x", "profile": profile}


def verdict(record, spec=WELDER_ORDER):
    return screen.screen(spec, record, baseline(spec, record))


def test_a_cook_is_skipped_for_a_welder_order():
    found = verdict(row("Commis Chef", summary="Tandoori and grill.", years=8))
    assert found.rule == screen.DIFFERENT_TRADE and found.category == "low_match"


def test_a_related_trade_is_sent():
    assert verdict(row("Boilermaker", summary="Joins stainless plate.", years=7)) is None


def test_any_mention_of_the_trade_anywhere_is_sent():
    record = row("Commis Chef", summary="Before cooking I worked two years as a welding helper.", years=8)
    assert verdict(record) is None


def test_a_record_without_a_recognisable_title_is_sent():
    assert verdict(row("", summary="Hard worker, ready to travel.", years=8)) is None
    assert verdict(row("Operative", years=8)) is None


def test_a_generic_helper_is_sent():
    assert verdict(row("Helper", years=8)) is None


def test_an_order_the_table_cannot_place_screens_nobody_on_trade():
    spec = normalize_job_order({"id": "JO-2", "title": "Ship chandler", "status": "OPEN"})
    assert verdict(row("Commis Chef", years=8), spec) is None


def test_experience_below_the_minimum_is_ineligible():
    found = verdict(row("TIG Welder", skills=["TIG Welding"], years=1))
    assert found.rule == screen.BELOW_EXPERIENCE and found.category == "ineligible"
    assert found.failed_requirement == "At least 3 years of relevant experience"


def test_dated_work_history_that_shows_more_years_is_sent():
    history = [{"designation": "Welder", "start_date": "2015", "end_date": "2022"}]
    assert verdict(row("TIG Welder", skills=["TIG Welding"], years=1, history=history)) is None


def test_an_experience_band_is_read_by_its_top():
    assert verdict(row("TIG Welder", skills=["TIG Welding"], band="0-1 years")).rule == screen.BELOW_EXPERIENCE
    assert verdict(row("TIG Welder", skills=["TIG Welding"], band="2-5 years")) is None  # might meet it
    assert verdict(row("TIG Welder", skills=["TIG Welding"], band="10+ years")) is None


def test_unknown_experience_is_never_a_reason_to_skip():
    assert verdict(row("TIG Welder", skills=["TIG Welding"])) is None


def test_stemming_meets_the_forms_a_cv_uses():
    assert screen.trades_in("Welders, welding and a welder") == {"welder"}
    assert screen.trades_in("Pipe fitting and pipefitters") == {"pipe_fitter"}
    assert screen.trades_in("Electrical Engineer") == {"electrical_engineer"}
