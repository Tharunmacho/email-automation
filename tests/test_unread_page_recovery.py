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


def test_pages_cut_by_the_local_ceiling_are_recovered_too(starved, monkeypatch):
    """OCR_MAX_PAGES bounds local CPU — it is not a reason to publish a hole.

    A page the ceiling refused and a page Tesseract fumbled arrive at the
    classifier identically: as an empty string it cannot score. The reason the
    text is missing does not change the question being asked of it.
    """
    _blank_out, uploaded = starved
    monkeypatch.setattr(settings, "ocr_max_pages", 10)

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded, "the pages past the ceiling were never offered to Veris"
    page = doc.pages[19]
    assert page.text.strip(), "the passport sat past the ceiling and stayed lost"
    assert pc.id_document_scores(page.text)[pc.PASSPORT] > 0


def test_recovery_stays_within_its_own_ceiling(starved, monkeypatch):
    """The cloud call is bounded even when the local one refused everything."""
    _blank_out, uploaded = starved
    monkeypatch.setattr(settings, "ocr_max_pages", 1)
    monkeypatch.setattr(settings, "veris_recover_max_pages", 5)

    tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded == [5], f"{uploaded} uploaded against a recovery ceiling of 5"


# --------------------------------------------------------------------------- #
#  A page that came back with *something* unusable
# --------------------------------------------------------------------------- #
"""The second half of the same failure, and the half that actually bites.

An empty page at least *looks* lost. What a starved host produces more often is
a page that answered only after `local_ocr` shrank it — observed in production
as ``Page 3 read as empty at 2550px but answered at 1600px: 81 chars
recovered``, on a bundle that then reported no passport.

Eighty-one characters of speckle is not a read, but it is not empty either, so
the recovery above never saw it, the classifier scored it zero for being under
`_MIN_PAGE_CHARS`, and `has_identity_hint` found no surviving word to justify a
second look. Every safety net was sized for text rather than for noise.
"""

#: What half a passport page looks like when Tesseract is shrunk until it
#: finishes: the right length, none of the words that carry meaning.
SPECKLE = "|.  ~ ,, l1I ]  ' `` ~-  .. i1| ,  ''  -~ .l  |] ,. `` ~~ ..  1I| '`"


@pytest.fixture
def fumbled(monkeypatch):
    """Local OCR that answers chosen pages with a degraded read.

    Yields ``(rescued, uploaded)``: map a page number to the text the local
    reader limped to, and it comes back tagged `tesseract:rescue` exactly as the
    half-size retry tags its own output.
    """
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "veris_refine_resume_pages", False)
    monkeypatch.setattr(settings, "ocr_max_pages", 300)
    monkeypatch.setattr(tx, "_page_layout_text", lambda _page: "")

    rescued: dict[int, str] = {}
    uploaded: list[int] = []

    def fake_local(data: bytes, dpi=None, pages=None, filename=""):
        with fitz.open(stream=data, filetype="pdf") as doc:
            wanted = sorted(pages) if pages else list(range(1, doc.page_count + 1))
            out = {}
            for n in wanted:
                if n in rescued:
                    out[n] = local_ocr.PageRead(
                        page_number=n, text=rescued[n], dpi=1600,
                        engine="tesseract:rescue",
                        quality=local_ocr.text_quality(rescued[n]),
                    )
                else:
                    text = doc[n - 1].get_text()
                    out[n] = local_ocr.PageRead(
                        page_number=n, text=text, dpi=settings.ocr_dpi,
                        engine="test", quality=local_ocr.text_quality(text),
                    )
            return out

    def fake_upload(data: bytes, _filename: str):
        from app.extraction.ocr import VerisRead

        with fitz.open(stream=data, filetype="pdf") as doc:
            uploaded.append(doc.page_count)
            return VerisRead([page.get_text() for page in doc])

    monkeypatch.setattr(tx.local_ocr, "ocr_pdf_page_reads", fake_local)
    monkeypatch.setattr(tx, "ocr_via_veris_read", fake_upload)
    return rescued, uploaded


