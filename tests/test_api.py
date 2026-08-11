from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user, repo
from app.core.models import CandidateProfile, CandidateRecord, SourceEmail, StoredResume


@pytest.fixture(autouse=True)
def mock_ensure_indexes():
    with patch("app.api.routes.ensure_indexes") as mock:
        yield mock



class MockRepository:
    def __init__(self):
        self.candidates = {}

    def get(self, candidate_id: str):
        return self.candidates.get(candidate_id)

    def update_profile(self, candidate_id: str, profile: CandidateProfile):
        if candidate_id in self.candidates:
            self.candidates[candidate_id].profile = profile
            from app.db.dedup import normalize_email, normalize_phone
            self.candidates[candidate_id].email_key = normalize_email(profile.email)
            self.candidates[candidate_id].phone_key = normalize_phone(profile.phone)

    def update_status(self, candidate_id: str, status: str, duplicate_of=None):
        if candidate_id in self.candidates:
            self.candidates[candidate_id].status = status
            if duplicate_of:
                self.candidates[candidate_id].duplicate_of = duplicate_of

    def delete(self, candidate_id: str) -> bool:
        return self.candidates.pop(candidate_id, None) is not None

    def count(self):
        return len(self.candidates)

    def list_candidates(self, limit=50, skip=0):
        return list(self.candidates.values())[skip : skip + limit]

    def list_summaries(self, limit=50, skip=0, minimal=False):
        """Rows, not records — the same shape the projection produces.

        What the projection actually leaves out is pinned in
        tests/test_candidate_listing.py, against the real repository.
        """
        records = list(self.candidates.values())[skip : skip + limit]
        if minimal:
            return [
                {
                    "id": r.id,
                    "full_name": r.profile.full_name,
                    "email": r.profile.email,
                    "phone": r.profile.phone,
                    "status": r.status,
                    "confidence": r.profile.confidence,
                    "created_at": r.created_at,
                }
                for r in records
            ]
        return [
            {
                "id": r.id,
                "status": r.status,
                "created_at": r.created_at,
                "profile": {
                    "full_name": r.profile.full_name,
                    "email": r.profile.email,
                    "phone": r.profile.phone,
                    "confidence": r.profile.confidence,
                },
            }
            for r in records
        ]


@pytest.fixture
def test_client():
    mock_repo = MockRepository()
    
    # Pre-populate with one candidate for testing
    profile = CandidateProfile(
        is_resume=True,
        confidence=0.9,
        full_name="Alice Smith",
        email="alice@example.com",
        phone="+1 555-0100",
        skills=["Python", "FastAPI"],
    )
    resume = StoredResume(
        original_filename="alice_resume.pdf",
        mime_type="application/pdf",
        size=1024,
        sha256="abc123hash",
        storage_backend="local",
        storage_key="2026/07/alice_resume.pdf",
    )
    source = SourceEmail(
        message_id="msg-123",
        thread_id="thread-123",
        from_addr="alice@example.com",
        subject="Resume Submission",
    )
    record = CandidateRecord(
        id="candidate-alice",
        profile=profile,
        resume=resume,
        source_email=source,
        email_key="alice@example.com",
        phone_key="5550100",
        status="ingested",
    )
    mock_repo.candidates["candidate-alice"] = record

    with patch("app.api.routes.repo", return_value=mock_repo):
        # Data endpoints require a bearer token. These tests cover the route
        # logic, not authentication, so stub the dependency rather than logging
        # in for each one — auth itself is covered by test_auth.py.
        app.dependency_overrides[current_user] = lambda: {
            "id": "test-user", "email": "test@example.com",
            "name": "Test User", "role": "admin",
        }
        client = TestClient(app)
        try:
            yield client
        finally:
            app.dependency_overrides.pop(current_user, None)



def test_health_endpoint(test_client):
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "candidates": 1}


def test_list_candidates(test_client):
    response = test_client.get("/candidates")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "candidate-alice"


def test_list_candidates_minimal_view_is_the_flat_listing_contract(test_client):
    response = test_client.get("/candidates?view=minimal")
    assert response.status_code == 200

    row = response.json()["items"][0]
    assert set(row) == {
        "id", "full_name", "email", "phone", "status", "confidence", "created_at",
    }
    assert row["full_name"] == "Alice Smith"


def test_list_candidates_rejects_a_view_it_does_not_serve(test_client):
    """`view=full` would put every OCR payload in one response — there is no
    such view, and asking for one must fail rather than fall back to the list."""
    assert test_client.get("/candidates?view=full").status_code == 422


def test_get_candidate(test_client):
    response = test_client.get("/candidates/candidate-alice")
    assert response.status_code == 200
    assert response.json()["profile"]["full_name"] == "Alice Smith"

    # Test non-existent candidate
    response = test_client.get("/candidates/non-existent")
    assert response.status_code == 404


def test_update_candidate_profile(test_client):
    updated_profile = {
        "is_resume": True,
        "confidence": 0.95,
        "full_name": "Alice Smith Updated",
        "email": "alice.updated@example.com",
        "phone": "+1 (555) 555-9999",
        "skills": ["Python", "FastAPI", "MongoDB"],
        "languages": ["English"],
        "work_experience": [],
        "education": [],
        "certifications": [],
        "projects": [],
        "achievements": [],
    }
    response = test_client.put("/candidates/candidate-alice", json=updated_profile)
    assert response.status_code == 200
    data = response.json()
    assert data["profile"]["full_name"] == "Alice Smith Updated"
    assert data["profile"]["skills"] == ["Python", "FastAPI", "MongoDB"]
    assert data["email_key"] == "alice.updated@example.com"
    assert data["phone_key"] == "5555559999"

    # Test updating non-existent candidate
    response = test_client.put("/candidates/non-existent", json=updated_profile)
    assert response.status_code == 404


