"""Non-CVs get their real skip reason, without changing candidate eligibility."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.exceptions import TextExtractionError
from app.core.models import Attachment, CandidateProfile, EmailMessage, ExtractedDocument, PageText
from app.extraction import page_classifier as pc
from app.ingestion import pipeline as pipeline_module
from app.ingestion.pipeline import AttachmentResult, IngestionPipeline
from app.ingestion.rejection_reason import not_resume_reason
from tests.test_pipeline_redelivery import FakeLedger, FakeRepo, FakeStorage


VISA = "Visa No. 12345\nWork - عمل\nVisa Type\nNationality India\nPassport No. X1234567\nVisa Fees"


def document(text=VISA, *, kind=pc.ID_DOCUMENT, is_resume=False):
    return ExtractedDocument(
        text=text, method="pdf_text", char_count=len(text), is_resume=is_resume,
        pages=[PageText(page_number=1, text=text, kind=kind)],
        classification_reason="no resume pages",
    )


@pytest.mark.parametrize("text, phrase", [
    (VISA, "a work visa"),
    (VISA.replace(" ", "\u00a0"), "a work visa"),
    ("Visa No. 12345\nنوع التأشيرةWork ‐ أساسي عمل\nVisa Type\nVisa Fees", "a work visa"),
    ("Visa Type: Work\nVisa Number 12345", "a work visa"),
    ("Visa Type: Tourist\nVisa Number 12345", "a visa"),
])
def test_visa_reason_uses_form_fields(text, phrase):
    assert not_resume_reason(document(text), "fallback") == (
        f"This attachment is {phrase}, not a resume/CV. Only resumes and CVs are ingested."
    )


@pytest.mark.parametrize("kind, phrase", [
    (pc.PASSPORT, "a passport"), (pc.AADHAAR, "an Aadhaar document"),
    (pc.CERTIFICATE, "a certificate"), (pc.EXPERIENCE_LETTER, "an experience letter"),
    (pc.ID_DOCUMENT, "an identity document"),
])
def test_known_non_resume_document_reason(kind, phrase):
    assert f"is {phrase}, not a resume/CV" in not_resume_reason(document("example", kind=kind), "fallback")


def test_unknown_non_cv_does_not_invent_a_document_type():
    reason = not_resume_reason(document("I work abroad and require a visa", kind=pc.UNKNOWN), "fallback")
    assert reason == "No resume/CV content was found in this attachment. Only resumes and CVs are ingested."


@pytest.mark.parametrize("extracted", [None, document(is_resume=True), document(is_resume=None)])
def test_validation_reason_is_preserved_when_document_is_not_a_confirmed_non_cv(extracted):
    reason = "Resume is missing candidate email and phone."
    assert not_resume_reason(extracted, reason) == reason


def parts(monkeypatch, parser):
    repo, storage = FakeRepo(), FakeStorage()
    pipe = IngestionPipeline(repo, storage, parser, FakeLedger())
    attachment = Attachment(filename="application.pdf", mime_type="application/pdf",
                            size=3, attachment_id="att-1", data=b"pdf")
    email = EmailMessage(message_id="m1", thread_id="t1", from_addr="sender@example.com",
                         attachments=[attachment])
    monkeypatch.setattr(pipeline_module, "detect", lambda _: SimpleNamespace(
        is_candidate=True, resume_attachments=[attachment], reason="application attachment"))
    notify = Mock()
    monkeypatch.setattr(pipeline_module, "notify_candidate_rejected", notify)
    return pipe, email, notify


def test_visa_not_ingested_and_not_misreported_as_foreign_candidate(monkeypatch):
    extracted = document()
    extracted.nationality_accepted = False  # even old bad nationality metadata must not win
    extracted.nationality_reason = "rejected: other nationality"
    parser = SimpleNamespace(parse_file=lambda *_: (CandidateProfile(is_resume=False), extracted))
    pipe, email, notify = parts(monkeypatch, parser)

    result = pipe.process_email(email)

    assert result.status == "skipped"
    assert result.attachments[0].status == "not_resume"
    assert pipe.repo.records == {} and pipe.storage.saved == {}
    notify.assert_called_once_with(
        reason="This attachment is a work visa, not a resume/CV. Only resumes and CVs are ingested.",
        filename="application.pdf", from_addr="sender@example.com", title="Attachment not ingested",
    )


def test_supporting_document_filed_with_cv_has_no_rejection_notification(monkeypatch):
    pipe, email, notify = parts(monkeypatch, SimpleNamespace())
    monkeypatch.setattr(pipe, "_process_attachment", lambda *_: AttachmentResult(
        "passport.pdf", "not_resume", detail="passport"))

    def attach(_email, results):
        results[0].status = "identity_document"
        results[0].candidate_id = "candidate-from-cv"

    monkeypatch.setattr(pipe, "_attach_sibling_identity_documents", attach)
    pipe.process_email(email)
    notify.assert_not_called()


def test_unreadable_document_stays_retryable_without_permanent_rejection(monkeypatch):
    parser = SimpleNamespace(parse_file=Mock(side_effect=TextExtractionError("OCR unavailable")))
    pipe, email, notify = parts(monkeypatch, parser)
    assert pipe.process_email(email).status == "error"
    notify.assert_not_called()


def test_supporting_document_waits_for_unreadable_cv_before_final_notification(monkeypatch):
    pipe, email, notify = parts(monkeypatch, SimpleNamespace())
    cv = email.attachments[0]
    passport = cv.model_copy(update={"filename": "passport.pdf", "attachment_id": "att-2"})
    monkeypatch.setattr(pipeline_module, "detect", lambda _: SimpleNamespace(
        is_candidate=True, resume_attachments=[passport, cv], reason="application"))
    monkeypatch.setattr(pipe, "_process_attachment", lambda _email, att, _gmail: AttachmentResult(
        att.filename, "not_resume" if att is passport else "error", detail="pending CV read"))
    assert pipe.process_email(email).status == "error"
    assert pipe.process_email(email).status == "error"
    notify.assert_not_called()


def test_failed_notification_does_not_fail_document_processing(monkeypatch):
    parser = SimpleNamespace(parse_file=lambda *_: (CandidateProfile(is_resume=False), document()))
    pipe, email, notify = parts(monkeypatch, parser)
    notify.side_effect = RuntimeError("notification unavailable")
    assert pipe.process_email(email).status == "skipped"


def test_attachment_notification_persists_and_pushes_exact_reason(monkeypatch):
    from app import notifications
    from app.api import websocket

    repo = Mock()
    users = SimpleNamespace(list_admins=lambda **_: [SimpleNamespace(id="admin1")])
    push = Mock()
    monkeypatch.setattr(websocket, "publish_event", push)
    reason = not_resume_reason(document(), "fallback")
    assert notifications.notify_candidate_rejected(
        reason=reason, filename="application.pdf", from_addr="sender@example.com",
        title="Attachment not ingested", repo=repo, users=users,
    ) == 1
    stored = repo.record.call_args.kwargs
    event = push.call_args.args[0]
    assert stored["title"] == event["title"] == "Attachment not ingested"
    assert stored["message"] == event["message"]
    assert reason in stored["message"]
    assert event["country"] == ""


def test_existing_foreign_cv_notification_title_is_preserved(monkeypatch):
    from app import notifications
    from app.api import websocket

    push = Mock()
    monkeypatch.setattr(websocket, "publish_event", push)
    notifications.notify_candidate_rejected(
        reason="Explicit nationality: Pakistani", country="Pakistan",
        repo=Mock(), users=SimpleNamespace(list_admins=lambda **_: []),
    )
    assert push.call_args.args[0]["title"] == "Candidate rejected — other nationality"
