"""Duplicate OCR fields and other people's details cannot decide citizenship."""
import pytest

from app.extraction import passport_nationality as pn
from app.extraction import resume_nationality as rn


@pytest.mark.parametrize("repetitions", [1, 2, 10])
def test_repeated_conflicting_fields_do_not_reject_an_indian_candidate(repetitions):
    text = "Nationality: Indian\n" + "Nationality: Pakistani\n" * repetitions
    text += "Place of birth: Lahore\nAddress: Karachi\nMobile: +92 300 1234567\n"

    verdict = rn.detect_resume_nationality(text)

    assert verdict.verdict == rn.UNDETERMINED
    assert rn.should_ingest(verdict)[0] is True


def test_repeated_foreign_fields_do_not_overrule_an_indian_passport():
    indian = pn.NationalityVerdict(verdict=pn.INDIAN, country_code="IND", confidence=0.99)

    verdict = rn.detect_resume_nationality(
        "Nationality: Pakistani\n" * 10, passport_verdicts={2: indian},
    )

    assert verdict.verdict != rn.FOREIGN
    assert rn.should_ingest(verdict)[0] is True


@pytest.mark.parametrize("owner", ["Father", "Mother's", "Spouse", "Employer", "Sponsor"])
def test_someone_elses_nationality_is_not_the_candidates(owner):
    verdict = rn.detect_resume_nationality(f"{owner} nationality: Pakistani\n")

    assert verdict.verdict == rn.UNDETERMINED
    assert rn.should_ingest(verdict)[0] is True


def test_explicit_foreign_candidate_is_still_rejected_despite_indian_family():
    verdict = rn.detect_resume_nationality(
        "Nationality: Pakistani\nFather's nationality: Indian\nMother nationality: Indian\n"
    )

    assert verdict.verdict == rn.FOREIGN
    assert rn.should_ingest(verdict)[0] is False


def test_agreeing_duplicate_fields_still_establish_foreign_nationality():
    verdict = rn.detect_resume_nationality("Nationality: Pakistani\n" * 10)

    assert verdict.verdict == rn.FOREIGN
    assert rn.should_ingest(verdict)[0] is False


@pytest.mark.parametrize("repetitions", [1, 2, 10])
def test_repeated_overseas_birthplace_does_not_overrule_indian_nationality(repetitions):
    text = "Nationality: Indian\n" + "Place of Birth: Lahore;\n" * repetitions
    text += "Address: Karachi\nMobile: +92 300 1234567\n"

    verdict = rn.detect_resume_nationality(text)

    assert verdict.verdict == rn.INDIAN
    assert rn.should_ingest(verdict)[0] is True


def test_mrz_only_indian_passport_outranks_foreign_statement_and_location_hints():
    indian = pn.detect_passport_country("P<INDKUMAR<<RAJESH<<<<<<<<<<<<<<<<<<<<<<<<<<")
    text = (
        "Nationality: Pakistani\nPlace of Birth: Lahore;\n"
        "Address: Karachi\nMobile: +92 300 1234567\n"
    )

    verdict = rn.detect_resume_nationality(text, passport_verdicts={2: indian})

    assert verdict.scores["PAK"] > verdict.scores["IND"]
    assert verdict.verdict == rn.INDIAN
    assert rn.should_ingest(verdict)[0] is True


def test_one_clear_foreign_passport_keeps_its_authority_over_indian_cv_hints():
    foreign = pn.detect_passport_country("P<PAKKHAN<<AHMED<<<<<<<<<<<<<<<<<<<<<<<<<<")
    text = "Nationality: Indian\nPlace of Birth: Chennai;\nMobile: +91 98765 43210\n"

    verdict = rn.detect_resume_nationality(text, passport_verdicts={2: foreign})

    assert verdict.verdict == rn.FOREIGN
    assert verdict.country_code == "PAK"
    assert rn.should_ingest(verdict)[0] is False


@pytest.mark.parametrize("repetitions", [1, 3])
def test_conflicting_passports_cannot_be_decided_by_number_of_pages(repetitions):
    indian = pn.detect_passport_country("P<INDKUMAR<<RAJESH<<<<<<<<<<<<<<<<<<<<<<<<<<")
    foreign = pn.detect_passport_country("P<PAKKHAN<<AHMED<<<<<<<<<<<<<<<<<<<<<<<<<<")
    passports = {1: indian, **{n + 2: foreign for n in range(repetitions)}}

    verdict = rn.detect_resume_nationality(
        "Nationality: Pakistani\nPlace of Birth: Lahore;\nMobile: +92 300 1234567\n",
        passport_verdicts=passports,
    )

    assert verdict.verdict == rn.UNDETERMINED
    assert "conflicting passport" in " ".join(verdict.evidence)
    assert rn.should_ingest(verdict)[0] is True


@pytest.mark.parametrize("wrapper", ["({})", "[{}]", '"{}"', "'{}'", "“{}”", "- {}"])
@pytest.mark.parametrize("value, code", [("Pakistani", "PAK"), ("Indonesian", "IDN"), ("Indian", "IND")])
def test_wrapped_nationality_value_still_establishes_its_country(wrapper, value, code):
    verdict = rn.detect_resume_nationality("Nationality: " + wrapper.format(value))

    assert verdict.country_code == code
    assert rn.should_ingest(verdict)[0] is (code == "IND")


@pytest.mark.parametrize(
    "value", ["(Employer: Pakistani Trading)", "[Passport issued in Nepal]", '"Unreadable" | Pakistani employer'],
)
def test_removing_wrappers_does_not_search_past_unrelated_field_text(value):
    verdict = rn.detect_resume_nationality("Nationality: " + value)

    assert verdict.verdict == rn.UNDETERMINED
    assert rn.should_ingest(verdict)[0] is True
