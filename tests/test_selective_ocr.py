"""Nothing is uploaded before it has been read and identified locally.

The original failure was an ordering mistake: a document was sent to the Veris
*résumé* endpoint in order to find out what it was. Bank statements, job-board
digests and marketing PDFs were therefore billed as résumé extractions, and a
timeout on a large scan lost the candidate outright.

The order is now: read every page locally, classify each page from its own
content, and only then send the pages that are a résumé — and only those — to
the résumé endpoint. These tests pin that ordering down, because it is the
property that keeps unwanted mail out of a paid API.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.extraction import text_extractor as tx
from tests.test_page_classifier import (
    CERTIFICATE_PAGE,
    EXPERIENCE_LETTER_PAGE,
    INVOICE_PAGE,
    PASSPORT_PAGE,
    RESUME_PAGE,
    RESUME_PAGE_TWO,
)
from tests.test_resume_location import make_pdf

fitz = pytest.importorskip("fitz", reason="PyMuPDF is needed to build test PDFs")


@pytest.fixture
def scanned(monkeypatch):
    """Make every PDF look like a pure scan and record what was read and sent.

    `_page_layout_text` returning "" is what a scan looks like to PyMuPDF. The
    stand-in local reader takes its text off the real pages it is asked for, so
    page numbering is exercised for real; only Tesseract and the network are
    faked.

    Yields ``(pages_read_locally, pages_uploaded)``.
    """
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(tx, "_page_layout_text", lambda _page: "")

    read_locally: list[int] = []
    uploaded: list[int] = []

    def fake_local(data: bytes, dpi=None, pages=None, filename=""):
        with fitz.open(stream=data, filetype="pdf") as doc:
            wanted = sorted(pages) if pages else list(range(1, doc.page_count + 1))
            read_locally.extend(wanted)
            return {n: doc[n - 1].get_text() for n in wanted}

    def fake_upload(data: bytes, _filename: str) -> list[str]:
        with fitz.open(stream=data, filetype="pdf") as doc:
            uploaded.append(doc.page_count)
            return [page.get_text() for page in doc]

    monkeypatch.setattr(tx.local_ocr, "ocr_pdf_page_texts", fake_local)
    monkeypatch.setattr(tx, "ocr_via_veris_pages", fake_upload)
    return read_locally, uploaded


def bundle_with_resume_at(position: int, total: int = 30) -> list[str]:
    """A `total`-page bundle whose two résumé pages start at `position`."""
    filler = [CERTIFICATE_PAGE, EXPERIENCE_LETTER_PAGE, PASSPORT_PAGE]
    pages = [filler[i % 3] for i in range(total)]
    pages[position - 1] = RESUME_PAGE
    pages[position] = RESUME_PAGE_TWO
    return pages


def test_every_page_of_a_scan_is_read_locally(scanned):
    """No page is skipped, whatever has already been found on an earlier one."""
    read_locally, _uploaded = scanned

    tx.extract_text(make_pdf(bundle_with_resume_at(15)), "Scan_2026.pdf")

    assert sorted(read_locally) == list(range(1, 31)), (
        "a page went unread — an Aadhaar or a passport behind the CV would be lost"
    )


def test_only_the_resume_pages_are_uploaded(scanned):
    """The bundle is 30 pages; exactly the 2 that hold the CV leave the host."""
    _read_locally, uploaded = scanned

    doc = tx.extract_text(make_pdf(bundle_with_resume_at(15)), "application.pdf")

    assert doc.resume_pages == [15, 16]
    assert uploaded == [2], f"{uploaded} page(s) were uploaded; only the CV's may be"


def test_a_document_that_is_not_a_resume_is_never_uploaded(scanned):
    """The whole complaint, in one assertion: junk mail costs nothing at Veris."""
    _read_locally, uploaded = scanned

    doc = tx.extract_text(make_pdf([INVOICE_PAGE] * 30), "cv.pdf")

    assert doc.is_resume is False
    assert doc.classification_confidence < 0.30
    assert uploaded == [], "a non-résumé reached a paid extraction endpoint"


def test_a_non_resume_is_still_read_in_full_before_it_is_rejected(scanned):
    """Rejection is a finding about content, so the content has to be read."""
    read_locally, _uploaded = scanned

    tx.extract_text(make_pdf([INVOICE_PAGE] * 30), "cv.pdf")

    assert sorted(read_locally) == list(range(1, 31))


def test_a_resume_deep_in_the_bundle_is_still_found(scanned):
    doc = tx.extract_text(make_pdf(bundle_with_resume_at(15)), "application.pdf")

    assert doc.is_resume is True
    assert doc.resume_pages == [15, 16]
    assert "EOT Crane Operator" in doc.resume_text


def test_a_readable_pdf_is_never_sent_to_ocr_at_all(monkeypatch):
    """The text layer is free; paying to re-read it caused the original timeouts."""
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    def explode(*_a, **_k):
        raise AssertionError("a PDF with a good text layer must not be sent to OCR")

    monkeypatch.setattr(tx, "ocr_via_veris_pages", explode)
    monkeypatch.setattr(tx.local_ocr, "ocr_pdf_page_texts", explode)

    doc = tx.extract_text(make_pdf([RESUME_PAGE, RESUME_PAGE_TWO]), "cv.pdf")
    assert doc.is_resume is True
    assert doc.ocr_used is False


def test_refinement_can_be_switched_off_entirely(scanned, monkeypatch):
    """An air-gapped or cost-capped deployment reads everything and sends nothing."""
    _read_locally, uploaded = scanned
    monkeypatch.setattr(settings, "veris_refine_resume_pages", False)

    doc = tx.extract_text(make_pdf(bundle_with_resume_at(15)), "application.pdf")

    assert doc.is_resume is True
    assert uploaded == []
