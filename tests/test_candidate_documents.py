"""Saving an edited profile, replacing a CV, and candidate attachments.

Three things a reviewer reported as broken or missing:

  * "Save changes" failed for some candidates — the edit tripped the unique
    `email_key` index, either by writing a null key or by claiming an address
    another record already holds.
  * There was no way to upload an updated CV.
  * There was nowhere to attach anything else.
"""
from __future__ import annotations

from datetime import timedelta

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo import ASCENDING

from app.api import routes
from app.api.routes import app, current_user
from app.config import settings
from app.core.models import (
    CandidateAttachment,
    CandidateProfile,
    CandidateRecord,
    SourceEmail,
    StoredResume,
    utcnow,
)
from app.db.dedup import sha256_hex
from app.db.repository import CandidateRepository
from app.services import identity_files

ORIGINAL_CV = b"%PDF-1.4 original cv bytes"
UPDATED_CV = b"%PDF-1.4 updated cv bytes"

STAFF = {"id": "staff-1", "email": "ramya@example.com", "name": "Ramya", "role": "staff",
         "pages": ["candidates", "assigned-candidates"]}
OTHER_STAFF = {"id": "staff-2", "email": "ravi@example.com", "name": "Ravi", "role": "staff",
               "pages": ["candidates", "assigned-candidates"]}
ADMIN = {"id": "admin-1", "email": "admin@example.com", "name": "Admin", "role": "admin"}


@pytest.fixture
def coll():
    collection = mongomock.MongoClient(tz_aware=True)["docs_test"]["candidates"]
    collection.create_index([("email_key", ASCENDING)], name="email_key_unique", unique=True, sparse=True)
    collection.create_index([("resume_hash", ASCENDING)], name="resume_hash_unique", unique=True, sparse=True)
    return collection


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from app.storage.factory import get_storage_backend

    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_local_dir", str(tmp_path))
    # The factory caches backends by name, so one built by an earlier test
    # would still point at that test's directory.
    get_storage_backend.cache_clear()
    yield tmp_path
    get_storage_backend.cache_clear()


def _email_candidate(candidate_id: str, *, email: str | None, assigned: str = "staff-1",
                     cv: bytes = ORIGINAL_CV, storage_dir=None) -> CandidateRecord:
    key = f"2026/11/{candidate_id}_resume.pdf"
    if storage_dir is not None:
        path = storage_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cv)
    return CandidateRecord(
        id=candidate_id,
        profile=CandidateProfile(full_name=f"Person {candidate_id}", email=email),
        resume=StoredResume(
            original_filename="resume.pdf", mime_type="application/pdf", size=len(cv),
            sha256=sha256_hex(cv), storage_backend="local", storage_key=key,
        ),
        resume_hash=sha256_hex(cv),
        source_email=SourceEmail(message_id=f"m-{candidate_id}", thread_id="t", from_addr="hr@example.com"),
        email_key=email,
        assigned_staff_id=assigned,
    )


@pytest.fixture
def client(coll, storage, monkeypatch):
    monkeypatch.setattr(routes, "repo", lambda: CandidateRepository(collection=coll))
    user = {"current": STAFF}
    app.dependency_overrides[current_user] = lambda: user["current"]
    test_client = TestClient(app)
    test_client.user = user  # type: ignore[attr-defined]
    try:
        yield test_client
    finally:
        app.dependency_overrides.pop(current_user, None)


# --------------------------------------------------------------------------- #
#  Saving an edited profile
# --------------------------------------------------------------------------- #
def test_saving_a_profile_with_no_email_leaves_the_key_absent_not_null(coll):
    """A null `email_key` is indexed by a sparse index; a missing one is not."""
    coll.insert_one({"_id": "a", "profile": {}, "email_key": "old@example.com", "phone_key": "9876543210"})
    repo = CandidateRepository(collection=coll)

    conflict = repo.update_profile("a", CandidateProfile(full_name="Edited", email="", phone=""))

    doc = coll.find_one({"_id": "a"})
    assert conflict is None
    assert doc["profile"]["full_name"] == "Edited"
    assert "email_key" not in doc
    assert "phone_key" not in doc


