from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

from app.core.models import CandidateProfile, CandidateRecord
from app.db.mongo import _prepare_passport_keys
from app.db.repository import CandidateRepository


def test_legacy_passport_collisions_are_quarantined_before_unique_index():
    db = mongomock.MongoClient(tz_aware=True)["passport_dedup"]
    candidates = db["candidates"]
    passports = db["passport_records"]
    first_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    candidates.insert_many([
        {
            "_id": "first",
            "created_at": first_at,
            "profile": {"passport_number": "z 1234-567"},
            "status": "ingested",
        },
        {
            "_id": "second",
            "created_at": first_at + timedelta(days=1),
            "profile": {"passport_number": "Z1234567"},
            "passport_key": "Z1234567",
            "status": "ingested",
        },
    ])

    _prepare_passport_keys(candidates, passports)
    candidates.create_index("passport_key", unique=True, sparse=True)

    first = candidates.find_one({"_id": "first"})
    second = candidates.find_one({"_id": "second"})
    assert first["passport_key"] == "Z1234567"
    assert "passport_key" not in second
    assert second["status"] == "duplicate"
    assert second["duplicate_of"] == "first"
    assert first["identity_review"]["candidate_ids"] == ["first", "second"]

    with pytest.raises(DuplicateKeyError):
        candidates.insert_one({"_id": "third", "passport_key": "Z1234567"})


def test_passport_identity_collection_backfills_candidate_key():
    db = mongomock.MongoClient(tz_aware=True)["passport_identity_backfill"]
    candidates = db["candidates"]
    passports = db["passport_records"]
    candidates.insert_one({
        "_id": "email-candidate",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "profile": {},
    })
    passports.insert_one({
        "_id": "passport-read",
        "candidate_id": "email-candidate",
        "passport_number": "a 12-34567",
    })

    _prepare_passport_keys(candidates, passports)

    assert candidates.find_one({"_id": "email-candidate"})["passport_key"] == "A1234567"


def test_repository_returns_passport_owner_when_concurrent_insert_hits_unique_index():
    db = mongomock.MongoClient(tz_aware=True)["passport_insert_race"]
    candidates = db["candidates"]
    candidates.create_index("passport_key", unique=True, sparse=True)
    repository = CandidateRepository(collection=candidates)
    first = CandidateRecord(
        id="first",
        source="manual",
        profile=CandidateProfile(full_name="First"),
        passport_key="Z1234567",
        cv_required=False,
    )
    second = CandidateRecord(
        id="second",
        source="manual",
        profile=CandidateProfile(full_name="Second"),
        passport_key="Z1234567",
        cv_required=False,
    )

    assert repository.insert(first) == "first"
    assert repository.insert(second) == "first"
    assert candidates.count_documents({}) == 1
