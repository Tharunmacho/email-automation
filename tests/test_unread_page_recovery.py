"""A page local OCR could not read is offered to Veris, not dropped.

The production failure this pins down: a 28-page application bundle produced a
résumé and an Aadhaar record but no passport, and the only trace was a run of
``Page N ignored: page.signals.chars (0) < _MIN_PAGE_CHARS (40)`` lines in the
classifier log.

Nothing was wrong with the passport page. Local OCR had returned "" for it —
under CPU pressure a Tesseract pass hits its timeout and comes back empty — and
an empty page is indistinguishable from a blank sheet by the time it reaches the
classifier. It scored nothing, so it never entered ``passport_pages``, so it was
never sent to the passport endpoint. Local OCR is the gatekeeper for identity
documents, and a gatekeeper that fails silently loses the document.

That it was a race and not a property of the file is the other half of the
evidence: the *same* bundle read twice fifteen minutes apart gave 147 and 312
characters on two pages the first time and zero on both the second.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.extraction import local_ocr
from app.extraction import page_classifier as pc
from app.extraction import text_extractor as tx
from tests.test_page_classifier import (
    CERTIFICATE_PAGE,
    PASSPORT_PAGE,
    RESUME_PAGE,
    RESUME_PAGE_TWO,
)
from tests.test_resume_location import make_pdf

fitz = pytest.importorskip("fitz", reason="PyMuPDF is needed to build test PDFs")


@pytest.fixture
def starved(monkeypatch):
    """Local OCR that returns "" for chosen pages, as a timed-out pass does.

    Yields ``(blank_out, uploaded)``: add page numbers to ``blank_out`` to make
    the local reader fail on them, and read ``uploaded`` for the page counts of
    every payload that left the host.
    """
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "veris_refine_resume_pages", False)
    # conftest caps this at 10 for speed; these bundles are 28 pages.
    monkeypatch.setattr(settings, "ocr_max_pages", 300)
    monkeypatch.setattr(tx, "_page_layout_text", lambda _page: "")

    blank_out: set[int] = set()
    uploaded: list[int] = []

    def fake_local(data: bytes, dpi=None, pages=None, filename=""):
        with fitz.open(stream=data, filetype="pdf") as doc:
            wanted = sorted(pages) if pages else list(range(1, doc.page_count + 1))
            texts = {
                n: ("" if n in blank_out else doc[n - 1].get_text()) for n in wanted
            }
        return {
            n: local_ocr.PageRead(
                page_number=n, text=t, dpi=settings.ocr_dpi,
                engine="test", quality=local_ocr.text_quality(t),
            )
            for n, t in texts.items()
        }

    def fake_upload(data: bytes, _filename: str):
        from app.extraction.ocr import VerisRead

        with fitz.open(stream=data, filetype="pdf") as doc:
            uploaded.append(doc.page_count)
            return VerisRead([page.get_text() for page in doc])

    monkeypatch.setattr(tx.local_ocr, "ocr_pdf_page_reads", fake_local)
    monkeypatch.setattr(tx, "ocr_via_veris_read", fake_upload)
    return blank_out, uploaded


def bundle_with_passport_at(passport: int, total: int = 28) -> list[str]:
    """A `total`-page bundle: CV on pages 1-2, a passport at `passport`."""
    pages = [CERTIFICATE_PAGE] * total
    pages[0] = RESUME_PAGE
    pages[1] = RESUME_PAGE_TWO
    pages[passport - 1] = PASSPORT_PAGE
    return pages


def test_a_passport_local_ocr_could_not_read_is_still_found(starved):
    """The production bug: page 20 read as "" and the passport vanished."""
    blank_out, _uploaded = starved
    blank_out.add(20)

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    page = doc.pages[19]
    assert page.text.strip(), (
        "page 20 is still empty — a passport local OCR fumbled is lost silently, "
        "which is exactly the reported failure"
    )
    assert pc.id_document_scores(page.text)[pc.PASSPORT] > 0, (
        "the recovered page does not read as a passport, so it will never be "
        "routed to the passport endpoint"
    )


def test_only_the_unread_pages_are_uploaded(starved):
    """Recovery must not turn into "upload the bundle"."""
    blank_out, uploaded = starved
    blank_out.update({20, 21})

    tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded == [2], (
        f"{uploaded} page(s) were uploaded; only the 2 unread ones may be"
    )


def test_a_clean_bundle_uploads_nothing(starved):
    """The common case stays free: nothing unread, no call."""
    _blank_out, uploaded = starved

    tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded == [], f"{uploaded} page(s) uploaded from a bundle that read fine"


def test_recovery_is_skipped_when_veris_is_not_configured(starved, monkeypatch):
    """No key, no upload — the local read still stands, and says so."""
    blank_out, uploaded = starved
    blank_out.add(20)
    monkeypatch.setattr(settings, "veris_ocr_api_key", "")

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded == []
    assert doc.pages[19].text.strip() == ""


def test_a_genuinely_blank_page_stays_blank(starved, monkeypatch):
    """Veris returning nothing either is a finding, not a crash."""
    blank_out, _uploaded = starved
    blank_out.add(20)

    def empty_upload(data: bytes, _filename: str):
        from app.extraction.ocr import VerisRead

        return VerisRead([""])

    monkeypatch.setattr(tx, "ocr_via_veris_read", empty_upload)

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert doc.pages[19].text.strip() == ""
    assert doc.is_resume is True, "the rest of the bundle must survive one blank page"


def test_a_failing_veris_call_does_not_lose_the_local_read(starved, monkeypatch):
    """The résumé still comes through when recovery itself falls over."""
    blank_out, _uploaded = starved
    blank_out.add(20)

    def boom(_data: bytes, _filename: str):
        raise RuntimeError("veris is down")

    monkeypatch.setattr(tx, "ocr_via_veris_read", boom)

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert doc.is_resume is True
    assert doc.resume_pages == [1, 2]
