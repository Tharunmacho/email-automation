"""The OCR service's recovery log is not a list of problems.

Veris puts both in one `warnings` array. The passport that prompted this split
parsed cleanly — check digits valid — and still arrived carrying five
"warnings", every one of them describing a successful recovery. Read as
warnings they teach an operator to ignore the list, which is the one thing it
must never become.
"""
from __future__ import annotations

import pytest

from app.db.identity_records import _base_document
from app.extraction.ocr_notes import merged, split_service_messages

# Verbatim from a real passport extraction.
RECOVERY = [
    "page 2 was rotated in the scan — auto-corrected 90° before extraction",
    "page 3 was rotated in the scan — auto-corrected 270° before extraction",
    "MRZ recovered from page 2",
    "back-page rescue: 2 LLM call(s), 4 field(s) rescued · 1 page(s) already had >=2 back-page fields",
    "page 1: rescued 2 back-page field(s) via vision LLM",
    "structured complete resume — validated before persistence",
]


@pytest.mark.parametrize("message", RECOVERY)
def test_every_message_from_a_clean_extraction_is_a_note(message):
    notes, warnings = split_service_messages([message])

    assert notes == [message]
    assert warnings == [], "a successful recovery must not be reported as a problem"


def test_a_real_complaint_is_still_a_warning():
    notes, warnings = split_service_messages(
        ["date_of_expiry could not be read", "MRZ recovered from page 2"]
    )

    assert warnings == ["date_of_expiry could not be read"]
    assert notes == ["MRZ recovered from page 2"]


def test_an_unrecognised_message_is_treated_as_a_problem():
    """The lopsided half of the split, and the one worth pinning.

    Veris owns this vocabulary and can extend it without telling us. A recovery
    note shown as a warning is noise; a real complaint filed away as a note is a
    misread document nobody ever looks at. Unknown must fail toward the noise.
    """
    notes, warnings = split_service_messages(["something we have never seen before"])

    assert warnings == ["something we have never seen before"]
    assert notes == []


def test_nothing_at_all_is_two_empty_lists():
    assert split_service_messages(None) == ([], [])
    assert split_service_messages([]) == ([], [])


def test_blanks_and_non_strings_are_dropped():
    """A `None` rendered as "None" in a warning list is its own small lie."""
    notes, warnings = split_service_messages(["  ", None, 7, {"a": 1}, "unreadable page 4"])

    assert warnings == ["unreadable page 4"]
    assert notes == []


def test_the_whole_log_can_still_be_read_back_warnings_first():
    notes, warnings = split_service_messages(["MRZ recovered from page 2", "page 4 unreadable"])

    assert merged(notes, warnings) == ["page 4 unreadable", "MRZ recovered from page 2"]


def test_the_stored_record_separates_them_and_keeps_the_original():
    """The split is a projection. `raw` still holds what the service sent."""
    result = {"warnings": RECOVERY + ["date_of_expiry could not be read"], "passport_number": "Z1"}

    doc = _base_document(
        "rec-1",
        document_type="passport",
        candidate_id="cand-1",
        provider="email",
        account_id="",
        message_id="m1",
        attachment_id="a1",
        filename="bundle.pdf",
        sha256="sha",
        pages=[4],
        ocr_job_id="job-1",
        result=result,
    )

    assert doc["extraction_notes"] == RECOVERY
    assert doc["warnings"] == ["date_of_expiry could not be read"]
    assert doc["raw"]["warnings"] == result["warnings"], "the service's answer, untouched"
