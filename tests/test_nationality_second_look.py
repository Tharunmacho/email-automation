"""Two layers, because the first one reads a worse copy of the same résumé.

The local read works off the PDF's own text layer. A two-column CV hides
`Nationality : Pakistani` across three lines there, so the stated-nationality
rule matches nothing and the verdict is UNDETERMINED — which is accepted, and
deliberately so: most Indian CVs never state a nationality either, and refusing
every silent one loses far more placements than it saves.

By the time the résumé service has answered there is better evidence: its own
structured reading of the nationality field, a place of birth, a passport place
of issue, and page text with the columns pulled apart. This is that second look.
"""
from __future__ import annotations

from app.ai.resume_parser import _reconsider_nationality
from app.core.models import ExtractedDocument
from app.extraction import resume_nationality as rn


def _undetermined() -> ExtractedDocument:
    """What the local read produces for a CV whose columns defeated it."""
    return ExtractedDocument(
        text="Spray painter with 8 years experience.",
        method="pdf_text",
        char_count=37,
        nationality_accepted=True,
        nationality_reason="accepted under the undetermined-nationality policy",
    )


def test_the_service_settles_a_nationality_the_local_read_could_not():
    """The case that got in: Veris returned `nationality: Pakistani` outright,
    in a structured field nothing was reading."""
    result = {
        "personal_info": {"nationality": "Pakistani", "place_of_birth": None},
        "pages": [],
    }

    out = _reconsider_nationality(_undetermined(), result, "Spray painter.")

    assert out.nationality_accepted is False
    assert "Pakistan" in out.nationality_reason


def test_a_place_of_issue_alone_corroborates_but_does_not_decide():
    """A place name is the weakest evidence there is, and passing it through
    the second look does not promote it.

    That is the point of reusing one detector rather than scoring these fields
    separately: an Indian driver working in Sharjah has a UAE address, a UAE
    employer and very possibly a passport issued at a UAE consulate, and
    refusing him is the expensive mistake this whole filter is shaped around.
    One place scores 1.0 against a floor of 3.0 — so it corroborates a stated
    nationality and can never overrule one.
    """
    result = {
        "personal_info": {},
        "passport_details": {"place_of_issue": "Kathmandu, Nepal"},
        "pages": [],
    }

    out = _reconsider_nationality(_undetermined(), result, "")

    assert out.nationality_accepted is True


def test_a_place_of_issue_does_decide_alongside_a_stated_nationality():
    """The same weak signal, doing the job it is actually for."""
    result = {
        "personal_info": {"nationality": "Nepali"},
        "passport_details": {"place_of_issue": "Kathmandu, Nepal"},
        "pages": [],
    }

    out = _reconsider_nationality(_undetermined(), result, "")

    assert out.nationality_accepted is False
    assert "Nepal" in out.nationality_reason


def test_a_service_that_also_cannot_tell_changes_nothing():
    """UNDETERMINED twice is still accepted. The second look only ever makes the
    answer stricter — it is not a second chance to refuse a silent CV."""
    before = _undetermined()
    result = {"personal_info": {"nationality": None}, "pages": []}

    out = _reconsider_nationality(before, result, "Undergraduate student, Chennai.")

    assert out.nationality_accepted is True
    assert out.nationality_reason == before.nationality_reason


def test_an_indian_candidate_is_confirmed_not_refused():
    result = {"personal_info": {"nationality": "Indian"}, "pages": []}

    assert _reconsider_nationality(_undetermined(), result, "").nationality_accepted is True


def test_a_local_refusal_is_never_reopened():
    """A CV the local read already refused never reaches Veris at all — and if
    one ever did, the second look must not soften the first answer."""
    refused = ExtractedDocument(
        text="x", method="pdf_text", char_count=1,
        nationality_accepted=False,
        nationality_reason="rejected: other nationality (Pakistan)",
    )

    out = _reconsider_nationality(refused, {"personal_info": {"nationality": "Indian"}}, "")

    assert out.nationality_accepted is False
    assert "Pakistan" in out.nationality_reason


def test_a_broken_service_payload_does_not_fail_the_parse():
    """A second opinion is a bonus. It must never turn a successful extraction
    into an error."""
    class Hostile:
        def get(self, *_a, **_k):
            raise RuntimeError("nope")

    out = _reconsider_nationality(_undetermined(), Hostile(), "")

    assert out.nationality_accepted is True