def test_verify_candidate(test_client):
    response = test_client.get("/candidates/candidate-alice/verify")
    # verification POST route
    response = test_client.post("/candidates/candidate-alice/verify")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "verified"

    # Test verifying non-existent candidate
    response = test_client.post("/candidates/non-existent/verify")
    assert response.status_code == 404


def test_raw_ocr_persistence(test_client):
    ocr_payload = {
        "name": "Nabeel Noorudheen",
        "pages": [{"text": "Sample text", "lines": [{"text": "Sample text"}]}]
    }
    profile = CandidateProfile(
        is_resume=True,
        confidence=0.95,
        full_name="Nabeel Noorudheen",
        email="nabeel@example.com",
        raw_ocr=ocr_payload,
    )
    record = CandidateRecord(
        id="candidate-nabeel",
        profile=profile,
        resume=StoredResume(
            original_filename="nabeel.pdf",
            mime_type="application/pdf",
            size=500,
            sha256="hash123",
            storage_backend="local",
            storage_key="keys/nabeel.pdf",
        ),
        source_email=SourceEmail(
            message_id="msg-nab",
            thread_id="thread-nab",
            from_addr="nabeel@example.com",
        ),
        raw_ocr=ocr_payload,
    )
    assert record.raw_ocr["name"] == "Nabeel Noorudheen"
    assert record.profile.raw_ocr["pages"][0]["text"] == "Sample text"


def test_unsuppress_on_delete():
    from app.db.ledger import IngestLedger
    mock_coll = {}

    class MockColl:
        def __init__(self):
            self.docs = {}
        def update_one(self, filter_dict, update_dict, upsert=False):
            key = filter_dict.get("_id")
            self.docs[key] = {"suppressed": False, "candidate_id": "cand-1", "resume_hash": "hash1"}
        def delete_many(self, filter_dict):
            count = len(self.docs)
            self.docs.clear()
            class Res:
                deleted_count = count
            return Res()
        def count_documents(self, filter_dict, limit=1):
            return len(self.docs)

    mock = MockColl()
    ledger = IngestLedger(collection=mock)
    ledger.record("msg-1", "hash1", "cand-1", "ingested")
    deleted = ledger.unsuppress_candidate("cand-1", "hash1", "msg-1")
    assert deleted == 1
    assert ledger.is_suppressed("hash1") is False



def test_delete_candidate_retires_every_message_carrying_the_resume(test_client):
    """A resume that arrived on three emails must retire all three, not just the
    one recorded on the candidate — a leftover keeps coming back on every poll.

    Retiring means the deleted label goes on and the processed label comes off:
    the search excludes both, so these emails never return, while a *new* email
    carrying the same resume is unlabelled and ingests as a new candidate."""
    gmail = MagicMock()
    every_message = {"msg-123", "msg-forwarded", "msg-resent"}

    with patch("app.storage.factory.get_storage_backend"), \
         patch("app.db.ledger.get_ledger_collection"), \
         patch("app.db.ledger.IngestLedger.message_ids_for_candidate",
               return_value=sorted(every_message)), \
         patch("app.db.ledger.IngestLedger.retire_candidate", return_value=3), \
         patch("app.email_client.get_email_client", return_value=gmail):
        response = test_client.delete("/api/v1/candidates/candidate-alice")

    assert response.status_code == 200
    assert {c.args[0] for c in gmail.apply_label.call_args_list} == every_message
    assert {c.args[1] for c in gmail.apply_label.call_args_list} == {"Resumes/Deleted"}
    assert {c.args[0] for c in gmail.remove_label.call_args_list} == every_message
    assert {c.args[1] for c in gmail.remove_label.call_args_list} == {"Resumes/Processed"}


def test_delete_candidate_survives_a_concurrent_delete(test_client):
    """The loser of a duplicate DELETE must not report a 500 — the record is
    gone either way. `get` still sees the record; `delete` removes nothing
    because the winning request already did."""
    raced = MockRepository()
    raced.candidates["candidate-alice"] = CandidateRecord(
        id="candidate-alice",
        profile=CandidateProfile(is_resume=True, confidence=0.9, full_name="Alice Smith"),
        resume=StoredResume(
            original_filename="alice_resume.pdf",
            mime_type="application/pdf",
            size=1024,
            sha256="abc123hash",
            storage_backend="local",
            storage_key="2026/07/alice_resume.pdf",
        ),
        source_email=SourceEmail(message_id="msg-123", thread_id="t", from_addr="a@b.c"),
        status="ingested",
    )
    raced.delete = lambda candidate_id: False

    with patch("app.api.routes.repo", return_value=raced), \
         patch("app.storage.factory.get_storage_backend"), \
         patch("app.db.ledger.get_ledger_collection"), \
         patch("app.db.ledger.IngestLedger.message_ids_for_candidate", return_value=[]), \
         patch("app.db.ledger.IngestLedger.retire_candidate", return_value=0):
        response = test_client.delete("/api/v1/candidates/candidate-alice")

    assert response.status_code == 200
    assert response.json()["status"] == "success"



