import mongomock

from app.config import settings
from app.core.models import CandidateProfile, CandidateRecord, StoredResume
from app.db.repository import CANDIDATE_DELETIONS_COLLECTION, CandidateRepository
from app.db.users import USER_DELETIONS_COLLECTION, UserRepository


def test_candidate_hard_delete_removes_related_rows_and_keeps_a_retry_tombstone():
    db = mongomock.MongoClient(tz_aware=True)["resume_ats_test"]
    repository = CandidateRepository(collection=db["candidates"])
    record = CandidateRecord(
        id="candidate-1",
        source="manual",
        profile=CandidateProfile(
            full_name="Meera Nair",
            email="meera@example.com",
            phone="+91 98765 43210",
        ),
        email_key="meera@example.com",
        phone_key="9876543210",
        resume=StoredResume(
            original_filename="meera.pdf",
            mime_type="application/pdf",
            size=12,
            sha256="resume-digest",
            storage_backend="gridfs",
            storage_key="resume-key",
        ),
        resume_hash="resume-digest",
        cv_required=False,
    )
    repository.insert(record)

    for collection_name in (
        settings.mongo_aadhaar_collection,
        settings.mongo_passport_collection,
        settings.mongo_document_collection,
    ):
        db[collection_name].insert_one({
            "candidate_id": record.id,
            "file": {
                "storage_backend": "gridfs",
                "storage_key": f"{collection_name}/file",
            },
        })
    db["notifications"].insert_one({"candidate_id": record.id})
    db["sla_alerts"].insert_one({"candidate_id": record.id})
    db["ingestion_state"].insert_one({"candidate_id": record.id})

    repository.record_deletion(record)
    assert repository.delete(record.id) is True
    related = repository.delete_related(record.id)

    assert repository.get(record.id) is None
    assert related["identity_records"] == 3
    assert related["notifications"] == 1
    assert related["sla_alerts"] == 1
    assert related["ingestion_state"] == 1
    assert len(related["storage_refs"]) == 3
    assert repository.was_deleted(email_key="meera@example.com") is True
    assert repository.was_deleted(phone_key="9876543210") is True
    assert repository.was_deleted(resume_hash="resume-digest") is True

    tombstone = db[CANDIDATE_DELETIONS_COLLECTION].find_one({"_id": record.id})
    assert tombstone is not None
    assert "Meera Nair" not in str(tombstone)
    assert "meera@example.com" not in str(tombstone)
    assert "+91 98765 43210" not in str(tombstone)


def test_user_hard_delete_removes_notifications_and_prevents_auto_reseed():
    db = mongomock.MongoClient(tz_aware=True)["resume_ats_test"]
    repository = UserRepository(collection=db["users"])
    user = repository.create(
        email="recruiter@example.com",
        password="safe-password",
        name="Recruiter",
        role="staff",
    )
    db["notifications"].insert_one({"user_id": user.id, "title": "Assigned"})

    assert repository.delete_user(user.id) is True
    assert repository.get(user.id) is None
    assert db["notifications"].count_documents({"user_id": user.id}) == 0
    assert repository.was_deleted_email("recruiter@example.com") is True

    tombstone = db[USER_DELETIONS_COLLECTION].find_one({"_id": user.id})
    assert tombstone is not None
    assert "recruiter@example.com" not in str(tombstone)
    assert set(tombstone) == {"_id", "email_fingerprint", "deleted_at"}

    restored = repository.create(
        email="recruiter@example.com",
        password="new-safe-password",
        name="Recruiter Restored",
        role="staff",
    )
    assert restored.id != user.id
    assert repository.get(restored.id).email == "recruiter@example.com"
    assert repository.was_deleted_email("recruiter@example.com") is False