def test_the_evidence_builder_labels_the_fields_it_found():
    """Rendered as labelled lines so the existing rules read it — one detector,
    one set of weights, rather than a second scoring path to drift."""
    text = rn.evidence_from_service(
        {"personal_info": {"nationality": "Pakistani", "place_of_birth": "Lahore"},
         "passport_details": {"place_of_issue": "Karachi"}},
        "body text here",
    )

    assert "Nationality : Pakistani" in text
    assert "Place of Birth : Lahore" in text
    assert "Place of Issue : Karachi" in text
    assert "body text here" in text


def test_nothing_to_read_is_not_a_refusal():
    assert rn.evidence_from_service({}, "") == ""
    assert _reconsider_nationality(_undetermined(), {}, "").nationality_accepted is True


def test_passport_label_misread_as_nationality_does_not_reject_on_second_look():
    result = {
        "personal_info": {"nationality": "رقم الجوازU1234567"},
        "pages": [],
    }
    out = _reconsider_nationality(_undetermined(), result, "Mumbai\nSaudi Arabia")

    assert out.nationality_accepted is True


def test_indian_service_nationality_is_not_overruled_by_foreign_employer_text():
    result = {"personal_info": {"nationality": "Indian"}, "pages": []}
    out = _reconsider_nationality(
        _undetermined(), result,
        "Nationality: Indian  Employer: Pakistani Trading Co\nKarachi\n+92 300 1234567",
    )

    assert out.nationality_accepted is True


def test_service_cannot_discard_an_explicit_indian_local_read():
    local_text = "Nationality: Indian\nSpray painter with 8 years experience."
    local = _undetermined().model_copy(update={
        "text": local_text,
        "nationality": rn.detect_resume_nationality(local_text).as_dict(),
    })
    result = {
        "personal_info": {"nationality": "Pakistani", "place_of_birth": "Lahore"},
        "pages": [],
    }

    out = _reconsider_nationality(local, result, "Nationality: Pakistani\n+92 300 1234567")

    assert out.nationality_accepted is True
    assert out.nationality["verdict"] == rn.UNDETERMINED
    assert "conflicting" in " ".join(out.nationality["evidence"])


def test_service_keeps_indian_passport_evidence_from_pages_outside_the_cv():
    from app.core.models import PageText
    from app.extraction import page_classifier as pc
    from tests.test_passport_nationality import INDIAN_PASSPORT

    local = _undetermined().model_copy(update={
        # parse_file keeps the local pages but its text field contains only the
        # CV by this stage. The passport can be recovered only from pages.
        "pages": [
            PageText(page_number=1, text="Experienced painter", kind=pc.RESUME),
            PageText(page_number=4, text=INDIAN_PASSPORT, kind=pc.PASSPORT),
        ],
        "nationality": {"verdict": rn.INDIAN, "country_code": "IND"},
    })

    out = _reconsider_nationality(
        local, {"personal_info": {"nationality": "Pakistani"}}, "Experienced painter",
    )

    assert out.nationality_accepted is True


def test_cached_indian_label_without_underlying_evidence_does_not_override_service():
    local = _undetermined().model_copy(update={
        "nationality": {"verdict": rn.INDIAN, "country_code": "IND"},
    })

    out = _reconsider_nationality(
        local, {"personal_info": {"nationality": "Pakistani"}}, "Experienced painter",
    )

    assert out.nationality_accepted is False
    assert out.nationality["country_code"] == "PAK"


def test_foreign_service_read_still_decides_when_local_india_is_only_a_workplace():
    local = _undetermined().model_copy(update={
        "text": "Experienced painter\nEmployer: Indian Trading Company, Chennai",
    })

    out = _reconsider_nationality(
        local, {"personal_info": {"nationality": "Pakistani"}}, "Experienced painter",
    )

    assert out.nationality_accepted is False
    assert out.nationality["country_code"] == "PAK"


def test_service_duplicate_birthplace_cannot_overrule_retained_indian_mrz():
    from app.core.models import PageText
    from app.extraction import page_classifier as pc

    local = _undetermined().model_copy(update={
        "pages": [PageText(
            page_number=2, text="P<INDKUMAR<<RAJESH<<<<<<<<<<<<<<<<<<<<<<<<<<", kind=pc.PASSPORT,
        )],
        "nationality": {"verdict": rn.INDIAN, "country_code": "IND"},
    })
    result = {"personal_info": {"nationality": "Pakistani", "place_of_birth": "Lahore"}}

    out = _reconsider_nationality(
        local, result,
        "Nationality: Pakistani\nPlace of Birth: Lahore;\n" * 3 + "Mobile: +92 300 1234567",
    )

    assert out.nationality_accepted is True
    assert out.nationality["verdict"] == rn.INDIAN
