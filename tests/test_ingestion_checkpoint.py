"""A successful OCR must survive a failed candidate write and process restart."""
from datetime import timedelta

import mongomock
import pytest

from app.core.exceptions import AIParseError
from app.core.models import Attachment, CandidateProfile, EmailMessage, ExtractedDocument, PageText, utcnow
from app.db import ingestion_checkpoints as checkpoints
from app.extraction.jobs import current_job_context
from app.ingestion.pipeline import IngestionPipeline


class FakeRepo:
    def __init__(self):
        self.records = {}

    def find_by_message_id(self, message_id):
        return next((r for r in self.records.values() if r.source_email.message_id == message_id), None)

    def find_by_resume_hash(self, sha256):
        return next((r for r in self.records.values() if r.resume_hash == sha256), None)

    def find_by_email_or_phone(self, *args):
        return None

    def insert(self, record):
        self.records[record.id] = record
        return record.id


class FakeLedger:
    def __init__(self):
        self.messages = set()

    def is_message_suppressed(self, message_id):
        return False

    def message_seen(self, message_id):
        return message_id in self.messages

    def find_by_hash(self, sha256):
        return None

    def record(self, message_id, *args):
        self.messages.add(message_id)


class FakeStorage:
    name = "test"

    def __init__(self):
        self.saved = {}

    def exists(self, key):
        return key in self.saved

    def save(self, key, data, **kwargs):
        self.saved[key] = data
        return key


def _email(message_id):
    data = b"%PDF-1.4 Resume of Alice Smith"
    return EmailMessage(message_id=message_id, thread_id="thread-1", from_addr="alice@example.com",
                        subject="My resume", body_text="Please find my resume attached.",
                        attachments=[Attachment(filename="alice_resume.pdf", mime_type="application/pdf",
                                                size=len(data), attachment_id="att-1", data=data)])


class CountingParser:
    def __init__(self):
        self.calls = 0

    def parse_file(self, data, filename):
        self.calls += 1
        return (CandidateProfile(is_resume=True, confidence=0.95, full_name="Alice Smith",
                                 email="alice@example.com", phone="+1 555-0100"),
                ExtractedDocument(text="Alice Smith", method="pdf_ocr", ocr_used=True,
                                  veris_resume_result={"name": "Alice Smith"}))


@pytest.fixture
def parts(monkeypatch):
    now = [utcnow()]
    monkeypatch.setattr(checkpoints, "utcnow", lambda: now[0])
    database = mongomock.MongoClient(tz_aware=True)["checkpoint_integration"]
    store = checkpoints.IngestionCheckpointStore(database["checkpoints"], database["extractions"])
    repo, ledger, storage, parser = FakeRepo(), FakeLedger(), FakeStorage(), CountingParser()
    monkeypatch.setattr(IngestionPipeline, "_allocate", lambda *args: True)
    monkeypatch.setattr("app.ingestion.pipeline.settings.auto_reply_enabled", False)
    pipeline = IngestionPipeline(repo, storage, parser, ledger, checkpoints=store)
    return pipeline, store, database, now, repo, parser


def fail_contact_lookup(repo):
    original = repo.find_by_email_or_phone

    def fail(*_args):
        raise ValueError("Existing candidate contact is incompatible")

    repo.find_by_email_or_phone = fail
    return original


def test_restart_resumes_candidate_write_without_another_parse(parts):
    pipeline, store, database, now, repo, parser = parts
    original = fail_contact_lookup(repo)
    first = pipeline.process_email(_email("msg-retry"))
    assert first.status == "error"
    assert database.extractions.count_documents({}) == 1
    assert not repo.records

    # A new process/pipeline uses only durable state, not an in-memory answer.
    repo.find_by_email_or_phone = original
    now[0] += timedelta(seconds=91)
    restarted = IngestionPipeline(repo, pipeline.storage, parser, pipeline.ledger, checkpoints=store)
    result = restarted.process_email(_email("msg-retry"))
    assert result.status == "processed"
    assert parser.calls == 1
    assert len(repo.records) == 1
    assert database.extractions.count_documents({}) == 0


