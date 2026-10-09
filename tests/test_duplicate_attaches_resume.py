"""An emailed CV from someone already on file is attached, not thrown away.

The production symptom: candidates registered on WhatsApp, then emailed their
CV. The mailbox pipeline found them by email/phone, reported "duplicate" and
discarded the file — so the CRM profile said "No resume on file" while the CV
sat in the mailbox.
"""
from __future__ import annotations

from app.core.models import CandidateProfile, CandidateRecord, StoredResume
from app.db.dedup import normalize_email
from app.ingestion.pipeline import IngestionPipeline
from tests.test_ingestion_allocation import StubParser, body_email
from tests.test_pipeline_redelivery import FakeLedger, FakeRepo, FakeStorage


class AttachingRepo(FakeRepo):
    def attach_resume(self, candidate_id, resume):
        record = self.records.get(candidate_id)
        if not record:
            return False
        record.resume = resume
        record.resume_hash = resume.sha256
        return True


def _existing(resume=None):
    return CandidateRecord(
        id="from-whatsapp",
        source="whatsapp",
        profile=CandidateProfile(full_name="Rajesh Kumar", email="rajesh@example.com"),
        email_key=normalize_email("rajesh@example.com"),
        resume=resume,
        cv_required=False,
    )


def _pipeline(repo, storage):
    return IngestionPipeline(
        repository=repo, storage=storage, parser=StubParser(), ledger=FakeLedger(),
    )


def test_resume_is_attached_to_an_existing_candidate_without_one(monkeypatch):
    monkeypatch.setattr("app.assignment.assign_candidate", lambda *a, **k: None)
    repo, storage = AttachingRepo(), FakeStorage()
    repo.records["from-whatsapp"] = _existing()

    result = _pipeline(repo, storage).process_email(body_email("cv-after-whatsapp"))

    attachment = result.attachments[0]
    assert attachment.status == "duplicate"
    assert attachment.candidate_id == "from-whatsapp"
    resume = repo.records["from-whatsapp"].resume
    assert resume is not None and resume.storage_key, "the emailed CV was discarded"
    assert resume.storage_key in storage.saved
    assert result.ingested_ids == []


def test_an_existing_resume_is_never_replaced(monkeypatch):
    monkeypatch.setattr("app.assignment.assign_candidate", lambda *a, **k: None)
    original = StoredResume(
        original_filename="first.pdf", mime_type="application/pdf", size=3,
        sha256="abc", storage_backend="fake", storage_key="2026/01/first.pdf",
    )
    repo, storage = AttachingRepo(), FakeStorage()
    repo.records["from-whatsapp"] = _existing(resume=original)

    _pipeline(repo, storage).process_email(body_email("second-cv"))

    assert repo.records["from-whatsapp"].resume.storage_key == "2026/01/first.pdf"
    assert storage.saved == {}
