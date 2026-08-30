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

    def insert(self, record: CandidateRecord):
        self.candidates[record.id] = record
        return record.id

    def find_by_resume_hash(self, resume_hash):
        if not resume_hash:
            return None
        return next(
            (record for record in self.candidates.values() if record.resume_hash == resume_hash),
            None,
        )

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

    def assign(self, candidate_id: str, staff_id: str, staff_name=None) -> bool:
        record = self.candidates.get(candidate_id)
        if not record:
            return False
        record.assigned_staff_id = staff_id
        record.assigned_staff_name = staff_name
        return True

    def count(self, query=None, staff_id=None):
        return len(self._scoped(staff_id))

    def list_candidates(self, limit=50, skip=0):
        return list(self.candidates.values())[skip : skip + limit]

    def _scoped(self, staff_id=None):
        """The isolation rule, modelled rather than ignored.

        The double honours `staff_id` instead of accepting and dropping it, so
        a route that stops passing the scope fails a test here rather than
        silently serving one staff member another's candidates.
        """
        records = list(self.candidates.values())
        if staff_id:
            return [r for r in records if getattr(r, "assigned_staff_id", None) == staff_id]
        return records

    def list_summaries(self, limit=50, skip=0, minimal=False, query=None, staff_id=None):
        """Rows, not records — the same shape the projection produces.

        What the projection actually leaves out is pinned in
        tests/test_candidate_listing.py, against the real repository.
        """
        records = self._scoped(staff_id)[skip : skip + limit]
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


def _uploaded_candidate_result():
    from app.services.candidate_upload_intake import CandidateUploadResult

    profile = CandidateProfile(
        is_resume=True,
        confidence=0.94,
        full_name="Meera Nair",
        email="meera@example.com",
        phone="+91 98765 43210",
    )
    record = CandidateRecord(
        id="candidate-uploaded",
        source="upload",
        profile=profile,
        resume=StoredResume(
            original_filename="meera.pdf",
            mime_type="application/pdf",
            size=12,
            sha256="uploaded-hash",
            storage_backend="local",
            storage_key="2026/08/candidate-uploaded_meera.pdf",
            extraction_method="veris_resume_api",
            ocr_used=True,
        ),
        resume_hash="uploaded-hash",
        email_key="meera@example.com",
        phone_key="9876543210",
        cv_required=True,
    )
    return CandidateUploadResult(
        candidate=record,
        identity={
            "aadhaar": [{"name": "Meera Nair", "masked_aadhaar_number": "XXXXXXXX9017"}],
            "passport": [{"passport_number": "Z1234567", "check_digits_valid": True}],
        },
    )