def test_repeated_candidate_errors_are_quarantined_and_reset_reuses_answer(parts):
    pipeline, store, database, now, repo, parser = parts
    original = fail_contact_lookup(repo)
    for _ in range(3):
        assert pipeline.process_email(_email("msg-poison")).status == "error"
        now[0] += timedelta(minutes=5)
    row = database.checkpoints.find_one({})
    assert row["status"] == "quarantined"
    assert row["attempts"] == 3
    for _ in range(10):
        pipeline.process_email(_email("msg-poison"))
    assert parser.calls == 1
    assert database.checkpoints.find_one({})["attempts"] == 3
    review = store.review_queue()
    assert review[0]["checkpoint_id"] == row["_id"]
    assert "incompatible" in review[0]["last_error"]
    assert "profile" not in review[0] and "extracted" not in review[0]

    assert store.reset_for_retry(row["_id"])
    repo.find_by_email_or_phone = original
    assert pipeline.process_email(_email("msg-poison")).status == "processed"
    assert parser.calls == 1


def test_backoff_does_not_consume_an_attempt_or_parse_again(parts):
    pipeline, _, database, _, repo, parser = parts
    fail_contact_lookup(repo)
    pipeline.process_email(_email("msg-deferred"))
    result = pipeline.process_email(_email("msg-deferred"))
    assert result.status == "error"
    assert "deferred" in result.attachments[0].detail
    assert parser.calls == 1
    assert database.checkpoints.find_one({})["attempts"] == 1


def test_checkpoint_is_scoped_to_source_bytes_and_message(parts):
    pipeline, _, database, _, repo, parser = parts
    fail_contact_lookup(repo)
    email = _email("msg-same-handle")
    pipeline.process_email(email)
    changed = _email("msg-same-handle")
    changed.attachments[0].data += b" modified"
    pipeline.process_email(changed)
    other_mail = _email("msg-other-mailbox")
    pipeline.process_email(other_mail)
    assert parser.calls == 3
    assert database.extractions.count_documents({}) == 3


def test_pending_ocr_is_deferred_without_exhausting_intake_budget(parts):
    pipeline, _, database, now, _, _ = parts

    class PendingParser:
        def parse_file(self, data, filename):
            recorder = current_job_context().recorder
            recorder.on_submitted("resume", "already-running", "stable-key")
            recorder.on_finished("resume", "already-running", "running")
            raise AIParseError("OCR job has not completed yet")

    pipeline.parser = PendingParser()
    for _ in range(7):
        result = pipeline.process_email(_email("msg-pending"))
        assert result.status == "error"
        now[0] += timedelta(minutes=5)
    row = database.checkpoints.find_one({})
    assert row["status"] == "retry"
    assert row["attempts"] == 0
    assert database.extractions.count_documents({}) == 0


def test_unavailable_checkpoint_database_stops_before_paid_parse(parts, monkeypatch):
    pipeline, store, _, _, _, parser = parts

    def unavailable(*args, **kwargs):
        raise RuntimeError("Checkpoint database is unavailable")

    monkeypatch.setattr(store, "begin", unavailable)
    result = pipeline.process_email(_email("msg-db-down"))
    assert result.status == "error"
    assert parser.calls == 0


def test_expired_saved_extraction_does_not_silently_buy_a_new_one(parts):
    pipeline, store, database, now, repo, parser = parts
    original = fail_contact_lookup(repo)
    pipeline.process_email(_email("msg-expired"))
    now[0] += timedelta(days=31)
    # Simulate Mongo's TTL sweep removing the payload, not the quarantine gate.
    database.extractions.delete_many({})
    for _ in range(3):
        pipeline.process_email(_email("msg-expired"))
        now[0] += timedelta(minutes=5)
    row = database.checkpoints.find_one({})
    assert row["status"] == "quarantined"
    assert "expired or is missing" in row["last_error"]
    assert parser.calls == 1
    assert store.reset_for_retry(row["_id"])
    repo.find_by_email_or_phone = original
    assert pipeline.process_email(_email("msg-expired")).status == "processed"
    assert parser.calls == 2


