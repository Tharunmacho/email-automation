"""An Aadhaar or passport sent as its own file beside the CV reaches its endpoint.

Candidates commonly send three files — `cv.pdf`, `aadhaar.jpg`, `passport.pdf`.
Multipass only ever looked inside the résumé's own file, so each ID file was
read, correctly judged "not a résumé", and dropped: the documents a recruiter
needs for a visa file never reached the Aadhaar or passport endpoint at all.

The rule these tests pin: content decides, never the filename, and nothing is
extracted unless a résumé in the same email produced the candidate.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.core.models import (
    Attachment, CandidateProfile, EmailMessage, ExtractedDocument, PageText,
)
from app.ingestion import multipass
from app.ingestion import pipeline as pl
from app.ingestion.pipeline import IngestionPipeline
from tests.test_pipeline_redelivery import FakeLedger, FakeRepo, FakeStorage

CV = b"%PDF-1.4 cv bytes"
ID_SCAN = b"%PDF-1.4 id scan bytes"
CERT = b"%PDF-1.4 certificate bytes"


class ContentParser:
    """Judges each file by its bytes — the filenames below are deliberately useless."""

    def parse_file(self, data, filename):
        if data == CV:
            profile = CandidateProfile(
                is_resume=True, confidence=0.9, full_name="Ravi Kumar",
                email="ravi@example.com", phone="+91 98765 43210",
            )
            return profile, ExtractedDocument(
                text="Ravi Kumar", method="pdf_text", char_count=10,
                pages=[PageText(page_number=1, text="Ravi Kumar CV")],
            )
        text = "AADHAAR 1234 5678 9012" if data == ID_SCAN else "Certificate of completion"
        extracted = ExtractedDocument(
            text=text, method="pdf_ocr", char_count=len(text), is_resume=False,
            pages=[PageText(page_number=1, text=text)],
        )
        return CandidateProfile(is_resume=False, confidence=0.1), extracted


class _Classification:
    def __init__(self, text):
        is_id = text.startswith("AADHAAR")
        self.aadhaar_pages = [1] if is_id else []
        self.passport_pages = []
        self.document_pages = []
        self.foreign_passport_pages = []


@pytest.fixture
def routed(monkeypatch):
    """Records every identity extraction the pipeline asks for."""
    monkeypatch.setattr(settings, "veris_ocr_api_key", "test-key")
    monkeypatch.setattr(settings, "multipass_extraction_enabled", True)
    monkeypatch.setattr(settings, "auto_reply_enabled", False)
    monkeypatch.setattr(settings, "auto_assign_enabled", False)
    monkeypatch.setattr(pl.pc, "classify_multipass",
                        lambda texts: _Classification(texts[0]))
    monkeypatch.setattr(IngestionPipeline, "_announce", lambda *a, **k: None)

    calls = []

    class FakeExtractor:
        def run(self, page_texts, data, **kw):
            calls.append({"filename": kw["filename"], "candidate_id": kw["candidate_id"],
                          "data": data})
            result = multipass.MultipassResult()
            if kw.get("classification") is not None and kw["classification"].aadhaar_pages:
                result.passes.append(multipass.PassResult(
                    mode="aadhaar", pages=[1], status="succeeded"))
            return result

    monkeypatch.setattr(multipass, "MultipassExtractor", FakeExtractor)
    return calls


def _email(*files):
    return EmailMessage(
        message_id="hr@findurjob.com:77", thread_id="t", from_addr="ravi@example.com",
        from_name="Ravi", subject="Fwd:", body_text="",
        attachments=[
            Attachment(filename=name, mime_type="application/pdf", size=len(data),
                       attachment_id=f"att-{i}", data=data)
            for i, (name, data) in enumerate(files)
        ],
    )


def _pipeline():
    return IngestionPipeline(repository=FakeRepo(), storage=FakeStorage(),
                             parser=ContentParser(), ledger=FakeLedger())


def test_a_separate_aadhaar_file_goes_to_the_aadhaar_endpoint(routed):
    # The ID scan comes *first* and has a meaningless name: order and filename
    # must not matter, only content.
    result = _pipeline().process_email(_email(("scan_001.pdf", ID_SCAN), ("doc.pdf", CV)))

    assert result.status == "processed"
    candidate = result.ingested_ids[0]
    assert [c["filename"] for c in routed if c["filename"] == "scan_001.pdf"], (
        "the Aadhaar sent as its own file was never routed to identity extraction"
    )
    call = next(c for c in routed if c["filename"] == "scan_001.pdf")
    assert call["candidate_id"] == candidate
    assert call["data"] == ID_SCAN

    by_name = {a.filename: a for a in result.attachments}
    assert by_name["scan_001.pdf"].status == "identity_document"
    assert by_name["scan_001.pdf"].candidate_id == candidate


def test_an_id_document_alone_is_never_extracted(routed):
    """No résumé, no extraction — an ID on its own creates nothing."""
    result = _pipeline().process_email(_email(("scan_001.pdf", ID_SCAN)))

    assert not result.ingested_ids
    assert routed == []
    assert result.attachments[0].status == "not_resume"


def test_a_certificate_beside_the_cv_is_not_uploaded_anywhere(routed):
    result = _pipeline().process_email(_email(("doc.pdf", CV), ("resume.pdf", CERT)))

    assert result.ingested_ids
    assert not [c for c in routed if c["filename"] == "resume.pdf"], (
        "a certificate named resume.pdf was sent for identity extraction"
    )
    by_name = {a.filename: a for a in result.attachments}
    assert by_name["resume.pdf"].status == "not_resume"


def test_the_read_is_not_kept_after_the_message(routed):
    """Bytes and OCR text are for the sibling pass only, never carried out."""
    result = _pipeline().process_email(_email(("doc.pdf", CV), ("scan_001.pdf", ID_SCAN)))

    for att in result.attachments:
        assert att.data is None and att.extracted is None and att.source is None
