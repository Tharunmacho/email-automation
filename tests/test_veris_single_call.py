"""One résumé, one Veris call.

Locating the résumé sends its pages to the Veris résumé endpoint for a better
read. The parser then sent *the same pages* to *the same endpoint* again for the
structured fields — a second upload, a second extraction, a second wait, billed
separately, differing from the first only in idempotency key (`resume` against
`resume_parse`). A real 32-page bundle showed both in the log, forty seconds
apart, for one candidate.

The job's answer carries the fields alongside the text it was fetched for, so
the second call was always buying something it already had.
"""
from __future__ import annotations

import pytest

from app.core.models import ExtractedDocument


def test_the_read_carries_the_whole_payload():
    """`VerisRead.result` is what makes one call able to answer both questions."""
    from app.extraction.ocr import VerisRead

    read = VerisRead(pages=["page one"], result={"pages": [{"text": "page one"}], "name": "A"})

    assert read.pages == ["page one"]
    assert read.result["name"] == "A"


def test_a_read_with_no_job_payload_is_still_valid():
    """A synchronous call or a local fallback has no job payload, and the parser
    must then do exactly what it always did rather than see an empty extraction."""
    from app.extraction.ocr import VerisRead

    assert VerisRead(pages=["x"]).result is None


def test_the_extracted_document_carries_it_to_the_parser():
    doc = ExtractedDocument(text="cv", method="pdf_ocr", veris_resume_result={"name": "A"})

    assert doc.veris_resume_result == {"name": "A"}


def test_it_defaults_to_absent():
    """Backward compatible: every existing construction site omits the field."""
    assert ExtractedDocument(text="cv", method="pdf_text").veris_resume_result is None


def test_the_refine_pass_hands_back_what_the_job_returned(monkeypatch):
    """The text goes to the classifier and the payload goes to the parser, from
    the one call."""
    from app.config import settings
    from app.extraction import text_extractor as tx
    from app.extraction.ocr import VerisRead

    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "veris_refine_resume_pages", True)

    calls = []

    def one_call(data, filename):
        calls.append(filename)
        return VerisRead(pages=["a much better read of page one"], result={"name": "A"})

    monkeypatch.setattr(tx, "ocr_via_veris_read", one_call)

    texts, payload = tx._refine_resume_pages(b"%PDF-fake", "cv.pdf", ["local read"], [1])

    assert len(calls) == 1, "the refine pass made more than one call"
    assert texts[0] == "a much better read of page one"
    assert payload == {"name": "A"}, "the job payload was discarded"


def test_a_failed_refine_reports_no_payload(monkeypatch):
    """A failure must not hand the parser a half-answer it would treat as final —
    it has to fall through to its own call."""
    from app.config import settings
    from app.extraction import text_extractor as tx

    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "veris_refine_resume_pages", True)

    def boom(_data, _filename):
        raise RuntimeError("503 from Veris")

    monkeypatch.setattr(tx, "ocr_via_veris_read", boom)

    texts, payload = tx._refine_resume_pages(b"%PDF-fake", "cv.pdf", ["local read"], [1])

    assert texts == ["local read"], "the local read must still stand"
    assert payload is None


def test_refinement_switched_off_uploads_nothing_and_returns_no_payload(monkeypatch):
    from app.config import settings
    from app.extraction import text_extractor as tx

    monkeypatch.setattr(settings, "veris_refine_resume_pages", False)

    def explode(*_a, **_k):
        raise AssertionError("nothing may be uploaded when refinement is off")

    monkeypatch.setattr(tx, "ocr_via_veris_read", explode)

    texts, payload = tx._refine_resume_pages(b"%PDF-fake", "cv.pdf", ["local"], [1])
    assert texts == ["local"]
    assert payload is None


def test_ocr_via_veris_pages_still_returns_plain_text(monkeypatch):
    """The old entry point is unchanged for callers that never wanted the fields."""
    from app.extraction import ocr
    from app.extraction.ocr import VerisRead

    monkeypatch.setattr(ocr, "ocr_via_veris_read",
                        lambda d, f: VerisRead(pages=["one", "two"], result={"name": "A"}))

    assert ocr.ocr_via_veris_pages(b"x", "cv.pdf") == ["one", "two"]