def test_a_live_lease_blocks_a_second_worker_and_worker_crashes_are_bounded(parts):
    _, store, database, now, _, _ = parts
    args = ("cv@example.com", "message", "attachment", "source-sha256", "cv.pdf")
    first = store.begin(*args, lease_seconds=60)
    assert first.acquired
    assert not store.begin(*args, lease_seconds=60).acquired
    for _ in range(2):
        now[0] += timedelta(seconds=61)
        assert store.begin(*args, lease_seconds=60).acquired
    # A live third worker can still finish; no concurrent poll abandons it.
    assert database.checkpoints.find_one({})["status"] == "processing"
    now[0] += timedelta(seconds=61)
    assert not store.begin(*args, lease_seconds=60).acquired
    assert database.checkpoints.find_one({})["status"] == "quarantined"


def test_supporting_document_read_survives_a_sibling_resume_retry(parts):
    pipeline, _, _, _, _, _ = parts

    class SupportingParser:
        calls = 0

        def parse_file(self, data, filename):
            self.calls += 1
            return (CandidateProfile(is_resume=False, confidence=0.1),
                    ExtractedDocument(text="PASSPORT", method="pdf_ocr",
                                      pages=[PageText(page_number=1, text="PASSPORT", kind="id_document")]))

    pipeline.parser = SupportingParser()
    email = _email("msg-supporting")
    first = pipeline._process_attachment(email, email.attachments[0], None)
    second = pipeline._process_attachment(email, email.attachments[0], None)
    assert first.status == second.status == "not_resume"
    assert second.extracted.pages[0].text == "PASSPORT"
    assert second.source is email.attachments[0]
    assert pipeline.parser.calls == 1


