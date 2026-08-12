"""A scanned bundle must never be sent to OCR as one giant call.

A real 9-page 1.6 MB scan timed out at 180 seconds because the whole file went
to Veris in a single request, and the candidate was lost. A scan has no text
layer, so the pages holding the résumé cannot be picked before *something* has
been read — the answer is to read it a couple of pages at a time and stop as
soon as the résumé turns up.
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
    """Make every PDF look like a pure scan, and stand in for the cloud OCR.

    `_page_layout_text` returning "" is what a scan looks like to PyMuPDF. The
    fake OCR reads the text off whatever subset PDF it is handed, so page
    subsetting and page numbering are exercised for real; only the network is
    faked. Returns the list of per-call page counts.
    """
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(tx, "_page_layout_text", lambda _page: "")

    calls: list[int] = []

    def fake_ocr(data: bytes, _filename: str) -> list[str]:
        with fitz.open(stream=data, filetype="pdf") as doc:
            calls.append(doc.page_count)
            return [page.get_text() for page in doc]

    monkeypatch.setattr(tx, "ocr_via_veris_pages", fake_ocr)
    return calls


def bundle_with_resume_at(position: int, total: int = 30) -> list[str]:
    """A `total`-page bundle whose two résumé pages start at `position`."""
    filler = [CERTIFICATE_PAGE, EXPERIENCE_LETTER_PAGE, PASSPORT_PAGE]
    pages = [filler[i % 3] for i in range(total)]
    pages[position - 1] = RESUME_PAGE
    pages[position] = RESUME_PAGE_TWO
    return pages


def test_no_single_ocr_call_exceeds_the_chunk_size(scanned):
    doc = tx.extract_text(make_pdf(bundle_with_resume_at(15)), "Scan_2026.pdf")

    assert scanned, "OCR never ran"
    assert max(scanned) <= settings.ocr_chunk_pages, (
        f"a call carried {max(scanned)} pages; the timeout came from exactly this"
    )
    assert doc.is_resume is True


def test_ocr_stops_as_soon_as_the_resume_is_found(scanned):
    """A CV on pages 1-2 of a 30-page bundle must not cost 30 pages of OCR.

    The floor is one chunk, not two pages: nothing can be classified before a
    chunk has been read, so `ocr_chunk_pages` is the granularity at which
    stopping is possible. Asserted against the setting rather than a constant,
    because that is the actual guarantee — tuning the chunk size should not
    require editing this test to keep it true.
    """
    doc = tx.extract_text(make_pdf(bundle_with_resume_at(1)), "01.pdf")

    assert doc.resume_pages == [1, 2]
    assert sum(scanned) <= settings.ocr_chunk_pages, (
        f"read {sum(scanned)} pages to find a resume on page 1"
    )


def test_a_resume_deep_in_the_bundle_is_still_reached(scanned):
    """Stopping early must never mean stopping short of the CV."""
    doc = tx.extract_text(make_pdf(bundle_with_resume_at(15)), "application.pdf")

    assert doc.is_resume is True
    assert doc.resume_pages == [15, 16]
    assert "EOT Crane Operator" in doc.resume_text
    # It read up to the resume and then stopped, rather than the whole bundle.
    assert sum(scanned) < 30


def test_a_scan_that_is_not_an_application_gives_up_early(scanned):
    """Invoices are not supporting documents, so no CV is coming behind them."""
    doc = tx.extract_text(make_pdf([INVOICE_PAGE] * 30), "cv.pdf")

    assert doc.is_resume is False
    assert doc.classification_confidence < 0.30
    assert sum(scanned) <= settings.ocr_give_up_pages + settings.ocr_chunk_pages, (
        f"read {sum(scanned)} pages of an invoice before giving up"
    )


def test_certificates_do_not_trigger_the_give_up(scanned):
    """Fourteen certificates in front of the CV is a normal trade application."""
    doc = tx.extract_text(make_pdf(bundle_with_resume_at(15)), "bundle.pdf")

    assert doc.resume_pages == [15, 16]


def test_a_readable_pdf_is_never_sent_to_ocr_at_all(monkeypatch):
    """The text layer is free; paying the cloud to re-read it caused timeouts."""
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    def explode(*_a, **_k):
        raise AssertionError("a PDF with a good text layer must not be sent to OCR")

    monkeypatch.setattr(tx, "ocr_via_veris_pages", explode)
    monkeypatch.setattr(tx, "ocr_pdf_page_texts", explode)

    doc = tx.extract_text(make_pdf([RESUME_PAGE, RESUME_PAGE_TWO]), "cv.pdf")
    assert doc.is_resume is True
    assert doc.ocr_used is False