def test_an_email_held_by_another_candidate_is_saved_but_not_claimed(coll):
    coll.insert_one({"_id": "owner", "profile": {"email": "x@example.com"}, "email_key": "x@example.com",
                     "candidate_code": "CAN-OWNER"})
    # The startup backfill released this record's key because of the collision.
    coll.insert_one({"_id": "dupe", "profile": {"email": "x@example.com"}})
    repo = CandidateRepository(collection=coll)

    conflict = repo.update_profile("dupe", CandidateProfile(full_name="Corrected", email="X@example.com"))

    assert conflict["_id"] == "owner"
    doc = coll.find_one({"_id": "dupe"})
    assert doc["profile"]["full_name"] == "Corrected"
    assert "email_key" not in doc
    assert coll.find_one({"_id": "owner"})["email_key"] == "x@example.com"


def test_a_candidate_keeps_claiming_its_own_email(coll):
    coll.insert_one({"_id": "a", "profile": {"email": "a@example.com"}, "email_key": "a@example.com"})
    repo = CandidateRepository(collection=coll)

    assert repo.update_profile("a", CandidateProfile(email="a@example.com", phone="+91 98765 43210")) is None
    doc = coll.find_one({"_id": "a"})
    assert doc["email_key"] == "a@example.com"
    assert doc["phone_key"] == "9876543210"


def test_profile_put_reports_a_shared_email_instead_of_failing(client, coll):
    coll.insert_one(_email_candidate("owner", email="x@example.com", assigned="staff-9",
                                     cv=b"%PDF owner's cv").to_mongo())
    coll.insert_one(_email_candidate("mine", email=None).to_mongo())

    response = client.put("/candidates/mine", json={"full_name": "Mine", "email": "x@example.com"})

    assert response.status_code == 200, response.text
    assert "already on candidate" in response.json()["notice"]
    assert coll.find_one({"_id": "mine"})["profile"]["email"] == "x@example.com"


