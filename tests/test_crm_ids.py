from __future__ import annotations

import mongomock

from app.core.crm_ids import candidate_code, staff_code
from app.core.models import CandidateProfile, CandidateRecord
from app.db.repository import CandidateRepository
from app.db.users import ADMIN_ROLE, STAFF_ROLE, UserRepository


def test_public_codes_are_stable_and_role_specific():
    assert candidate_code("same-record") == candidate_code("same-record")
    assert candidate_code("same-record").startswith("CAN-")
    assert staff_code("same-record").startswith("STF-")
    assert candidate_code("same-record")[4:] == staff_code("same-record")[4:]


def test_candidate_model_persists_public_code():
    record = CandidateRecord(
        id="candidate-legacy-key",
        source="whatsapp",
        profile=CandidateProfile(full_name="Asha Kumar"),
        cv_required=False,
    )

    assert record.candidate_code == candidate_code(record.id)
    assert record.to_mongo()["candidate_code"] == candidate_code(record.id)


def test_candidate_summary_supplies_code_for_legacy_document():
    collection = mongomock.MongoClient()["crm_ids"]["candidates"]
    collection.insert_one({
        "_id": "legacy-candidate",
        "profile": {"full_name": "Legacy Candidate"},
        "status": "ingested",
    })

    row = CandidateRepository(collection=collection).list_summaries()[0]

    assert row["candidate_code"] == candidate_code("legacy-candidate")


def test_staff_creation_persists_and_exposes_staff_code():
    collection = mongomock.MongoClient()["crm_ids"]["users"]
    member = UserRepository(collection=collection).create(
        email="reviewer@example.com",
        password="safe-password",
        name="Reviewer",
        role=STAFF_ROLE,
    )

    stored = collection.find_one({"_id": member.id})
    assert member.staff_code == staff_code(member.id)
    assert stored["staff_code"] == member.staff_code
    assert member.to_public()["staff_code"] == member.staff_code


def test_admin_accounts_do_not_receive_staff_ids():
    collection = mongomock.MongoClient()["crm_ids"]["users"]
    admin = UserRepository(collection=collection).create(
        email="admin@example.com",
        password="safe-password",
        name="Administrator",
        role=ADMIN_ROLE,
    )

    assert "staff_code" not in collection.find_one({"_id": admin.id})
    assert admin.to_public()["staff_code"] is None
