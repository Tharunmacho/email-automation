from unittest.mock import patch

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

    def count(self):
        return len(self.candidates)

    def list_candidates(self, limit=50, skip=0):
        return list(self.candidates.values())[skip : skip + limit]


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
    response = test_client.post("/candidates/candidate-alice/verify")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "verified"

    # Test verifying non-existent candidate
    response = test_client.post("/candidates/non-existent/verify")
    assert response.status_code == 404
