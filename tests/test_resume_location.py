"""Locating the résumé, end to end, on real PDF bytes.

`test_page_classifier.py` pins the scoring; this pins the wiring — that
`extract_text` carries the classification, that `parse_file` refuses a misnamed
file before it spends anything, that a bundle is trimmed to the résumé pages
before it reaches the document parser, and that the pipeline turns all of that
into a `not_resume` result instead of a candidate record.
"""
from __future__ import annotations

import pytest

from app.ai.resume_parser import ResumeParser
from app.config import settings
from app.core.models import Attachment, EmailMessage
from app.extraction.text_extractor import extract_text
from tests.test_page_classifier import (
    CERTIFICATE_PAGE,
    EXPERIENCE_LETTER_PAGE,
    INVOICE_PAGE,
    PASSPORT_PAGE,
    RESUME_PAGE,
    RESUME_PAGE_TWO,
)

fitz = pytest.importorskip("fitz", reason="PyMuPDF is needed to build test PDFs")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """`.env` carries a live Veris key; no test may spend it."""
    monkeypatch.setattr(settings, "veris_ocr_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")


def make_pdf(pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 780), text, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def bundle_pages() -> list[str]:
    """A 30-page application whose CV sits on pages 15-16."""
    pages = [CERTIFICATE_PAGE, EXPERIENCE_LETTER_PAGE, PASSPORT_PAGE] * 4
    pages += [CERTIFICATE_PAGE, PASSPORT_PAGE]
    pages += [RESUME_PAGE, RESUME_PAGE_TWO]
    pages += [EXPERIENCE_LETTER_PAGE, CERTIFICATE_PAGE] * 7
    assert len(pages) == 30
    return pages


# --------------------------------------------------------------------------- #
#  Extraction carries the classification
# --------------------------------------------------------------------------- #
def test_extract_text_locates_the_resume_in_a_thirty_page_bundle():
    doc = extract_text(make_pdf(bundle_pages()), "application.pdf")

    assert doc.page_count == 30
    assert doc.is_resume is True
    assert doc.resume_pages == [15, 16]
    assert len(doc.pages) == 30


def test_full_text_is_kept_while_the_ai_payload_is_the_resume_only():
    """Rule 3 and rule 4 at once: nothing is discarded, but nothing extra is sent."""
    doc = extract_text(make_pdf(bundle_pages()), "application.pdf")

    # Every page is still in `text` — the extraction is not lossy.
    assert "TO WHOM IT MAY CONCERN" in doc.text
    assert "Passport No" in doc.text

    # The résumé slice is what the parsers get, and it is a fraction of the file.
    assert "EOT Crane Operator" in doc.resume_text
    assert "TO WHOM IT MAY CONCERN" not in doc.resume_text
    assert len(doc.resume_text) < len(doc.text) * 0.25


def test_invoice_named_cv_is_flagged_by_extraction():
    doc = extract_text(make_pdf([INVOICE_PAGE]), "cv.pdf")

    assert doc.is_resume is False
    assert doc.classification_confidence < 0.30
    assert doc.resume_pages == []


