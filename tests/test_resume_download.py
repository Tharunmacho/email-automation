"""Original-file downloads across storage, email recovery and filename formats."""
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.core.models import Attachment, CandidateProfile, CandidateRecord, EmailMessage, SourceEmail, StoredResume
from app.db.dedup import sha256_hex
from app.services import resume_recovery

DATA = b"%PDF-1.4 original application"


@pytest.fixture
def record():
    return CandidateRecord(
        id="candidate-1",
        profile=CandidateProfile(is_resume=True, confidence=1),
        resume=StoredResume(
            original_filename="application.pdf", mime_type="application/pdf",
            size=len(DATA), sha256=sha256_hex(DATA), storage_backend="gridfs",
            storage_key="2026/09/application.pdf",
        ),
        source_email=SourceEmail(message_id="old-id", thread_id="<original@example.test>", from_addr="a@example.test"),
        assigned_staff_id="staff-1",
    )


@pytest.fixture
def api(monkeypatch, record):
    monkeypatch.setattr(routes, "repo", lambda: SimpleNamespace(get=lambda cid: record))
    routes.app.dependency_overrides[routes.current_user] = lambda: {
        "id": "admin-1", "role": "admin", "pages": ["candidates"],
    }
    try:
        yield TestClient(routes.app)
    finally:
        routes.app.dependency_overrides.pop(routes.current_user, None)


@pytest.fixture
def mailbox(monkeypatch):
    message = EmailMessage(
        message_id="recovered-id", thread_id="<original@example.test>", from_addr="a@example.test",
        attachments=[Attachment(filename="application.pdf", mime_type="application/pdf", size=len(DATA), attachment_id="attachment-1")],
    )
    client = Mock()
    client.get_message_by_rfc_id.return_value = message
    client.download_attachment.return_value = DATA
    monkeypatch.setattr(resume_recovery, "get_all_email_clients", lambda: [client])
    storage = Mock(name="storage")
    storage.name = "gridfs"
    monkeypatch.setattr(resume_recovery, "get_storage_backend", lambda: storage)
    repository = Mock()
    monkeypatch.setattr(resume_recovery, "CandidateRepository", lambda: repository)
    return SimpleNamespace(client=client, message=message, storage=storage, repository=repository)


def test_lazy_email_attachment_is_recovered_and_cached(record, mailbox):
    assert resume_recovery.recover_from_email(record) == DATA
    mailbox.client.download_attachment.assert_called_once_with("recovered-id", mailbox.message.attachments[0])
    mailbox.storage.save.assert_called_once_with(record.resume.storage_key, DATA, content_type="application/pdf")
    mailbox.repository.set_storage_backend.assert_called_once_with(record.id, "gridfs")


def test_gmail_message_id_is_used_when_rfc_lookup_is_not_supported(record, mailbox):
    del mailbox.client.get_message_by_rfc_id
    mailbox.client.get_message.return_value = mailbox.message
    assert resume_recovery.recover_from_email(record) == DATA
    mailbox.client.get_message.assert_called_once_with("old-id")


def test_inline_attachment_needs_no_extra_fetch(record, mailbox):
    mailbox.message.attachments[0].data = DATA
    assert resume_recovery.recover_from_email(record) == DATA
    mailbox.client.download_attachment.assert_not_called()


def test_other_candidates_attachment_is_never_restored(record, mailbox):
    mailbox.client.download_attachment.return_value = b"someone else's resume"
    assert resume_recovery.recover_from_email(record) is None
    mailbox.storage.save.assert_not_called()


def test_no_fingerprint_does_not_select_an_arbitrary_attachment(record, mailbox):
    record.resume.sha256 = ""
    assert resume_recovery.recover_from_email(record) is None
    mailbox.client.get_message_by_rfc_id.assert_not_called()


def test_one_broken_attachment_does_not_hide_a_later_resume(record, mailbox):
    mailbox.message.attachments.insert(0, mailbox.message.attachments[0].model_copy())
    mailbox.client.download_attachment.side_effect = [TimeoutError("unreadable"), DATA]
    assert resume_recovery.recover_from_email(record) == DATA


def test_recovery_still_downloads_if_caching_fails(record, mailbox):
    mailbox.storage.save.side_effect = TimeoutError("offline")
    assert resume_recovery.recover_from_email(record) == DATA
    mailbox.repository.set_storage_backend.assert_not_called()


def test_email_body_application_is_recovered(record, mailbox):
    mailbox.message.attachments = []
    mailbox.message.body_text = "  Original resume text  "
    record.resume.original_filename = "email_body.txt"
    record.resume.sha256 = sha256_hex(b"Original resume text")
    assert resume_recovery.recover_from_email(record) == b"Original resume text"


@pytest.mark.parametrize("filename", ["application.pdf", "தமிழ் CV.pdf", "résumé.pdf", 'CV "final".pdf'])
def test_downloads_preserve_unicode_names(api, record, monkeypatch, filename):
    record.resume.original_filename = filename
    monkeypatch.setattr(routes, "get_storage_backend", lambda name: SimpleNamespace(load=lambda key: DATA))
    response = api.get("/candidates/candidate-1/resume")
    assert response.status_code == 200
    assert response.content == DATA
    header = response.headers["content-disposition"]
    assert "filename*=UTF-8''" + quote(filename, safe="") in header
    assert header.isascii()


def test_filename_cannot_inject_headers_or_paths():
    response = routes._attachment_response(DATA, "application/pdf", '../folder\\CV\r\nInjected.pdf')
    assert response.headers["content-disposition"] == 'attachment; filename="CVInjected.pdf"; filename*=UTF-8\'\'CVInjected.pdf'


def test_download_recovers_a_file_missing_from_both_backends(api, monkeypatch, mailbox):
    storage = Mock()
    storage.load.side_effect = FileNotFoundError("missing")
    monkeypatch.setattr(routes, "get_storage_backend", lambda name: storage)
    response = api.get("/candidates/candidate-1/resume")
    assert response.status_code == 200
    assert response.content == DATA


@pytest.mark.parametrize("failure,status", [(FileNotFoundError("missing"), 404), (TimeoutError("offline"), 503)])
def test_missing_files_and_storage_outages_are_distinguished(api, monkeypatch, failure, status):
    storage = Mock()
    storage.load.side_effect = failure
    monkeypatch.setattr(routes, "get_storage_backend", lambda name: storage)
    monkeypatch.setattr(resume_recovery, "recover_from_email", lambda record: None)
    assert api.get("/candidates/candidate-1/resume").status_code == status


def test_old_backend_pointer_still_downloads_from_alternate_storage(api, monkeypatch):
    missing = Mock()
    missing.load.side_effect = FileNotFoundError("missing")
    local = SimpleNamespace(load=lambda key: DATA)
    monkeypatch.setattr(routes, "get_storage_backend", lambda name: local if name == "local" else missing)
    assert api.get("/candidates/candidate-1/resume").content == DATA


def test_admin_can_download_candidates_assigned_to_any_staff(api, record, monkeypatch):
    record.assigned_staff_id = "other-staff"
    monkeypatch.setattr(routes, "get_storage_backend", lambda name: SimpleNamespace(load=lambda key: DATA))
    assert api.get("/candidates/candidate-1/resume").status_code == 200
