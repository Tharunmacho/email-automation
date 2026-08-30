"""A 50-page application bundle, read end to end.

A candidate scans everything they own into one PDF — certificates, marksheets,
an Aadhaar page, the CV, then more certificates — in whatever order the pages
came off the scanner. Two things have to be true of that file:

  * **every page is read.** The old pass stopped as soon as something scored as
    a CV, which is exactly why the identity documents behind it were never
    extracted, and why a CV sitting past the stopping point was missed outright.
  * **only the CV is treated as the CV.** The LLM sees the résumé pages; the
    certificates around them are kept in the full text and excluded from the
    slice that becomes a candidate profile.

Reading is local and therefore cheap in the only currency that matters here:
nothing in this file is billed to an external API.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.extraction import local_ocr
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
    """Make every PDF look like a pure scan and record which pages were read.

    Only Tesseract and the network are faked. The stand-in reader takes its text
    off the real pages it is handed, so page numbering is exercised for real —
    if the wrong pages were requested, the text would come back wrong.

    Yields the list of 1-based page numbers that were read.
    """
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    # A scan has no text layer; this is what PyMuPDF sees on one.
    monkeypatch.setattr(tx, "_page_layout_text", lambda _page: "")

    seen_pages: list[int] = []

    def fake_local(data: bytes, dpi=None, pages=None, filename=""):
        with fitz.open(stream=data, filetype="pdf") as doc:
            wanted = sorted(pages) if pages else list(range(1, doc.page_count + 1))
            seen_pages.extend(wanted)
            texts = {n: doc[n - 1].get_text() for n in wanted}
        # The real reading quality, not a flattering constant: these pages read
        # cleanly, so none of them should qualify for the escalation pass, and a
        # stub that claimed otherwise would hide it if one did.
        return {
            n: local_ocr.PageRead(
                page_number=n,
                text=text,
                dpi=settings.ocr_dpi,
                engine="test",
                quality=local_ocr.text_quality(text),
            )
            for n, text in texts.items()
        }

    def fake_upload(data: bytes, _filename: str):
        from app.extraction.ocr import VerisRead

        with fitz.open(stream=data, filetype="pdf") as doc:
            return VerisRead([page.get_text() for page in doc])

    monkeypatch.setattr(tx.local_ocr, "ocr_pdf_page_reads", fake_local)
    monkeypatch.setattr(tx, "ocr_via_veris_read", fake_upload)
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
def test_the_resume_is_found_and_the_tail_is_still_read(scan):
    doc = tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    assert doc.is_resume is True
    assert doc.resume_pages == [25, 26]
    # The tail is where the Aadhaar and the passport live. Finding the CV is not
    # a reason to stop looking at the rest of the bundle.
    assert max(scan) == 50, f"reading stopped at page {max(scan)}"


def test_every_page_is_read_exactly_once(scan):
    tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    assert sorted(scan) == list(range(1, 51))
    assert len(scan) == len(set(scan)), "a page was read twice"


def test_every_page_comes_back_with_its_own_text(scan):
    """Zero data loss: each of the 50 pages carries its own content."""
    doc = tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    assert len(doc.pages) == 50
    assert all(p.text.strip() for p in doc.pages), (
        "a page came back empty — its text was dropped"
    )
    # And each page was classified on its own evidence, not the bundle's.
    assert {p.kind for p in doc.pages} > {pc.RESUME}


def test_only_the_resume_pages_go_to_the_llm(scan):
    """The LLM sees pages 25-26 and nothing else — not the 48 pages around them."""
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


def test_the_identity_pages_behind_the_resume_survive_to_the_classifier(scan):
    """The reason full coverage matters: multipass can only route what was read."""
    doc = tx.extract_text(make_pdf(deep_bundle()), "Scan_2026.pdf")

    found = pc.classify_multipass([p.text for p in doc.pages])
    behind_the_cv = [n for n in found.passport_pages + found.foreign_passport_pages if n > 26]
    assert behind_the_cv, "no identity page behind the CV was seen at all"


# --------------------------------------------------------------------------- #
#  The safety ceiling, and the other direction
# --------------------------------------------------------------------------- #
def test_the_page_ceiling_is_a_safety_net_not_a_budget():
    """Local reading is CPU, not billing, so the ceiling sits above real bundles."""
    assert settings.ocr_max_pages >= 200


def test_a_document_past_the_ceiling_is_truncated_and_says_so(scan, monkeypatch, caplog):
    monkeypatch.setattr(settings, "ocr_max_pages", 20)

    with caplog.at_level("WARNING"):
        tx.extract_text(make_pdf(deep_bundle(total=60, resume_at=50)), "huge.pdf")

    assert max(scan) == 20
    assert any("safety ceiling" in record.message for record in caplog.records), (
        "truncation must never be silent"
    )


def test_a_bundle_that_is_not_an_application_is_read_and_then_rejected(scan):
    """No early give-up: the verdict is reached from the whole document."""
    doc = tx.extract_text(make_pdf([INVOICE_PAGE] * 50), "cv.pdf")

    assert doc.is_resume is False
    assert sorted(scan) == list(range(1, 51))