# --------------------------------------------------------------------------- #
#  Replacing the CV
# --------------------------------------------------------------------------- #
def test_replacing_the_cv_keeps_the_old_one_as_a_previous_cv(client, coll, storage):
    coll.insert_one(_email_candidate("c1", email=None, storage_dir=storage).to_mongo())

    response = client.put(
        "/candidates/c1/resume",
        files={"file": ("resume.pdf", UPDATED_CV, "application/pdf")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resume"]["sha256"] == sha256_hex(UPDATED_CV)
    # Same filename, same month — and still two distinct files on disk.
    assert body["resume"]["storage_key"] != f"2026/11/c1_resume.pdf"
    [previous] = body["attachments"]
    assert previous["kind"] == "previous_cv"
    assert previous["sha256"] == sha256_hex(ORIGINAL_CV)
    assert (storage / previous["storage_key"]).read_bytes() == ORIGINAL_CV
    assert (storage / body["resume"]["storage_key"]).read_bytes() == UPDATED_CV
    # The resume download now serves the new file.
    assert client.get("/candidates/c1/resume").content == UPDATED_CV


def test_the_same_file_again_changes_nothing(client, coll, storage):
    coll.insert_one(_email_candidate("c1", email=None, storage_dir=storage).to_mongo())

    response = client.put("/candidates/c1/resume", files={"file": ("cv.pdf", ORIGINAL_CV, "application/pdf")})

    assert response.status_code == 200
    assert "already the CV" in response.json()["notice"]
    assert coll.find_one({"_id": "c1"}).get("attachments", []) == []


def test_a_cv_already_on_another_candidate_is_refused(client, coll, storage):
    coll.insert_one(_email_candidate("c1", email=None, storage_dir=storage).to_mongo())
    other = _email_candidate("c2", email=None, assigned="staff-9", cv=UPDATED_CV)
    coll.insert_one(other.to_mongo())

    response = client.put("/candidates/c1/resume", files={"file": ("cv.pdf", UPDATED_CV, "application/pdf")})

    assert response.status_code == 409
    assert other.candidate_code in response.json()["detail"]
    assert coll.find_one({"_id": "c1"})["resume_hash"] == sha256_hex(ORIGINAL_CV)


def test_staff_cannot_replace_the_cv_of_someone_elses_candidate(client, coll, storage):
    coll.insert_one(_email_candidate("c1", email=None, assigned="staff-2", storage_dir=storage).to_mongo())

    response = client.put("/candidates/c1/resume", files={"file": ("cv.pdf", UPDATED_CV, "application/pdf")})

    assert response.status_code == 404


def test_a_replaced_cv_is_still_recognised_by_its_hash(coll):
    coll.insert_one(_email_candidate("c1", email=None).to_mongo())
    repo = CandidateRepository(collection=coll)
    previous = CandidateAttachment(
        id="p1", kind="previous_cv", original_filename="resume.pdf", mime_type="application/pdf",
        size=1, sha256=sha256_hex(ORIGINAL_CV), storage_backend="local", storage_key="k",
    )
    repo.replace_resume("c1", StoredResume(
        original_filename="new.pdf", mime_type="application/pdf", size=1,
        sha256=sha256_hex(UPDATED_CV), storage_backend="local", storage_key="k2",
    ), previous)

    assert repo.find_by_resume_hash(sha256_hex(ORIGINAL_CV)).id == "c1"
    assert repo.find_by_resume_hash(sha256_hex(UPDATED_CV)).id == "c1"


def test_identity_pages_are_still_cut_from_the_cv_they_were_read_from():
    record = _email_candidate("c1", email=None, cv=UPDATED_CV)
    old = CandidateAttachment(
        id="p1", kind="previous_cv", original_filename="bundle.pdf", mime_type="application/pdf",
        size=1, sha256=sha256_hex(ORIGINAL_CV), storage_backend="local", storage_key="old-key",
        uploaded_at=utcnow() - timedelta(days=1),
    )
    record.attachments = [old]

    fingerprinted = {"_id": "p", "source": {"sha256": sha256_hex(ORIGINAL_CV), "pages": [3]}}
    legacy = {"_id": "q", "source": {"pages": [3]}}
    current = {"_id": "r", "source": {"sha256": sha256_hex(UPDATED_CV)}}

    assert identity_files._bundle_for(record, fingerprinted).storage_key == "old-key"
    # No fingerprint: read at ingestion, so from the CV the candidate arrived with.
    assert identity_files._bundle_for(record, legacy).storage_key == "old-key"
    assert identity_files._bundle_for(record, current) is record.resume


# --------------------------------------------------------------------------- #
#  Attachments
# --------------------------------------------------------------------------- #
def test_attach_download_and_remove_a_document(client, coll, storage):
    coll.insert_one(_email_candidate("c1", email=None, storage_dir=storage).to_mongo())

    added = client.post(
        "/candidates/c1/attachments",
        files={"file": ("Welding cert.pdf", b"%PDF certificate", "application/pdf")},
        data={"kind": "certificate", "label": "6G welding"},
    )
    assert added.status_code == 201, added.text
    [attachment] = added.json()["attachments"]
    assert attachment["kind"] == "certificate"
    assert attachment["label"] == "6G welding"
    assert attachment["uploaded_by_name"] == "Ramya"

    download = client.get(f"/candidates/c1/attachments/{attachment['id']}")
    assert download.status_code == 200
    assert download.content == b"%PDF certificate"

    removed = client.delete(f"/candidates/c1/attachments/{attachment['id']}")
    assert removed.status_code == 200
    assert removed.json()["attachments"] == []
    assert not (storage / attachment["storage_key"]).exists()


def test_attachment_kind_must_be_one_a_recruiter_can_choose(client, coll, storage):
    coll.insert_one(_email_candidate("c1", email=None, storage_dir=storage).to_mongo())

    for kind in ("previous_cv", "nonsense"):
        response = client.post(
            "/candidates/c1/attachments",
            files={"file": ("x.pdf", b"%PDF x", "application/pdf")},
            data={"kind": kind},
        )
        assert response.status_code == 422


def test_staff_may_remove_only_their_own_uploads(client, coll, storage):
    record = _email_candidate("c1", email=None, storage_dir=storage)
    record.attachments = [
        CandidateAttachment(id="theirs", kind="photo", original_filename="p.jpg", mime_type="image/jpeg",
                            size=1, sha256="s", storage_backend="local", storage_key="k1",
                            uploaded_by="staff-2"),
        CandidateAttachment(id="old-cv", kind="previous_cv", original_filename="r.pdf",
                            mime_type="application/pdf", size=1, sha256="t",
                            storage_backend="local", storage_key="k2"),
    ]
    coll.insert_one(record.to_mongo())

    assert client.delete("/candidates/c1/attachments/theirs").status_code == 403
    assert client.delete("/candidates/c1/attachments/old-cv").status_code == 409

    client.user["current"] = ADMIN
    assert client.delete("/candidates/c1/attachments/theirs").status_code == 200
    # Not even an administrator removes a replaced CV.
    assert client.delete("/candidates/c1/attachments/old-cv").status_code == 409