def test_pending_refinement_never_falls_through_to_a_second_paid_upload(parts, monkeypatch):
    from app.ai.resume_parser import ResumeParser
    from app.extraction import ocr, ocr_gateway, text_extractor
    from app.extraction.jobs import JobHandle, JobOutcome

    pipeline, _, database, now, _, _ = parts
    monkeypatch.setattr("app.config.settings.veris_ocr_api_key", "test-key")
    monkeypatch.setattr("app.config.settings.ocr_async_jobs_enabled", True)
    calls = []

    def pending(data, filename, mode, key, **kwargs):
        calls.append(key)
        handle = JobHandle("existing-refinement", mode, "running", duplicate=bool(calls))
        kwargs["on_submitted"](handle)
        return handle, JobOutcome(handle.job_id, mode, "running", timed_out=True)

    def extract(data, filename):
        read = ocr.ocr_via_veris_read(data, filename)
        assert read.pages == []
        return ExtractedDocument(text="Curriculum vitae of Alice Smith", method="pdf_text", is_resume=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("A running refinement must not trigger another paid upload")

    monkeypatch.setattr(ocr_gateway, "run_job", pending)
    monkeypatch.setattr(text_extractor, "extract_text", extract)
    monkeypatch.setattr("recursai.veris_ocr.VerisOCR", forbidden)
    monkeypatch.setattr("app.extraction.jobs.AsyncOCRJobClient.run", forbidden)
    pipeline.parser = ResumeParser()
    for _ in range(4):
        assert pipeline.process_email(_email("msg-real-pending")).status == "error"
        now[0] += timedelta(minutes=5)
    assert len(set(calls)) == 1
    assert database.checkpoints.find_one({})["attempts"] == 0


def test_direct_resume_parser_records_running_job_without_spending_retry_budget(parts, monkeypatch):
    from app.ai.resume_parser import ResumeParser
    from app.extraction import text_extractor
    from app.extraction.jobs import AsyncOCRJobClient, JobHandle, JobOutcome

    pipeline, _, database, now, _, _ = parts
    monkeypatch.setattr("app.config.settings.veris_ocr_api_key", "test-key")
    monkeypatch.setattr("app.config.settings.require_veris_resume", True)
    monkeypatch.setattr(text_extractor, "extract_text", lambda *args: ExtractedDocument(
        text="Curriculum vitae Alice Smith alice@example.com", method="pdf_text", is_resume=True))
    keys = []

    def pending(self, data, filename, mode, idempotency_key, **kwargs):
        keys.append(idempotency_key)
        handle = JobHandle("existing-parse", mode, "running", duplicate=len(keys) > 1)
        kwargs["on_submitted"](handle)
        return handle, JobOutcome(handle.job_id, mode, "running", timed_out=True)

    monkeypatch.setattr(AsyncOCRJobClient, "run", pending)
    pipeline.parser = ResumeParser()
    for _ in range(4):
        assert pipeline.process_email(_email("msg-direct-pending")).status == "error"
        now[0] += timedelta(minutes=5)
    assert len(set(keys)) == 1
    row = database.checkpoints.find_one({})
    assert row["status"] == "retry"
    assert row["attempts"] == 0


def test_failed_checkpoint_write_never_causes_an_automatic_second_parse(parts, monkeypatch):
    pipeline, _, database, now, _, parser = parts

    def unavailable(*args, **kwargs):
        raise RuntimeError("Extraction cache write failed")

    monkeypatch.setattr(database.extractions, "update_one", unavailable)
    for _ in range(4):
        assert pipeline.process_email(_email("msg-cache-write-down")).status == "error"
        now[0] += timedelta(minutes=5)
    assert parser.calls == 1
    assert database.checkpoints.find_one({})["status"] == "quarantined"


def test_operator_review_and_retry_are_admin_only_and_preserve_cached_answer(parts, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api.routes import app, current_user

    pipeline, store, database, now, repo, _ = parts
    fail_contact_lookup(repo)
    for _ in range(3):
        pipeline.process_email(_email("msg-review-api"))
        now[0] += timedelta(minutes=5)
    row = database.checkpoints.find_one({})
    monkeypatch.setattr(checkpoints, "IngestionCheckpointStore", lambda: store)
    client = TestClient(app)
    try:
        app.dependency_overrides[current_user] = lambda: {"id": "staff-1", "role": "staff"}
        assert client.get("/ingest/checkpoints/review").status_code == 403
        assert client.post(f"/ingest/checkpoints/{row['_id']}/retry").status_code == 403
        app.dependency_overrides[current_user] = lambda: {"id": "admin-1", "role": "admin"}
        review = client.get("/ingest/checkpoints/review")
        assert review.status_code == 200
        assert review.json()["items"][0]["checkpoint_id"] == row["_id"]
        assert client.post(f"/ingest/checkpoints/{row['_id']}/retry").status_code == 200
        assert database.extractions.count_documents({}) == 1
        assert client.post(f"/ingest/checkpoints/{row['_id']}/retry").status_code == 404
    finally:
        app.dependency_overrides.pop(current_user, None)


def test_nonretryable_direct_job_error_cannot_masquerade_as_pending_forever(parts, monkeypatch):
    from app.ai.resume_parser import ResumeParser
    from app.extraction import text_extractor
    from app.extraction.jobs import AsyncOCRJobClient, JobHandle, OCRJobError

    pipeline, _, database, now, _, _ = parts
    monkeypatch.setattr("app.config.settings.veris_ocr_api_key", "test-key")
    monkeypatch.setattr("app.config.settings.require_veris_resume", True)
    monkeypatch.setattr(text_extractor, "extract_text", lambda *args: ExtractedDocument(
        text="Curriculum vitae Alice Smith alice@example.com", method="pdf_text", is_resume=True))

    def lost(self, data, filename, mode, idempotency_key, **kwargs):
        kwargs["on_submitted"](JobHandle("lost-job", mode, "queued"))
        raise OCRJobError("Job does not exist", retryable=False, status=404)

    monkeypatch.setattr(AsyncOCRJobClient, "run", lost)
    pipeline.parser = ResumeParser()
    for _ in range(3):
        assert pipeline.process_email(_email("msg-job-gone")).status == "error"
        now[0] += timedelta(minutes=5)
    row = database.checkpoints.find_one({})
    assert row["status"] == "quarantined"
    assert row["attempts"] == 3


def test_expired_worker_cannot_overwrite_or_settle_a_new_owners_checkpoint(parts, monkeypatch):
    from app.ingestion.pipeline import AttachmentResult

    _, store, database, now, _, _ = parts
    args = ("mailbox", "message", "attachment", "source-sha", "cv.pdf")
    old_claim = store.begin(*args, lease_seconds=60)
    real_write = database.extractions.update_one
    next_claim = []

    def delayed_write(query, update, **kwargs):
        if query["_id"] == f"{old_claim.id}:{old_claim.token}":
            # The old worker passed its ownership check, then stalled writing
            # its result. A new worker acquired and saved before it resumes.
            now[0] += timedelta(seconds=61)
            current = store.begin(*args, lease_seconds=60)
            next_claim.append(current)
            store.save_extraction(current, "source-sha",
                                  CandidateProfile(full_name="Current owner", confidence=0.9),
                                  ExtractedDocument(text="current result", method="pdf_ocr"))
        return real_write(query, update, **kwargs)

    monkeypatch.setattr(database.extractions, "update_one", delayed_write)
    with pytest.raises(RuntimeError, match="before publishing"):
        store.save_extraction(old_claim, "source-sha",
                              CandidateProfile(full_name="Expired owner", confidence=0.9),
                              ExtractedDocument(text="stale result", method="pdf_ocr"))
    current = next_claim[0]
    profile, extracted = store.load_extraction(current, "source-sha")
    assert profile.full_name == "Current owner"
    assert extracted.text == "current result"
    assert database.extractions.count_documents({}) == 1
    assert database.checkpoints.find_one({})["extraction_id"] == f"{current.id}:{current.token}"

    with pytest.raises(RuntimeError, match="before completing"):
        store.finish(old_claim, AttachmentResult("cv.pdf", "ingested", "wrong-candidate"))
    assert store.fail(old_claim, "old failure", pending_ocr=True) == "processing"
    row = database.checkpoints.find_one({})
    assert row["lease_token"] == current.token
    assert row["attempts"] == 2
    assert "candidate_id" not in row
    assert store.load_extraction(current, "source-sha")[0].full_name == "Current owner"


def test_expired_lease_cannot_publish_even_before_another_worker_claims(parts):
    _, store, database, now, _, _ = parts
    claim = store.begin("mailbox", "message", "attachment", "sha", "cv.pdf", lease_seconds=60)
    now[0] += timedelta(seconds=61)
    with pytest.raises(RuntimeError, match="before reading"):
        store.load_extraction(claim, "sha")
    with pytest.raises(RuntimeError, match="before checkpointing"):
        store.save_extraction(claim, "sha", CandidateProfile(full_name="Late result"),
                              ExtractedDocument(text="late result", method="pdf_ocr"))
    assert database.extractions.count_documents({}) == 0


@pytest.mark.parametrize("expiry", [None, 600])
def test_startup_rejects_a_conflicting_nonzero_or_missing_ttl(parts, monkeypatch, expiry):
    _, _, database, _, _, _ = parts
    monkeypatch.setattr(checkpoints, "get_checkpoint_collection", lambda: database.checkpoints)
    monkeypatch.setattr(checkpoints, "get_extraction_collection", lambda: database.extractions)
    options = {} if expiry is None else {"expireAfterSeconds": expiry}
    database.extractions.create_index("expires_at", name="intake_extraction_expiry", **options)
    with pytest.raises(RuntimeError, match="requires expires_at TTL"):
        checkpoints.ensure_ingestion_checkpoint_indexes()


def test_startup_verifies_ttl_created_with_zero_expiry(parts, monkeypatch):
    _, _, database, _, _, _ = parts
    monkeypatch.setattr(checkpoints, "get_checkpoint_collection", lambda: database.checkpoints)
    monkeypatch.setattr(checkpoints, "get_extraction_collection", lambda: database.extractions)
    checkpoints.ensure_ingestion_checkpoint_indexes()
    assert database.extractions.index_information()["intake_extraction_expiry"]["expireAfterSeconds"] == 0
