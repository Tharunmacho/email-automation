"""One candidate per email address, enforced by the database.

The production failure: one application was delivered to two of the four polled
mailboxes, fetched as two messages, and became two candidates — two ids, two
allocations, two auto-replies, for one person.

`find_by_email_or_phone` was supposed to stop that and could not. It is a read
followed by an insert, and `ingestion_max_workers` runs the two messages at the
same time: both threads look for the address, both find nothing because neither
has inserted yet, and both insert. Only a unique index can settle that, which is
the same conclusion `idempotency_key` and `passport_key` already reached.

Phone stays deliberately non-unique — one number legitimately reaches several
candidates — so these tests pin the asymmetry down too.
"""
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

from app.core.models import CandidateProfile, CandidateRecord
from app.db.mongo import _prepare_email_keys
from app.db.repository import CandidateRepository


def _repo_with_unique_email():
    db = mongomock.MongoClient(tz_aware=True)["email_dedup"]
    candidates = db["candidates"]
    candidates.create_index("email_key", unique=True, sparse=True)
    return candidates, CandidateRepository(collection=candidates)


def _record(cid: str, name: str, email_key=None, phone_key=None):
    return CandidateRecord(
        id=cid,
        source="manual",
        profile=CandidateProfile(full_name=name),
        email_key=email_key,
        phone_key=phone_key,
        cv_required=False,
    )


def test_the_same_person_from_two_mailboxes_is_one_candidate():
    """The reported bug, in one assertion."""
    candidates, repository = _repo_with_unique_email()

    first = _record("from-cv-mailbox", "A. SARAVANAN", "saravanasaran724@gmail.com")
    second = _record("from-hr-mailbox", "A. SARAVANAN", "saravanasaran724@gmail.com")

    assert repository.insert(first) == "from-cv-mailbox"
    assert repository.insert(second) == "from-cv-mailbox", (
        "the second mailbox's copy created a second person"
    )
    assert candidates.count_documents({}) == 1


def test_candidates_without_an_email_are_not_forced_to_collide():
    """The sparse-index trap: `null` is a value, a missing field is not.

    `to_mongo` drops None, so these carry no `email_key` at all. If they carried
    `email_key: None` the unique index would admit exactly one of them, and
    every phone-only candidate after the first would be rejected outright.
    """
    candidates, repository = _repo_with_unique_email()

    assert repository.insert(_record("no-email-1", "One", None, "9000000001")) == "no-email-1"
    assert repository.insert(_record("no-email-2", "Two", None, "9000000002")) == "no-email-2"

    assert candidates.count_documents({}) == 2
    assert "email_key" not in candidates.find_one({"_id": "no-email-1"})


def test_one_phone_may_still_reach_several_candidates():
    """An agent's mobile on a family's applications must not merge them."""
    candidates, repository = _repo_with_unique_email()

    repository.insert(_record("sibling-a", "A", "a@example.com", "9000000001"))
    repository.insert(_record("sibling-b", "B", "b@example.com", "9000000001"))

    assert candidates.count_documents({}) == 2


def test_legacy_email_collisions_are_released_before_the_unique_index():
    """Existing duplicates must not block the index from building."""
    db = mongomock.MongoClient(tz_aware=True)["email_backfill"]
    candidates = db["candidates"]
    first_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidates.insert_many([
        {   # older, and carries only the address — no key yet
            "_id": "first",
            "created_at": first_at,
            "profile": {"email": " Saravanasaran724@Gmail.com "},
            "status": "ingested",
        },
        {
            "_id": "second",
            "created_at": first_at + timedelta(days=1),
            "profile": {"email": "saravanasaran724@gmail.com"},
            "email_key": "saravanasaran724@gmail.com",
            "status": "ingested",
        },
    ])

    _prepare_email_keys(candidates)
    candidates.create_index("email_key", unique=True, sparse=True)

    first = candidates.find_one({"_id": "first"})
    second = candidates.find_one({"_id": "second"})
    assert first["email_key"] == "saravanasaran724@gmail.com", "backfilled from profile"
    assert "email_key" not in second, "the newer record must release the key"
    assert second["duplicate_of"] == "first"
    assert first["identity_review"]["candidate_ids"] == ["first", "second"]

    with pytest.raises(DuplicateKeyError):
        candidates.insert_one({"_id": "third", "email_key": "saravanasaran724@gmail.com"})


def test_a_flagged_duplicate_keeps_its_place_in_the_crm():
    """Flagged, not hidden — an address read off a degraded scan is soft evidence.

    The passport routine sets `status: "duplicate"`; this one deliberately does
    not. OCR that drops pages under load also mis-reads addresses, and removing
    a real candidate from a recruiter's list on that evidence is the worse of
    the two errors.
    """
    db = mongomock.MongoClient(tz_aware=True)["email_flagging"]
    candidates = db["candidates"]
    first_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidates.insert_many([
        {"_id": "first", "created_at": first_at,
         "profile": {"email": "x@example.com"}, "status": "ingested"},
        {"_id": "second", "created_at": first_at + timedelta(days=1),
         "profile": {"email": "x@example.com"}, "status": "ingested"},
    ])

    _prepare_email_keys(candidates)

    second = candidates.find_one({"_id": "second"})
    assert second["status"] == "ingested", "a flagged duplicate must stay visible"
    assert second["identity_review"]["reason"] == "duplicate_email"


def test_a_lone_candidate_is_left_alone():
    """No collision, no flag."""
    db = mongomock.MongoClient(tz_aware=True)["email_solo"]
    candidates = db["candidates"]
    candidates.insert_one({
        "_id": "solo",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "profile": {"email": "solo@example.com"},
        "status": "ingested",
    })

    _prepare_email_keys(candidates)

    solo = candidates.find_one({"_id": "solo"})
    assert solo["email_key"] == "solo@example.com"
    assert "identity_review" not in solo
    assert "duplicate_of" not in solo
