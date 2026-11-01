"""Résumés agents send on WhatsApp become labelled candidates, checked by VeriIS.

VeriIS, storage and allocation are faked; the Sourcing Hub and the submission
ledger are mongomock. The bot half (download, reply wording) is tested in the
bot repo's `src/agent-resume-test.ts`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

# Module scope, deliberately: see the note in `tests/test_b2b_enquiries.py`.
from app.api.routes import app as fastapi_app
from app import agent_resumes
from app.agent_resumes import SUBMISSIONS, find_agent, submit_agent_resume
from app.config import settings
from app.core.models import CandidateProfile, CandidateRecord, ExtractedDocument, StoredResume

SERVICE_KEY = "test-service-key-12345"


class MemoryRepository:
    def __init__(self, existing_phone=None):
        self.records = {}
        self.existing_phone = existing_phone

    def find_by_resume_hash(self, _digest):
        return None

    def find_by_email_or_phone(self, _email, phone):
        if self.existing_phone and phone == self.existing_phone:
            return CandidateRecord(id="old", source="upload", profile=CandidateProfile(full_name="Old"),
                                   resume=_stored())
        return None

    def find_by_passport_key(self, _key):
        return None

    def insert(self, record):
        self.records[record.id] = record
        return record.id

    def get(self, candidate_id):
        return self.records.get(candidate_id)

    def delete(self, candidate_id):
        return self.records.pop(candidate_id, None) is not None


class Veris:
    """Stands in for `ResumeParser`: what VeriIS reads out of the file."""

    def __init__(self, *, is_resume=True, name="Ramesh Kumar", phone="+91 90000 11111", confidence=0.9):
        self.is_resume, self.name, self.phone, self.confidence = is_resume, name, phone, confidence

    def parse_file(self, _data, _filename):
        profile = CandidateProfile(
            is_resume=self.is_resume, confidence=self.confidence,
            full_name=self.name, phone=self.phone,
            additional_info={"extraction_source": "veris_ocr_api"},
        )
        return profile, ExtractedDocument(text="cv", method="veris_resume_api", ocr_used=True, char_count=2)


def _stored(**_kwargs):
    return StoredResume(original_filename="cv.pdf", mime_type="application/pdf", size=3,
                        sha256="h", storage_backend="local", storage_key="k/cv.pdf")


SOURCING = [
    {"id": "AGT-1", "name": "Gulf Manpower", "type": "agent", "status": "ACTIVE",
     "contact": "Imran", "phone": "98765 43210",
     "contacts": [{"name": "Imran", "phone": "98765 43210"}, {"name": "Salma", "phone": "+91 91234 56789"}]},
    {"id": "AGT-2", "name": "Old Partner", "type": "agent", "status": "INACTIVE", "phone": "9000000001"},
    {"id": "CLI-1", "name": "A client", "type": "client", "status": "ACTIVE", "phone": "9000000002"},
]


@pytest.fixture
def db():
    database = mongomock.MongoClient(tz_aware=True)["agent_resumes_test"]
    database["sourcing_clients"].insert_many([dict(row) for row in SOURCING])
    return database


@pytest.fixture(autouse=True)
def fakes():
    """VeriIS configured, storage and allocation faked, nothing leaves the test."""
    with patch("app.services.candidate_upload_intake.settings.veris_ocr_api_key", "key"), \
         patch("app.services.candidate_upload_intake.store_resume", side_effect=_stored), \
         patch("app.services.candidate_upload_intake.route_embedded_identity_documents", return_value=[]), \
         patch.object(agent_resumes, "_allocate") as allocate:
        yield allocate


def submit(db, repository=None, *, phone="919876543210", wamid="wamid.1", parser=None):
    return submit_agent_resume(
        data=b"%PDF-1.4 resume", filename="ramesh.pdf", mime_type="application/pdf",
        phone=phone, wamid=wamid, line_id="558448830676126",
        repository=repository or MemoryRepository(), database=db, parser=parser or Veris(),
    )


# --------------------------------------------------------------------------- #
#  Who counts as an agent
# --------------------------------------------------------------------------- #
def test_any_of_an_agents_contact_numbers_identifies_the_agent(db):
    assert find_agent(db, "+91 91234 56789")["contact_name"] == "Salma"
    assert find_agent(db, "919876543210")["agent_id"] == "AGT-1"


def test_clients_and_unknown_numbers_are_not_agents(db):
    assert find_agent(db, "9000000002") is None
    assert find_agent(db, "9111111111") is None


def test_a_non_agent_is_ignored_and_nothing_is_created(db):
    repository = MemoryRepository()
    assert submit(db, repository, phone="919000000002") == {"status": "ignored", "reason": "not_an_agent"}
    assert repository.records == {}


def test_an_inactive_agent_is_ignored(db):
    assert submit(db, phone="919000000001") == {"status": "ignored", "reason": "inactive_agent"}


# --------------------------------------------------------------------------- #
#  A résumé becomes a labelled candidate
# --------------------------------------------------------------------------- #
def test_a_resume_from_an_agent_becomes_a_candidate_labelled_with_that_agent(db, fakes):
    repository = MemoryRepository()
    outcome = submit(db, repository)

    assert outcome["status"] == "created"
    assert outcome["full_name"] == "Ramesh Kumar"
    assert outcome["agent_name"] == "Gulf Manpower"
    assert outcome["candidate_code"]
    record = repository.records[outcome["candidate_id"]]
    assert record.source == "agent"
    assert record.submitted_by_agent.agent_id == "AGT-1"
    assert record.submitted_by_agent.agent_name == "Gulf Manpower"
    assert record.submitted_by_agent.contact_name == "Imran"
    assert record.submitted_by_agent.line_id == "558448830676126"
    assert record.submitted_by_agent.wamid == "wamid.1"
    assert record.resume is not None
    fakes.assert_called_once()  # sent to normal allocation


def test_a_file_that_is_not_a_resume_is_refused(db):
    repository = MemoryRepository()
    outcome = submit(db, repository, parser=Veris(is_resume=False))
    assert outcome["status"] == "rejected"
    assert outcome["code"] == "not_a_resume"
    assert repository.records == {}


def test_a_resume_without_contact_details_is_refused(db):
    outcome = submit(db, parser=Veris(phone=None))
    assert (outcome["status"], outcome["code"]) == ("rejected", "missing_candidate_contact")


def test_an_existing_candidate_is_refused_as_a_duplicate(db):
    repository = MemoryRepository(existing_phone="9000011111")
    outcome = submit(db, repository)
    assert (outcome["status"], outcome["code"]) == ("rejected", "duplicate_candidate")
    assert repository.records == {}


def test_veris_being_down_is_an_error_not_a_rejection(db):
    with patch("app.services.candidate_upload_intake.settings.veris_ocr_api_key", ""):
        outcome = submit(db)
    assert (outcome["status"], outcome["code"]) == ("error", "veris_not_configured")


# --------------------------------------------------------------------------- #
#  One WhatsApp message, one outcome
# --------------------------------------------------------------------------- #
def test_the_same_message_twice_returns_the_first_outcome_and_creates_one_candidate(db):
    repository = MemoryRepository()
    first = submit(db, repository)
    second = submit(db, repository)
    assert second == first
    assert len(repository.records) == 1


def test_a_message_still_being_processed_says_so(db):
    db[SUBMISSIONS].insert_one({"_id": "wamid.1", "started_at": datetime.now(timezone.utc)})
    assert submit(db) == {"status": "processing"}


def test_a_stale_claim_is_taken_over(db):
    db[SUBMISSIONS].insert_one({"_id": "wamid.1",
                                "started_at": datetime.now(timezone.utc) - timedelta(hours=1)})
    assert submit(db)["status"] == "created"


def test_an_unexpected_failure_releases_the_message_for_a_retry(db):
    class Broken(MemoryRepository):
        def insert(self, record):
            raise RuntimeError("database down")

    with pytest.raises(RuntimeError):
        submit(db, Broken())
    assert db[SUBMISSIONS].count_documents({}) == 0


# --------------------------------------------------------------------------- #
#  The endpoint
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(db):
    with patch("app.api.routes.ensure_indexes"), \
         patch("app.config.settings.whatsapp_service_key", SERVICE_KEY), \
         patch("app.db.mongo.get_db", return_value=db), \
         patch("app.api.routes.repo", return_value=MemoryRepository()), \
         patch("app.services.candidate_upload_intake.ResumeParser", Veris):
        yield TestClient(fastapi_app)


def _post(client, key=SERVICE_KEY):
    return client.post(
        "/agent-resumes",
        headers={"X-Service-Key": key},
        files={"file": ("ramesh.pdf", b"%PDF-1.4 resume", "application/pdf")},
        data={"phone": "919876543210", "wamid": "wamid.9", "line_id": "558448830676126"},
    )


def test_the_endpoint_needs_the_service_key(client):
    assert _post(client, key="wrong").status_code == 401


def test_the_endpoint_creates_the_candidate(client):
    response = _post(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "created"
    assert body["agent_name"] == "Gulf Manpower"
