"""A résumé buried on page 25 of a 50-page bundle, read 10 pages at a time.

This is the case the whole progressive-OCR design exists for. A candidate
scans everything they own into one PDF — certificates, marksheets, an ID page,
then the CV, then more certificates — and the CV is the only part worth
parsing. It cannot be found without reading, and reading all fifty pages of a
scan is both slow enough to time out and expensive enough to notice.

So the guarantees under test are:

  * every page inside a chunk is read individually — no page's text is lost;
  * chunk boundaries are 10 pages, so pages 21-30 are read as one unit;
  * once the CV is located on 25-26, the scan stops — 31-50 never reach OCR;
  * only the CV's pages are handed to the LLM.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.extraction import page_classifier as pc
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
def scan(monkeypatch):
    """Make every PDF look like a pure scan and record what OCR actually saw.

    Only the network is faked. The stand-in reads its text off whatever subset
    PDF it is handed, so page carving and page numbering are exercised for real
    — if the chunker asked for the wrong pages, the text would come back wrong.

    Yields the list of 1-based page numbers sent to OCR, in order.
    """
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    # A scan has no text layer; this is what PyMuPDF sees on one.
    monkeypatch.setattr(tx, "_page_layout_text", lambda _page: "")

    seen_pages: list[int] = []
    real_subset = tx.pdf_pages.subset_pdf

    def spy_subset(data: bytes, pages):
        wanted = sorted({int(p) for p in pages})
        seen_pages.extend(wanted)
        return real_subset(data, wanted)

    monkeypatch.setattr(tx.pdf_pages, "subset_pdf", spy_subset)

    def fake_ocr(data: bytes, _filename: str) -> list[str]:
        with fitz.open(stream=data, filetype="pdf") as doc:
            return [page.get_text() for page in doc]

    monkeypatch.setattr(tx, "ocr_via_veris_pages", fake_ocr)
    return seen_pages


def deep_bundle(total: int = 50, resume_at: int = 25) -> list[str]:
    """`total` pages of supporting documents, with a two-page CV at `resume_at`."""
    filler = [CERTIFICATE_PAGE, EXPERIENCE_LETTER_PAGE, PASSPORT_PAGE]
    pages = [filler[i % 3] for i in range(total)]
    pages[resume_at - 1] = RESUME_PAGE
    pages[resume_at] = RESUME_PAGE_TWO
    return pages


# --------------------------------------------------------------------------- #
#  The 50-page bundle
# --------------------------------------------------------------------------- #
def test_resume_on_page_25_is_found_and_the_tail_is_never_scanned(scan):
    doc = tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    assert doc.is_resume is True
    assert doc.resume_pages == [25, 26]

    # The whole point: the scan stopped at the end of the chunk holding the CV.
    assert max(scan) <= 30, f"OCR reached page {max(scan)}; pages 31-50 must be skipped"
    assert not [p for p in scan if p > 30]


def test_it_reads_in_ten_page_chunks(scan):
    """Pages 1-10, 11-20, 21-30 — three chunks, in order, nothing in between."""
    tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    assert settings.ocr_chunk_pages == 10
    assert scan == list(range(1, 31))


def test_every_page_in_a_chunk_is_read_individually(scan):
    """Zero data loss: each of the 30 pages read comes back with its own text."""
    doc = tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    read = [p for p in doc.pages if p.page_number <= 30]
    assert len(read) == 30
    assert all(p.text.strip() for p in read), (
        "a page inside a chunk came back empty — its text was dropped"
    )
    # And each page was classified on its own evidence, not the bundle's.
    assert {p.kind for p in read} > {pc.RESUME}


def test_only_the_resume_pages_go_to_the_llm(scan):
    """The LLM sees pages 25-26 and nothing else — not the 28 pages around them."""
    doc = tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    assert "EOT Crane Operator" in doc.resume_text
    # Supporting documents were read, kept in `text`, and excluded from the slice.
    assert "this is to certify that" not in doc.resume_text.lower()
    assert "to whom it may concern" not in doc.resume_text.lower()
    assert doc.resume_text != doc.text


def test_nothing_read_is_thrown_away(scan):
    """`text` keeps every page that was read, even the ones the LLM never sees."""
    doc = tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    assert "this is to certify that" in doc.text.lower()
    assert len(doc.text) > len(doc.resume_text)


# --------------------------------------------------------------------------- #
#  The budget, and the other direction
# --------------------------------------------------------------------------- #
def test_the_page_budget_supports_a_sixty_page_document():
    assert settings.ocr_max_pages == 60


def test_a_resume_past_the_budget_stops_at_the_budget(scan):
    """A 200-page mis-send costs 60 pages, not 200 — and says so."""
    tx.extract_text(make_pdf(deep_bundle(total=200, resume_at=150)), "huge.pdf")

    assert max(scan) <= settings.ocr_max_pages


def test_a_bundle_that_is_not_an_application_still_gives_up_early(scan):
    """Ten-page chunks must not defeat the give-up: invoices lead nowhere."""
    doc = tx.extract_text(make_pdf([INVOICE_PAGE] * 50), "cv.pdf")

    assert doc.is_resume is False
    # One chunk is the floor — nothing can be judged before it has been read.
    assert max(scan) <= settings.ocr_chunk_pages