def test_a_passport_that_only_answered_when_shrunk_is_still_found(fumbled):
    """The reported failure: 81 chars back from page 3, and no passport."""
    rescued, _uploaded = fumbled
    rescued[20] = SPECKLE

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    page = doc.pages[19]
    assert pc.id_document_scores(page.text)[pc.PASSPORT] > 0, (
        "the passport page kept its speckle — a degraded read is invisible to "
        "every gate downstream, which is how the page was lost in production"
    )


def test_a_page_too_short_to_classify_is_recovered(fumbled):
    """Under `_MIN_PAGE_CHARS` the classifier forms no verdict at all."""
    rescued, uploaded = fumbled
    rescued[20] = "Passport"  # 8 chars: a real word, and still unclassifiable

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded == [1], f"{uploaded}: the unclassifiable page was not fetched"
    assert pc.id_document_scores(doc.pages[19].text)[pc.PASSPORT] > 0


def test_only_the_degraded_pages_are_uploaded(fumbled):
    """Recovery still must not turn into "upload the bundle"."""
    rescued, uploaded = fumbled
    rescued[20] = SPECKLE
    rescued[21] = SPECKLE

    tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded == [2], f"{uploaded} page(s) uploaded; only the 2 degraded ones may be"


def test_a_sparse_identity_page_is_not_treated_as_degraded(fumbled):
    """The line that separates a failed read from a thin one.

    A passport page carries few words by design and scores under
    `ocr_page_quality_floor` on a *perfect* read. Recovering everything under
    that floor would upload every ID card in every bundle — the documents the
    local reader handled correctly.
    """
    _rescued, uploaded = fumbled

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded == [], (
        f"{uploaded} page(s) uploaded from a bundle that read fine; a sparse "
        "identity page is being mistaken for a failed one"
    )
    assert pc.id_document_scores(doc.pages[19].text)[pc.PASSPORT] > 0


def test_a_worse_cloud_read_does_not_overwrite_the_local_one(fumbled, monkeypatch):
    """Recovery may improve a page. It may never make one worse."""
    rescued, _uploaded = fumbled
    rescued[20] = "Republic of India, Passport No. M4471902, Date of Expiry"

    def worse_upload(data: bytes, _filename: str):
        from app.extraction.ocr import VerisRead

        return VerisRead([SPECKLE])

    monkeypatch.setattr(tx, "ocr_via_veris_read", worse_upload)

    doc = tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert "M4471902" in doc.pages[19].text, (
        "the local read was thrown away for a cloud read that is plainly worse"
    )


def test_degraded_recovery_can_be_switched_off(fumbled, monkeypatch):
    """One flag, for a deployment where the cloud calls are the constraint."""
    rescued, uploaded = fumbled
    rescued[20] = SPECKLE
    monkeypatch.setattr(settings, "veris_recover_degraded_pages", False)

    tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert uploaded == [], f"{uploaded} uploaded with VERIS_RECOVER_DEGRADED_PAGES off"


def test_a_recovered_page_is_not_re_read_locally_at_high_dpi(fumbled, monkeypatch):
    """Speed: the cloud already answered it, so the 450-DPI pass is waste.

    A host that could not finish a page at 300 DPI will not finish it at 450 —
    2.25x the pixels for the same empty string — and once Veris has read the
    page there is nothing left for a local re-read to add.
    """
    rescued, _uploaded = fumbled
    rescued[20] = SPECKLE
    monkeypatch.setattr(settings, "ocr_deep_read_enabled", True)

    deepened: list[list[int]] = []

    def spy(data, pages, filename, dpi=None, label=""):
        deepened.append(sorted(pages))
        return {}

    monkeypatch.setattr(tx.local_ocr, "single_pass_pages", spy)

    tx.extract_text(make_pdf(bundle_with_passport_at(20)), "Full Docs.pdf")

    assert all(20 not in batch for batch in deepened), (
        f"page 20 was re-read locally at high DPI after Veris had answered it: {deepened}"
    )
