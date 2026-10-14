"""Only Indian candidates reach the résumé endpoint and the database.

Two halves, and the second is the one that matters.

The first pins the refusal: a CV that says it belongs to somebody who is not an
Indian national must not be uploaded, must not be structured, and must not
produce a record.

The second pins the *acceptance*, and it is the half a filter like this usually
gets wrong. Rejecting a real candidate loses a placement silently — there is no
record to review, because the whole point was that no record was written — so
these tests spend more effort on the people who must get through than on the
one who must not.
"""
from __future__ import annotations

import fitz
import pytest

from app.config import settings
from app.core.exceptions import ForeignNationalityError
from app.extraction import resume_nationality as rn
from app.extraction import text_extractor as tx
from app.ingestion.pipeline import _refuse_foreign_candidate


def text_pdf(body: str) -> bytes:
    """A PDF with a real text layer, so no OCR is involved in these tests."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 560, 800), body, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out


PAKISTANI_CV = """MUHAMMAD USMAN
Bus Driver / Heavy Vehicle Operator
Nationality: Pakistani          Place of Birth: Lahore
Mobile: +92 300 1234567     Email: usman@example.com
EXPERIENCE
Al Nakheel Transport LLC, Dubai - Driver, 2019 to present
Operated 52 seat coaches on scheduled city routes and airport runs.
EDUCATION
Higher Secondary Certificate, Board of Lahore, 2006
SKILLS  Route planning, passenger safety, vehicle inspection
"""

INDIAN_CV_SILENT = """RAJESH KUMAR
Heavy Vehicle Driver
Mobile: +91 98765 43210   Email: rajesh@example.com
EXPERIENCE
Ashok Leyland, Chennai - Driver, 2018 to present
Drove articulated tankers on interstate routes across Tamil Nadu.
EDUCATION
Higher Secondary, Tamil Nadu Board, 2010
SKILLS  Route planning, vehicle inspection, first aid
"""

INDIAN_CV_IN_THE_GULF = """SURESH NAIR
Bus Driver
Address: Al Nahda, Sharjah, United Arab Emirates
Mobile: +971 50 123 4567   Email: suresh@example.com
EXPERIENCE
Emirates Transport, Dubai - Driver, 2016 to present
Operated school coaches on scheduled routes.
EDUCATION  Secondary School Certificate, 2008
SKILLS  Passenger safety, defensive driving
"""


@pytest.fixture
def no_uploads(monkeypatch):
    """Fail loudly if anything reaches the résumé endpoint, and count calls."""
    calls: list[str] = []

    def _refine(data, filename, page_texts, resume_pages):
        calls.append(filename)
        return page_texts, None

    monkeypatch.setattr(tx, "_refine_resume_pages", _refine)
    return calls


# --------------------------------------------------------------------------- #
#  The detector
# --------------------------------------------------------------------------- #
def test_a_stated_foreign_nationality_is_read():
    verdict = rn.detect_resume_nationality(PAKISTANI_CV)
    assert verdict.verdict == rn.FOREIGN
    assert verdict.country_code == "PAK"
    assert "Pakistani" in verdict.describe()


def test_a_cv_that_states_nothing_is_undetermined_not_foreign():
    """The distinction the whole policy rests on."""
    verdict = rn.detect_resume_nationality(INDIAN_CV_SILENT)
    assert verdict.verdict == rn.UNDETERMINED
    accept, _reason = rn.should_ingest(verdict)
    assert accept is True


def test_an_indian_abroad_is_not_rejected_on_address_and_phone_alone():
    """The expensive mistake, pinned.

    A UAE address and a +971 mobile are two weak signals and nothing else. They
    total 2.0 against a floor of 3.0, so they cannot refuse anybody — which is
    the difference between a filter and a liability, because this CV belongs to
    an Indian driver working in Sharjah.
    """
    verdict = rn.detect_resume_nationality(INDIAN_CV_IN_THE_GULF)
    assert verdict.verdict != rn.FOREIGN
    assert rn.should_ingest(verdict)[0] is True


def test_a_stated_indian_nationality_outranks_a_foreign_workplace():
    verdict = rn.detect_resume_nationality(
        "SURESH NAIR  Nationality: Indian\n"
        "Address: Sharjah, UAE   Mobile: +971 50 123 4567\n"
        "Emirates Transport, Dubai - Driver"
    )
    assert verdict.verdict == rn.INDIAN
    assert rn.should_ingest(verdict)[0] is True


def test_an_empty_document_is_undetermined_and_accepted():
    """An unreadable scan is not a foreigner."""
    verdict = rn.detect_resume_nationality("")
    assert verdict.verdict == rn.UNDETERMINED
    assert rn.should_ingest(verdict)[0] is True


def test_a_passport_in_the_bundle_outranks_the_cv(monkeypatch):
    """A booklet is a government document; a work history is not."""
    import app.extraction.passport_nationality as pn

    foreign = pn.NationalityVerdict(verdict=pn.FOREIGN, country_code="PAK", confidence=0.9)
    verdict = rn.detect_resume_nationality(
        "RAJESH KUMAR\nDriver, Chennai\n", passport_verdicts={4: foreign}
    )
    assert verdict.verdict == rn.FOREIGN
    assert verdict.country_code == "PAK"


def test_the_filter_can_be_switched_off():
    verdict = rn.detect_resume_nationality(PAKISTANI_CV)
    accept, reason = rn.should_ingest(verdict, india_only=False)
    assert accept is True
    assert "disabled" in reason


def test_undetermined_is_refused_when_the_policy_demands_proof():
    """The strict setting exists; it is simply not the default."""
    verdict = rn.detect_resume_nationality(INDIAN_CV_SILENT)
    accept, _reason = rn.should_ingest(verdict, allow_undetermined=False)
    assert accept is False


# --------------------------------------------------------------------------- #
#  The gates
# --------------------------------------------------------------------------- #
def test_a_foreign_cv_never_reaches_the_resume_endpoint(no_uploads):
    """The upload gate: refused before a byte leaves the building.

    Driven through `_classified` with `ocr_pages` set, because that is the only
    condition under which the résumé endpoint is called at all — a PDF with its
    own text layer is never sent for a better read, so extracting one would
    prove nothing about the gate.
    """
    data = text_pdf(PAKISTANI_CV)
    extracted = tx._classified(
        [PAKISTANI_CV], method="pdf_ocr", ocr_pages={1}, data=data, filename="usman.pdf",
    )
    assert extracted.nationality_accepted is False
    assert no_uploads == [], "a rejected CV was uploaded to Veris"


def test_an_indian_cv_does_reach_the_resume_endpoint(no_uploads):
    """The same path, the same conditions, the other verdict.

    Without this, a gate that refused *everything* would pass every other test
    in this file.
    """
    data = text_pdf(INDIAN_CV_SILENT)
    extracted = tx._classified(
        [INDIAN_CV_SILENT], method="pdf_ocr", ocr_pages={1}, data=data, filename="cv.pdf",
    )
    assert extracted.nationality_accepted is True
    assert no_uploads == ["cv.pdf"], "an accepted CV was not sent for a better read"


def test_a_foreign_cv_never_reaches_the_database(no_uploads):
    """The database gate, reading the same decision the upload gate read."""
    extracted = tx.extract_text(text_pdf(PAKISTANI_CV), "usman.pdf")
    with pytest.raises(ForeignNationalityError) as caught:
        _refuse_foreign_candidate("usman.pdf", extracted)
    assert "Pakistan" in str(caught.value)


@pytest.mark.parametrize(
    "cv, label",
    [(INDIAN_CV_SILENT, "states nothing"), (INDIAN_CV_IN_THE_GULF, "works abroad")],
)
def test_an_indian_cv_still_reaches_both(no_uploads, cv, label):
    """The regression that would cost placements rather than API calls."""
    extracted = tx.extract_text(text_pdf(cv), "cv.pdf")
    assert extracted.nationality_accepted is True, label
    _refuse_foreign_candidate("cv.pdf", extracted)  # must not raise


def test_an_extraction_without_the_field_is_not_refused():
    """Anything predating the filter is ingested, not thrown away."""

    class Old:
        pass

    _refuse_foreign_candidate("legacy.pdf", Old())  # must not raise


# --------------------------------------------------------------------------- #
#  Any foreign country, not merely the tabled ones
# --------------------------------------------------------------------------- #
def _field(line: str) -> rn.ResumeNationality:
    return rn.detect_resume_nationality(
        f"CANDIDATE NAME\nDriver\n{line}\nEmail: x@example.com"
    )


@pytest.mark.parametrize(
    "stated",
    ["Pakistani", "Bangladeshi", "Nepali", "Sri Lankan", "Filipino", "Afghan",
     "Burmese", "Indonesian", "Nigerian", "Egyptian", "Syrian", "Kenyan"],
)
def test_a_tabled_foreign_nationality_is_refused(stated):
    assert rn.should_ingest(_field(f"Nationality: {stated}"))[0] is False


@pytest.mark.parametrize(
    "stated",
    ["Zimbabwean", "Bolivian", "Icelandic", "Namibian", "Papua New Guinean",
     "Trinidadian", "Luxembourgish", "Seychellois", "Malagasy"],
)
def test_less_common_recognised_nationalities_are_still_refused(stated):
    """Requiring a recognised nationality must preserve the wider country list."""
    verdict = _field(f"Nationality: {stated}")
    assert verdict.verdict == rn.FOREIGN
    assert rn.should_ingest(verdict)[0] is False


@pytest.mark.parametrize(
    "stated",
    ["Indian", "INDIAN", "indian", "lndian", "Indien", "Bharatiya",
     "Indian (Hindu)", "Indian by birth", "Indian national"],
)
def test_indian_survives_its_spellings_and_the_scanner(stated):
    """`lndian` is what Tesseract makes of a capital I. It must still get home."""
    assert rn.should_ingest(_field(f"Nationality: {stated}"))[0] is True


@pytest.mark.parametrize(
    "stated", ["N/A", "-", "Not Specified", "NIL", "None", "Male", "TBD"],
)
def test_an_unfilled_field_is_not_a_foreigner(stated):
    assert rn.should_ingest(_field(f"Nationality: {stated}"))[0] is True


@pytest.mark.parametrize(
    "line",
    ["Nationality and religion: Indian, Hindu",
     "Nationality details are attached separately",
     "Nationality proof enclosed",
     "Nationality of spouse: Indian",
     "Citizenship: Indian by birth",
     "Nationality    Indian"],
)
def test_prose_about_nationality_never_rejects_an_indian(line):
    """The regression this nearly shipped with.

    Reading the first words after the label as a country turned "Nationality
    proof enclosed" into a foreign national and threw away an Indian candidate
    over a filing note. A country is looked for across the whole line, and the
    unrecognised-answer path is fenced off behind prose openers.
    """
    assert rn.should_ingest(_field(line))[0] is True


@pytest.mark.parametrize(
    "line",
    ["Nationality and religion: Pakistani, Muslim",
     "Citizenship: Nepali by birth",
     "Nationality of applicant: Filipino"],
)
def test_a_country_named_later_on_the_line_is_still_read(line):
    """Fencing off prose must not become a way to smuggle a nationality past."""
    assert rn.should_ingest(_field(line))[0] is False


# --------------------------------------------------------------------------- #
#  A refusal already decided must not be paid for again
# --------------------------------------------------------------------------- #
def test_a_foreign_cv_is_refused_before_the_resume_endpoint_is_called(no_uploads, monkeypatch):
    """The waste this closes: the refusal used to land *after* the Veris parse.

    The extractor establishes nationality and declines its own refinement call
    on the answer, but `parse_file` went on to send the résumé endpoint a CV the
    desk had already decided it cannot place — a full OCR and a billed parse, on
    every poll, to reach a refusal that was known seconds earlier.
    """
    from app.ai.resume_parser import ResumeParser

    sent: list = []

    def record_send(*args, **kwargs):
        sent.append(args)
        raise AssertionError("the résumé endpoint must not be called for a refused CV")

    parser = ResumeParser()
    monkeypatch.setattr(parser, "_resume_only_document", record_send, raising=False)

    with pytest.raises(ForeignNationalityError) as caught:
        parser.parse_file(text_pdf(PAKISTANI_CV), "usman.pdf")

    assert "Pakistan" in str(caught.value)
    assert sent == [], "no work was done past the refusal"


def test_an_indian_cv_is_not_stopped_on_its_way_to_the_parsers(no_uploads, monkeypatch):
    """The regression that would cost placements: the guard must let CVs past."""
    from app.ai.resume_parser import ResumeParser

    reached = []

    def stop_here(data, filename, extracted):
        reached.append(filename)
        raise RuntimeError("far enough — the guard let this through")

    parser = ResumeParser()
    monkeypatch.setattr(parser, "_resume_only_document", stop_here, raising=False)

    with pytest.raises(RuntimeError):
        parser.parse_file(text_pdf(INDIAN_CV_SILENT), "cv.pdf")

    assert reached == ["cv.pdf"]


# --------------------------------------------------------------------------- #
#  What a PDF text layer does to a stated nationality
# --------------------------------------------------------------------------- #
# Verbatim from the two-column CV that was ingested despite saying "Pakistani".
# The label, the colon and the value each land on their own line.
SPLIT_LABEL_CV = (
    "Experienced as a Spray painter with 8 years in the industry.\n\n"
    "CONTACT\n\n"
    "Father Name  : \nLaiq Shah \nNationality \n: \nPakistani \n"
    "Gender \n : \nMale \nQ I D \n: \n30058612071 \n"
    "Contact \n : \n +974 74045265 \nDate of Birth \n: \n02-01-2000 \n"
)


def test_a_nationality_split_across_lines_is_still_stated():
    """The bug that let a Pakistani CV through.

    Every rule expects `Nationality : Pakistani`. A two-column PDF's text layer
    gives `Nationality \n : \n Pakistani`, so the stated-nationality rule
    matched nothing and the verdict came back UNDETERMINED at 0.00 — which the
    résumé policy admits by default. The same detector on the same résumé,
    de-columnised, said FOREIGN at 0.92.
    """
    verdict = rn.detect_resume_nationality(SPLIT_LABEL_CV)
    accept, reason = rn.should_ingest(verdict)

    assert verdict.verdict == rn.FOREIGN, verdict.describe()
    assert accept is False, reason
    assert "Pakistan" in reason


def test_the_gender_male_is_not_the_capital_of_the_maldives():
    """`\bmal[eé]\b` matched "Gender : Male" on every CV that states one.

    On the Pakistani CV above that invented a second country, and the two
    competing signals were what pushed a clear FOREIGN down to UNDETERMINED.
    """
    verdict = rn.detect_resume_nationality(SPLIT_LABEL_CV)

    assert "Maldives" not in verdict.describe()
    assert not any("maldiv" in e.lower() or "male" in e.lower() for e in verdict.evidence), (
        verdict.evidence
    )


def test_a_maldivian_cv_is_still_recognised():
    """The token was narrowed, not deleted."""
    verdict = rn.detect_resume_nationality("Nationality : Maldivian\nBorn in Maldives\n")

    assert verdict.verdict == rn.FOREIGN
    assert "Maldives" in verdict.describe()


def test_an_ordinary_indian_cv_is_untouched_by_the_line_joining():
    verdict = rn.detect_resume_nationality(
        "Nationality \n: \nIndian \nGender \n : \nMale \nPlace of Birth \n: \nChennai \n"
    )
    accept, _ = rn.should_ingest(verdict)

    assert accept is True
    assert verdict.verdict == rn.INDIAN


@pytest.mark.parametrize(
    "field",
    [
        "Nationality رقم الجوازU1234567",
        "Nationality: رقم الجوازU1234567",
        "Nationality\nرقم الجوازU1234567",
        "Nationality\n: \nرقم الجوازU1234567",
    ],
)
def test_passport_label_in_nationality_field_is_not_a_foreign_national(field):
    # The reported rejection assigned OTH 4.0 and confidence 0.83 to an
    # Arabic passport-number label. The identifier here is synthetic.
    verdict = rn.detect_resume_nationality(f"{field}\nMumbai\nSaudi")

    assert verdict.verdict == rn.UNDETERMINED
    assert rn.OTHER_CODE not in verdict.scores
    assert rn.should_ingest(verdict)[0] is True


@pytest.mark.parametrize(
    "value",
    ["Unreadable", "Not available", "Unknown nationality", "U1234567",
     "Passport Number", "Date of expiry", "الجنسية", "رقم الجواز", "???"],
)
def test_unrecognised_nationality_is_not_proof_of_a_foreign_country(value):
    verdict = _field(f"Nationality: {value}")

    assert verdict.verdict == rn.UNDETERMINED
    assert rn.should_ingest(verdict)[0] is True


@pytest.mark.parametrize("label", ["Nationality", "Nationality:", "Citizenship"])
@pytest.mark.parametrize("next_line", ["Pakistani employer", "Nepal", "Saudi Arabia"])
def test_blank_nationality_does_not_capture_an_unrelated_next_line(label, next_line):
    verdict = rn.detect_resume_nationality(f"{label}\n{next_line}\n")

    assert verdict.verdict == rn.UNDETERMINED
    assert rn.should_ingest(verdict)[0] is True


@pytest.mark.parametrize(
    "field",
    [
        "Nationality: Indian    Employer: Pakistani Trading Co",
        "Nationality: Indian | Place of Birth: Bangladesh",
        "Nationality: Indian / Current location: United Arab Emirates",
        "Nationality: lndian    Work location: Pakistan",
    ],
)
def test_nationality_value_is_not_overruled_by_another_field_on_the_same_line(field):
    verdict = rn.detect_resume_nationality(field)

    assert verdict.verdict == rn.INDIAN
    assert rn.should_ingest(verdict)[0] is True


@pytest.mark.parametrize(
    "field",
    [
        "Nationality: Passport issued in Nepal",
        "Nationality:    Employer: Pakistani Trading Co",
        "Nationality proof enclosed from Pakistan",
    ],
)
def test_country_in_an_unrelated_field_is_not_a_stated_nationality(field):
    verdict = rn.detect_resume_nationality(field)

    assert verdict.verdict == rn.UNDETERMINED
    assert rn.should_ingest(verdict)[0] is True


def test_birthplace_address_and_phone_cannot_prove_foreign_citizenship():
    verdict = rn.detect_resume_nationality(
        "Place of Birth: Lahore\nAddress: Karachi\nMobile: +92 300 1234567\n"
    )

    assert verdict.verdict == rn.UNDETERMINED
    assert rn.should_ingest(verdict)[0] is True


def test_unknown_passport_text_reaches_extraction_and_database_gates(no_uploads):
    cv = INDIAN_CV_SILENT + "\nNationality: رقم الجوازU1234567\nSaudi Arabia\n"
    extracted = tx._classified(
        [cv], method="pdf_ocr", ocr_pages={1}, data=b"pdf", filename="cv.pdf",
    )

    assert extracted.nationality_accepted is True
    assert no_uploads == ["cv.pdf"]
    _refuse_foreign_candidate("cv.pdf", extracted)


@pytest.mark.parametrize(
    "stated, code, native", [("India", "IND", "ﺍﻟﻬﻨﺪ"), ("Pakistan", "PAK", "باكستان")],
)
def test_bilingual_visa_row_reads_nationality_before_the_trailing_english_label(stated, code, native):
    # The original PDF uses Arabic presentation forms, no gap before the
    # country value, and the English label at the end of the extracted row.
    text = (
        f"ﺍﻟﺠﻨﺴﻴﺔ{stated}\u00a0‐\u00a0{native}Nationality\n"
        "ﺭﻗﻢ\u00a0ﺍﻟﺠﻮﺍﺯU1234567\nPassport\u00a0No.\n"
        "Saudi Mission In Mumbai\n"
    )
    verdict = rn.detect_resume_nationality(text)

    assert verdict.country_code == code
    assert rn.should_ingest(verdict)[0] is (code == "IND")


@pytest.mark.parametrize("stated", ["Pakistani", "Indonesian", "Filipino"])
def test_explicit_foreign_nationality_is_not_replaced_by_indian_employer(stated):
    verdict = _field(f"Nationality: {stated}    Employer: Indian Trading Co")

    assert verdict.verdict == rn.FOREIGN
    assert rn.should_ingest(verdict)[0] is False


def test_indian_resume_with_bilingual_visa_passes_both_gates(no_uploads):
    visa = (
        "KINGDOM OF SAUDI ARABIA\nWork Visa\nSaudi Mission in Mumbai\n"
        "ﺍﻟﺠﻨﺴﻴﺔIndia ‐ ﺍﻟﻬﻨﺪNationality\n"
        "ﺭﻗﻢ ﺍﻟﺠﻮﺍﺯU1234567\nPassport No.\n"
    )
    extracted = tx._classified(
        [INDIAN_CV_SILENT, visa], method="pdf_ocr", ocr_pages={1},
        data=b"pdf", filename="cv-and-visa.pdf",
    )

    assert extracted.nationality["verdict"] == rn.INDIAN
    assert extracted.nationality_accepted is True
    assert extracted.resume_pages == [1]
    assert no_uploads == ["cv-and-visa.pdf"]
    _refuse_foreign_candidate("cv-and-visa.pdf", extracted)


@pytest.mark.parametrize(
    "is_resume, expected_status", [(False, "not_resume"), (True, "rejected_nationality")],
)
def test_only_a_resume_can_emit_a_candidate_nationality_rejection(monkeypatch, is_resume, expected_status):
    from app.core.models import Attachment, CandidateProfile, EmailMessage, ExtractedDocument
    from app.ingestion.pipeline import IngestionPipeline
    from tests.test_pipeline_redelivery import FakeLedger, FakeRepo, FakeStorage

    class Parser:
        def parse_file(self, _data, _filename):
            return CandidateProfile(is_resume=is_resume), ExtractedDocument(
                text="KINGDOM OF SAUDI ARABIA\nVisa", method="pdf_text", char_count=31,
                is_resume=is_resume, classification_reason="identity document",
                nationality_accepted=False, nationality_reason="rejected: other nationality",
            )

    repo, storage = FakeRepo(), FakeStorage()
    pipeline = IngestionPipeline(repo, storage, Parser(), FakeLedger())
    rejections = []
    monkeypatch.setattr(pipeline, "_announce_rejection", lambda *args: rejections.append(args))
    attachment = Attachment(
        filename="attachment.pdf", mime_type="application/pdf", size=3,
        attachment_id="attachment-1", data=b"pdf",
    )
    email = EmailMessage(message_id="visa-1", thread_id="visa-1", from_addr="sender@example.com")

    result = pipeline._process_attachment(email, attachment, None)

    assert result.status == expected_status
    assert bool(rejections) is is_resume
    assert repo.records == {}
    assert storage.saved == {}