# --------------------------------------------------------------------------- #
#  parse_file refuses, and refuses cheaply
# --------------------------------------------------------------------------- #
def test_parse_file_rejects_a_misnamed_document_without_calling_a_parser(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("a rejected document must never reach the parsers")

    parser = ResumeParser()
    monkeypatch.setattr(parser, "parse_text_fallback", explode)

    profile, doc = parser.parse_file(make_pdf([INVOICE_PAGE]), "cv.pdf")

    assert profile.is_resume is False
    assert profile.confidence < 0.30
    assert profile.full_name is None and profile.email is None and profile.phone is None
    assert "no resume content" in profile.additional_info["rejection_reason"]
    assert profile.additional_info["page_kinds"] == {"1": "other"}
    assert doc.is_resume is False


def test_parse_file_extracts_the_candidate_from_deep_inside_a_bundle():
    profile, _ = ResumeParser().parse_file(make_pdf(bundle_pages()), "application.pdf")

    assert profile.is_resume
    assert profile.email == "rajesh.kumar87@gmail.com"
    assert "98765 43210" in (profile.phone or "")
    assert "RAJESH KUMAR" in (profile.full_name or "").upper()


def test_bundle_is_trimmed_to_the_resume_pages_before_parsing():
    """What the document parser receives is a 2-page PDF, not a 30-page one."""
    data = make_pdf(bundle_pages())
    extracted = extract_text(data, "application.pdf")

    trimmed, name = ResumeParser._resume_only_document(data, "application.pdf", extracted)

    assert name.endswith(".pdf") and trimmed != data
    with fitz.open(stream=trimmed, filetype="pdf") as doc:
        assert doc.page_count == 2
        assert "EOT Crane Operator" in doc[0].get_text()
        assert "TO WHOM IT MAY CONCERN" not in "".join(p.get_text() for p in doc)


def test_a_single_page_resume_is_sent_whole():
    data = make_pdf([RESUME_PAGE])
    extracted = extract_text(data, "cv.pdf")

    trimmed, name = ResumeParser._resume_only_document(data, "cv.pdf", extracted)

    assert trimmed is data and name == "cv.pdf"


# --------------------------------------------------------------------------- #
#  The pipeline turns a rejection into a skip, not a candidate
# --------------------------------------------------------------------------- #
class _NoLedger:
    def is_message_suppressed(self, *_a):
        return False

    def message_seen(self, *_a):
        return False

    def is_suppressed(self, *_a):
        return False

    def find_by_hash(self, *_a):
        return None

    def record(self, *_a, **_k):
        return None


class _NoRepo:
    def find_by_message_id(self, *_a):
        return None

    def find_by_resume_hash(self, *_a):
        return None

    def find_by_email_or_phone(self, *_a):
        raise AssertionError("a rejected attachment must never reach dedup")

    def insert(self, *_a):
        raise AssertionError("a rejected attachment must never be stored")


class _NoStorage:
    name = "none"

    def exists(self, *_a, **_k):
        raise AssertionError("a rejected attachment must never be stored")

    def save(self, *_a, **_k):
        raise AssertionError("a rejected attachment must never be stored")


def _email_with(filename: str, data: bytes) -> EmailMessage:
    return EmailMessage(
        message_id="msg-1",
        thread_id="thr-1",
        from_addr="applicant@example.com",
        subject="Application for Crane Operator - resume attached",
        attachments=[
            Attachment(
                filename=filename, mime_type="application/pdf",
                size=len(data), attachment_id="att-1", data=data,
            )
        ],
    )


def _pipeline():
    from app.ingestion.pipeline import IngestionPipeline

    return IngestionPipeline(
        repository=_NoRepo(), storage=_NoStorage(),
        parser=ResumeParser(), ledger=_NoLedger(),
    )


def test_pipeline_skips_an_invoice_named_cv():
    data = make_pdf([INVOICE_PAGE])
    result = _pipeline().process_email(_email_with("cv.pdf", data))

    assert [a.status for a in result.attachments] == ["not_resume"]
    assert result.status == "skipped"
    assert result.ingested_ids == []


def test_pipeline_skips_a_bundle_with_no_resume_in_it():
    data = make_pdf([CERTIFICATE_PAGE, EXPERIENCE_LETTER_PAGE, PASSPORT_PAGE])
    result = _pipeline().process_email(_email_with("resume.pdf", data))

    assert [a.status for a in result.attachments] == ["not_resume"]
    assert "not a resume" in result.attachments[0].detail


# --------------------------------------------------------------------------- #
#  "I could not read it" is not "it is not a resume"
# --------------------------------------------------------------------------- #
def test_an_unreadable_scan_is_an_error_not_a_rejection(monkeypatch):
    """A scanned CV with OCR unavailable must be retryable, never dismissed.

    A real 9-page scanned application produced zero characters because Tesseract
    was not installed. Scored on that evidence the classifier said "not a
    resume" — correctly, and catastrophically: every scanned application in the
    mailbox would have been skipped as a misnamed file and marked handled.
    """
    from app.core.exceptions import TextExtractionError
    from app.extraction import text_extractor as tx

    # What a host with no OCR engine actually produces: not an exception, but
    # zero characters. That is the dangerous case — an empty extraction reads
    # to the classifier as a perfectly confident "this is not a resume".
    def no_ocr(*_a, **_k):
        return {}

    monkeypatch.setattr(tx.local_ocr, "ocr_pdf_page_texts", no_ocr)

    # A PDF with pages but no text layer at all.
    doc = fitz.open()
    for _ in range(9):
        doc.new_page()
    scanned = doc.tobytes()
    doc.close()

    with pytest.raises(TextExtractionError) as exc:
        extract_text(scanned, "Asif_mohd_MOTOR WORKSHOP ADMIN.pdf")
    assert "no text" in str(exc.value).lower()


def test_pipeline_reports_an_unreadable_scan_as_an_error(monkeypatch):
    """Status 'error', so the runner leaves the mail unlabelled and retries it."""
    from app.core.exceptions import TextExtractionError
    from app.extraction import text_extractor as tx

    monkeypatch.setattr(
        tx.local_ocr, "ocr_pdf_page_texts",
        lambda *a, **k: (_ for _ in ()).throw(TextExtractionError("no tesseract")),
    )

    doc = fitz.open()
    for _ in range(9):
        doc.new_page()
    scanned = doc.tobytes()
    doc.close()

    result = _pipeline().process_email(_email_with("scanned_cv.pdf", scanned))

    assert [a.status for a in result.attachments] == ["error"]
    assert result.status == "error"


# --------------------------------------------------------------------------- #
#  Rule 4 — the recruiter downloads the original, all pages intact
# --------------------------------------------------------------------------- #
class _CapturingStorage:
    name = "capture"

    def __init__(self):
        self.saved: dict[str, bytes] = {}

    def exists(self, key) -> bool:
        return key in self.saved

    def save(self, key, data, content_type=None):
        self.saved[key] = data
        return key


class _CapturingRepo(_NoRepo):
    def __init__(self):
        self.inserted = []

    def find_by_email_or_phone(self, *_a):
        return None

    def insert(self, record):
        self.inserted.append(record)
        return record.id

    def mark_auto_reply_sent(self, *_a):
        return None


def test_the_stored_file_is_the_untouched_original_bundle():
    """Trimming is a parsing-time copy. Storage gets all 30 pages, byte for byte."""
    from app.ingestion.pipeline import IngestionPipeline

    data = make_pdf(bundle_pages())
    storage, repo = _CapturingStorage(), _CapturingRepo()
    pipeline = IngestionPipeline(
        repository=repo, storage=storage, parser=ResumeParser(), ledger=_NoLedger(),
    )

    result = pipeline.process_email(_email_with("application.pdf", data))
    assert [a.status for a in result.attachments] == ["ingested"], result.attachments

    assert len(storage.saved) == 1
    stored = next(iter(storage.saved.values()))
    assert stored == data, "the stored file is not the original bytes"

    with fitz.open(stream=stored, filetype="pdf") as doc:
        assert doc.page_count == 30
        every = "".join(p.get_text() for p in doc)
        assert "TO WHOM IT MAY CONCERN" in every      # experience letters kept
        assert "Passport No" in every                  # ID scans kept
        assert "CERTIFICATE OF COMPLETION" in every    # certificates kept

    # And the record points at that file, with its real size and hash.
    record = repo.inserted[0]
    assert record.resume.size == len(data)
    assert record.resume.original_filename == "application.pdf"


def test_trimming_never_mutates_the_caller_s_bytes():
    data = make_pdf(bundle_pages())
    before = bytes(data)
    extracted = extract_text(data, "application.pdf")

    ResumeParser._resume_only_document(data, "application.pdf", extracted)

    assert data == before


# --------------------------------------------------------------------------- #
#  What the filename blocklist used to guard, now guarded by content
# --------------------------------------------------------------------------- #
HALL_TICKET = """
ANNA UNIVERSITY
HALL TICKET / ADMIT CARD
Roll No: 812021104033
Name: THARUN V
Examination Centre: Trichy Zone 4
Seat No: B-118
"""

OD_LETTER = """
DEPARTMENT OF INFORMATION TECHNOLOGY

Sub: Request for On-Duty permission

Respected Sir,
I request you to kindly grant me on-duty leave for 14/05/2024 to attend the
inter-college symposium. Kindly do the needful.

Thanking you
Tharun V
"""


def test_a_hall_ticket_is_refused_on_content():
    """Stage 1 now opens it; this is what keeps it out of the database."""
    result = _pipeline().process_email(_email_with("Ethics HT.pdf", make_pdf([HALL_TICKET])))

    assert [a.status for a in result.attachments] == ["not_resume"]
    assert result.ingested_ids == []


def test_an_od_letter_is_refused_on_content():
    result = _pipeline().process_email(
        _email_with("OD Request Letter for Tharun V.pdf", make_pdf([OD_LETTER]))
    )

    assert [a.status for a in result.attachments] == ["not_resume"]


def test_a_resume_named_like_junk_is_ingested_anyway():
    """The inverse, and the bug that started this: the name means nothing."""
    data = make_pdf([RESUME_PAGE, RESUME_PAGE_TWO])
    storage, repo = _CapturingStorage(), _CapturingRepo()
    from app.ingestion.pipeline import IngestionPipeline

    pipeline = IngestionPipeline(
        repository=repo, storage=storage, parser=ResumeParser(), ledger=_NoLedger(),
    )
    result = pipeline.process_email(
        _email_with("Asif_mohd_MOTOR WORKSHOP ADMIN.pdf", data)
    )

    assert [a.status for a in result.attachments] == ["ingested"]
    assert repo.inserted[0].profile.email == "rajesh.kumar87@gmail.com"


# --------------------------------------------------------------------------- #
#  Rule 3 — the fields the cloud extractor does not return on its own
# --------------------------------------------------------------------------- #
def test_location_is_split_into_city_and_country():
    from app.ai.resume_parser import _split_location

    assert _split_location("Chennai, Tamil Nadu, India") == ("Chennai", "India")
    assert _split_location("Dubai, United Arab Emirates") == ("Dubai", "United Arab Emirates")
    # Unrecognised country: keep the whole thing rather than cut at a guess.
    assert _split_location("Some Village, Somewhere") == ("Some Village", None)
    assert _split_location("") == (None, None)
    assert _split_location(None) == (None, None)


def test_trade_skills_are_picked_out_of_a_generic_skills_list():
    from app.ai.resume_parser import _trade_skills_from

    skills = ["EOT Crane Operation", "MS Office", "TIG Welding", "Communication"]
    found = _trade_skills_from(skills, "")

    assert "EOT Crane Operation" in found
    assert "TIG Welding" in found
    assert "MS Office" not in found
    assert "Communication" not in found


def test_trade_skills_are_also_recovered_from_the_resume_text():
    from app.ai.resume_parser import _trade_skills_from

    text = "Operated 50-ton EOT crane and carried out pipe fitting on shutdown jobs."
    found = [f.lower() for f in _trade_skills_from([], text)]

    assert "eot crane" in found
    assert "pipe fitting" in found


class _SilentlyLosingStorage:
    """A backend whose `save` reports success and keeps nothing.

    A full disk, a GridFS write the server rejected, a mount that went away —
    they all look like this from in here, and none of them raise.
    """

    name = "lossy"

    def exists(self, _key) -> bool:
        return False

    def save(self, key, _data, content_type=None):
        return key


def test_a_candidate_is_never_created_when_the_file_did_not_store():
    """The upload is the one artefact that cannot be recreated.

    A profile can be re-parsed and the OCR re-run, but a résumé nobody kept is
    gone once the mail is filed. Inserting the record anyway produces a
    candidate whose Download button can only fail — discovered by a recruiter,
    months later, at the moment they need the file. Reported as an error, so the
    email stays unlabelled and the next poll tries again.
    """
    from app.ingestion.pipeline import IngestionPipeline

    data = make_pdf(bundle_pages())
    repo = _CapturingRepo()
    pipeline = IngestionPipeline(
        repository=repo, storage=_SilentlyLosingStorage(),
        parser=ResumeParser(), ledger=_NoLedger(),
    )

    result = pipeline.process_email(_email_with("application.pdf", data))

    assert [a.status for a in result.attachments] == ["error"]
    assert "not there when read back" in result.attachments[0].detail
    assert repo.inserted == [], "a candidate was created with no downloadable file"
