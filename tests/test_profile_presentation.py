"""What reaches the screen is the document, not the extractor's working notes.

Two things the profile was showing that no recruiter asked for:

* the passport's printed fields as a numbered dump of extraction records —
  ``0  Label: Surname Value: ANGURAJ Category: Personal Page: 1 Source:
  label-substring Confidence: 0.75`` — because the service returns them as a
  list and the record stored that list where the screen expected named values;
* a Summary that ran on past its own heading and swallowed the education
  section, repeating as an unreadable paragraph what the Education section
  below it already showed properly.

Both are presentation faults over correct data: `raw` keeps every field, every
source and every confidence for the case where somebody does ask.
"""
from __future__ import annotations

from app.ai.resume_parser import _objective_only
from app.db.identity_records import printed_fields_from


# --------------------------------------------------------------------------- #
#  Passport printed fields
# --------------------------------------------------------------------------- #
VERIS_FIELDS = [
    {"label": "Surname", "value": "ANGURAJ", "category": "Personal", "page": 1,
     "source": "label-substring", "confidence": 0.75},
    {"label": "Given Names", "value": "SARAVANAN", "category": "Personal", "page": 1,
     "source": "label-substring", "confidence": 0.75},
    {"label": "Place of Issue", "value": "MADURAI", "category": "Document", "page": 1,
     "source": "vision-llm", "confidence": 0.95},
]


def test_the_printed_fields_become_named_values():
    assert printed_fields_from(VERIS_FIELDS) == {
        "Surname": "ANGURAJ",
        "Given Names": "SARAVANAN",
        "Place of Issue": "MADURAI",
    }


def test_how_a_field_was_read_is_not_part_of_the_record_on_screen():
    """Category, page, source and confidence answer a question nobody asked."""
    flattened = str(printed_fields_from(VERIS_FIELDS))

    for noise in ("category", "Personal", "confidence", "0.75", "label-substring", "vision-llm"):
        assert noise not in flattened, f"{noise!r} leaked into the displayed fields"


def test_a_field_with_no_value_is_not_shown_as_an_empty_row():
    fields = VERIS_FIELDS + [{"label": "Observations", "value": None, "confidence": 0.4}]

    assert "Observations" not in printed_fields_from(fields)


def test_a_mapping_is_passed_through_unchanged():
    """Older records — and any service that sends a map — still work."""
    assert printed_fields_from({"Place of Issue": "MADURAI"}) == {"Place of Issue": "MADURAI"}


def test_nothing_at_all_is_nothing_to_show():
    assert printed_fields_from(None) is None
    assert printed_fields_from([]) is None
    assert printed_fields_from("unexpected") is None


# --------------------------------------------------------------------------- #
#  The summary
# --------------------------------------------------------------------------- #
RUN_ON = (
    "To work with progressive organization that gives me scope to develop and "
    "update my knowledge and skills and be a part of team that dynamically work "
    "towards growth of organization. EDUCATIONAL QUALIFICATION: QUALIFICATION "
    "INSTITUTION/UNIVERSITY YEAR OF PASSING SSLC ICI GOVT BOYS HR. SEC. SCHOOL, "
    "TENKASI 2006 HSC ICI GOVT BOYS HR. SEC. SCHOOL, TENKASI 2008"
)


def test_the_summary_stops_where_the_next_section_starts():
    summary = _objective_only(RUN_ON)

    assert summary.endswith("growth of organization")
    assert "EDUCATIONAL QUALIFICATION" not in summary
    assert "SSLC" not in summary


def test_an_ordinary_sentence_about_skills_is_not_a_heading():
    """"...develop and update my knowledge and skills..." is prose. Treating the
    bare word as a heading truncated the objective mid-thought."""
    objective = (
        "Seeking a role where I can develop and update my knowledge and skills "
        "in warehouse operations and grow with the organization."
    )

    assert _objective_only(objective).endswith("grow with the organization")


def test_a_generic_word_punctuated_as_a_heading_does_end_it():
    text = (
        "To work as a heavy equipment operator with a progressive organization "
        "on Gulf projects. EXPERIENCE: BHARAT HEAVY LIFTERS 4 YEARS"
    )

    assert _objective_only(text).endswith("on Gulf projects")


def test_a_summary_that_is_only_a_heading_is_left_alone():
    """Truncating to nothing would lose the little it does say."""
    assert _objective_only("EDUCATION: SSLC 2006 HSC 2008") == "EDUCATION: SSLC 2006 HSC 2008"


def test_no_summary_stays_no_summary():
    assert _objective_only(None) is None
    assert _objective_only("   ") is None