def test_upload_candidate_sends_files_to_document_intake_and_returns_curated_result(test_client):
    result = _uploaded_candidate_result()
    with patch(
        "app.services.candidate_upload_intake.intake_uploaded_candidate",
        return_value=result,
    ) as intake, patch("app.api.routes.assign_candidate") as assign:
        assign.return_value = MagicMock(assigned=False)
        response = test_client.post(
            "/candidates/upload",
            files={
                "resume": ("meera.pdf", b"resume-bytes", "application/pdf"),
                "aadhaar": ("aadhaar.jpg", b"aadhaar-bytes", "image/jpeg"),
                "passport": ("passport.png", b"passport-bytes", "image/png"),
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["candidate"]["profile"]["full_name"] == "Meera Nair"
    assert "raw_ocr" not in body["candidate"]
    assert "raw_ocr" not in body["candidate"]["profile"]
    assert body["identity"]["aadhaar"][0]["masked_aadhaar_number"] == "XXXXXXXX9017"
    assert body["identity"]["passport"][0]["passport_number"] == "Z1234567"
    assert body["processed"] == ["resume", "aadhaar", "passport"]
    assert body["ocr_provider"] == "VeriIS"

    sent = intake.call_args.kwargs
    assert sent["resume"].data == b"resume-bytes"
    assert sent["aadhaar"].data == b"aadhaar-bytes"
    assert sent["passport"].data == b"passport-bytes"
    assert sent["uploader_id"] == "test-user"
    assign.assert_called_once()


def test_staff_upload_is_assigned_to_the_uploader(test_client):
    result = _uploaded_candidate_result()

    def persist_result(*, repository, **_kwargs):
        repository.insert(result.candidate)
        return result

    previous_user = app.dependency_overrides[current_user]
    app.dependency_overrides[current_user] = lambda: {
        "id": "staff-7",
        "email": "recruiter@example.com",
        "name": "Recruiter Seven",
        "role": "staff",
        "pages": ["candidates", "candidate-entry", "settings"],
    }
    try:
        with patch(
            "app.services.candidate_upload_intake.intake_uploaded_candidate",
            side_effect=persist_result,
        ), patch("app.api.routes.assign_candidate") as balance:
            response = test_client.post(
                "/candidates/upload",
                files={"resume": ("meera.pdf", b"resume-bytes", "application/pdf")},
            )
    finally:
        app.dependency_overrides[current_user] = previous_user

    assert response.status_code == 201
    assert response.json()["candidate"]["assigned_staff_id"] == "staff-7"
    assert response.json()["candidate"]["assigned_staff_name"] == "Recruiter Seven"
    balance.assert_not_called()


def test_upload_candidate_requires_a_resume(test_client):
    response = test_client.post(
        "/candidates/upload",
        files={"passport": ("passport.jpg", b"passport", "image/jpeg")},
    )
    assert response.status_code == 422


def test_upload_candidate_translates_intake_refusal(test_client):
    from app.services.candidate_upload_intake import CandidateUploadError

    with patch(
        "app.services.candidate_upload_intake.intake_uploaded_candidate",
        side_effect=CandidateUploadError(
            "The passport MRZ checksum failed. Upload a clearer passport scan.",
            status_code=422,
        ),
    ):
        response = test_client.post(
            "/candidates/upload",
            files={"resume": ("meera.pdf", b"resume", "application/pdf")},
        )

    assert response.status_code == 422
    assert "passport MRZ checksum failed" in response.json()["detail"]


def test_import_candidate_preserves_record_and_stores_matching_resume(test_client):
    imported = _uploaded_candidate_result().candidate.model_copy(
        update={"id": "candidate-imported", "resume_hash": "imported-hash"},
        deep=True,
    )
    imported.resume.sha256 = "imported-hash"
    payload = b"original-resume-bytes"
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    imported.resume.sha256 = digest
    imported.resume_hash = digest
    storage = MagicMock(name="import-storage")
    storage.name = "gridfs"

    with patch("app.api.routes.get_storage_backend", return_value=storage):
        response = test_client.post(
            "/candidates/import",
            data={"record_json": imported.model_dump_json()},
            files={"resume_file": ("meera.pdf", payload, "application/pdf")},
        )

    assert response.status_code == 201
    assert response.json() == {
        "status": "imported",
        "candidate_id": "candidate-imported",
        "resume_stored": True,
    }
    storage.save.assert_called_once()
    saved_key, saved_payload = storage.save.call_args.args[:2]
    assert saved_key.startswith("imports/candidate-imported/")
    assert saved_payload == payload


def test_import_candidate_rejects_resume_hash_mismatch(test_client):
    imported = _uploaded_candidate_result().candidate.model_copy(
        update={"id": "candidate-imported", "resume_hash": "expected-hash"},
        deep=True,
    )
    imported.resume.sha256 = "expected-hash"

    with patch("app.api.routes.get_storage_backend") as storage:
        response = test_client.post(
            "/candidates/import",
            data={"record_json": imported.model_dump_json()},
            files={"resume_file": ("meera.pdf", b"wrong-file", "application/pdf")},
        )

    assert response.status_code == 422
    assert "SHA-256" in response.json()["detail"]
    storage.return_value.save.assert_not_called()


def test_import_candidate_requires_explicit_missing_resume_acknowledgement(test_client):
    imported = _uploaded_candidate_result().candidate.model_copy(
        update={"id": "candidate-imported", "resume_hash": "missing-hash"},
        deep=True,
    )
    imported.resume.sha256 = "missing-hash"

    response = test_client.post(
        "/candidates/import",
        data={"record_json": imported.model_dump_json()},
    )

    assert response.status_code == 422
    assert "allow_missing_resume" in response.json()["detail"]


def test_import_candidate_allows_admin_to_preserve_record_with_lost_resume(test_client):
    imported = _uploaded_candidate_result().candidate.model_copy(
        update={"id": "candidate-imported", "resume_hash": "missing-hash"},
        deep=True,
    )
    imported.resume.sha256 = "missing-hash"
    storage = MagicMock(name="import-storage")
    storage.name = "gridfs"

    with patch("app.api.routes.get_storage_backend", return_value=storage):
        response = test_client.post(
            "/candidates/import",
            data={
                "record_json": imported.model_dump_json(),
                "allow_missing_resume": "true",
            },
        )

    assert response.status_code == 201
    assert response.json()["resume_stored"] is False
    storage.save.assert_not_called()


def test_import_candidate_is_forbidden_to_staff(test_client):
    imported = _uploaded_candidate_result().candidate.model_copy(
        update={"id": "candidate-imported", "resume_hash": "missing-hash"},
        deep=True,
    )
    imported.resume.sha256 = "missing-hash"
    previous_user = app.dependency_overrides[current_user]
    app.dependency_overrides[current_user] = lambda: {
        "id": "staff-7",
        "email": "recruiter@example.com",
        "name": "Recruiter Seven",
        "role": "staff",
        "pages": ["candidates", "candidate-entry"],
    }
    try:
        response = test_client.post(
            "/candidates/import",
            data={
                "record_json": imported.model_dump_json(),
                "allow_missing_resume": "true",
            },
        )
    finally:
        app.dependency_overrides[current_user] = previous_user

    assert response.status_code == 403


def test_manual_candidate_json_endpoint_is_removed(test_client):
    response = test_client.post(
        "/candidates/manual",
        json={"profile": {"full_name": "Meera Nair"}},
    )
    # The dynamic candidate-id route can make FastAPI report 405 for this
    # obsolete path; either result confirms there is no writable manual API.
    assert response.status_code in {404, 405}


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
         patch("app.email_client.get_email_client", return_value=gmail), \
         patch("app.email_client.get_all_email_clients", return_value=[gmail]):
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

