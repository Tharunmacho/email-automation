"""Atomic duplicate prevention shared by every candidate intake route."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import mongomock

from app.core.models import CandidateProfile, CandidateRecord
from app.db.dedup import normalize_phone
from app.db.repository import CandidateRepository


def _record(candidate_id: str, phone: str, *, name: str = "Nishar B") -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id,
        source="whatsapp",
        profile=CandidateProfile(
            is_resume=False,
            confidence=0.0,
            full_name=name,
            phone=phone,
            phone_e164=phone,
        ),
        phone_key=normalize_phone(phone),
        idempotency_key=f"submission/{candidate_id}",
        cv_required=False,
    )


def test_simultaneous_intake_of_same_phone_creates_one_candidate():
    database = mongomock.MongoClient(tz_aware=True)["candidate_dedup_test"]
    collection = database["candidates"]
    first = _record("candidate-a", "+91 98765 43210")
    second = _record("candidate-b", "+919876543210")

    # Separate repositories model separate intake routes sharing one database.
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(
            pool.map(
                lambda item: CandidateRepository(collection=collection).insert(item),
                (first, second),
            )
        )

    assert ids[0] == ids[1]
    assert collection.count_documents({}) == 1
    assert database["candidate_creation_locks"].count_documents({}) == 0


def test_same_name_with_different_phone_remains_two_candidates():
    database = mongomock.MongoClient(tz_aware=True)["candidate_dedup_test"]
    repository = CandidateRepository(collection=database["candidates"])

    first_id = repository.insert(_record("candidate-a", "+919876543210"))
    second_id = repository.insert(_record("candidate-b", "+919999999999"))

    assert first_id != second_id
    assert database["candidates"].count_documents({}) == 2


def test_equal_last_ten_digits_in_different_countries_do_not_merge():
    database = mongomock.MongoClient(tz_aware=True)["candidate_dedup_test"]
    repository = CandidateRepository(collection=database["candidates"])

    first_id = repository.insert(_record("candidate-a", "+60 12 345 67890"))
    second_id = repository.insert(_record("candidate-b", "+91 12 345 67890"))

    assert first_id != second_id
    assert database["candidates"].count_documents({}) == 2
