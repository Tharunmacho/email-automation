"""The paragraph that describes the candidate, under whatever heading they used.

Veris returns no `summary` field of its own — the response carries name,
contact, education, experience, skills and counters, and nothing else. So the
profile summary comes entirely from the résumé's own section headings, and a
heading the section reader does not know is a summary silently thrown away
while everything beneath it is kept.
"""
from __future__ import annotations

import pytest

from app.ai.resume_parser import extract_sections

OBJECTIVE_CV = """RAMESH KUMAR
CAREER OBJECTIVE
To work as a heavy equipment operator with a progressive organization.

EDUCATION
SSLC, Govt Boys Hr Sec School, 2006
"""

# Verbatim shape of the CV whose summary was dropped.
PROFILE_CV = """CURRICULUM VITAE

Syed Talha

PROFILE
Experienced as a Spray painter with 8 years in the industry. Skilled in
painting interior and exterior surfaces of buildings and structures.

CONTACT
Nationality : Pakistani
"""


@pytest.mark.parametrize(
    "heading",
    ["OBJECTIVE", "CAREER OBJECTIVE", "SUMMARY", "PROFESSIONAL SUMMARY",
     "PROFILE", "PROFESSIONAL PROFILE", "CAREER PROFILE", "OVERVIEW"],
)
def test_the_opening_paragraph_is_found_under_any_of_its_usual_headings(heading):
    cv = f"NAME HERE\n\n{heading}\nA competent tradesman with eight years on site.\n\nEDUCATION\nSSLC 2006\n"

    assert extract_sections(cv).get("objective") == [
        "A competent tradesman with eight years on site."
    ], heading


def test_the_profile_heading_that_was_being_dropped():
    """`PROFILE` was not in the vocabulary, so this résumé stored no summary at
    all — while its education and skills were read normally from the same
    text."""
    assert extract_sections(PROFILE_CV).get("objective") == [
        "Experienced as a Spray painter with 8 years in the industry. Skilled in",
        "painting interior and exterior surfaces of buildings and structures.",
    ]


def test_an_objective_heading_still_works():
    """The headings that already worked must keep working."""
    assert extract_sections(OBJECTIVE_CV).get("objective") == [
        "To work as a heavy equipment operator with a progressive organization."
    ]


def test_personal_details_are_not_swallowed_by_the_new_headings():
    """"Personal profile" names a block of fields, not a paragraph, and stays
    where it was — otherwise the new "profile" entry would capture it."""
    cv = "NAME\n\nPERSONAL PROFILE\nFather Name : X\nDate of Birth : 02-01-2000\n"
    sections = extract_sections(cv)

    assert sections.get("personal")
    assert not sections.get("objective")
