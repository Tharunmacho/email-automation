from __future__ import annotations

import mongomock

from app.config import Settings
from app.db.consolidation import consolidate_adira_into_resume_ats


def test_adira_environment_value_is_redirected_to_resume_ats():
    settings = Settings(_env_file=None, mongo_db="adira")
    assert settings.mongo_db == "resume_ats"


def test_every_collection_is_merged_verified_and_legacy_database_is_dropped():
    client = mongomock.MongoClient()
    legacy = client["adira"]
    canonical = client["resume_ats"]

    legacy["candidates"].insert_many([
        {"_id": "legacy-candidate", "name": "Legacy"},
        {"_id": "shared-candidate", "name": "Newer source value"},
    ])
    canonical["candidates"].insert_one(
        {"_id": "shared-candidate", "name": "Old target value"}
    )

    legacy["users"].create_index("email", unique=True)
    canonical["users"].create_index("email", unique=True)
    legacy["users"].insert_one(
        {"_id": "legacy-admin", "email": "admin@example.com", "name": "Legacy Admin"}
    )
    canonical["users"].insert_one(
        {"_id": "canonical-admin", "email": "admin@example.com", "name": "Seed Admin"}
    )

    # GridFS collections are ordinary MongoDB collections for consolidation.
    legacy["resumes.files"].insert_one({"_id": "resume-key", "length": 4})
    legacy["resumes.chunks"].insert_one(
        {"_id": "chunk-id", "files_id": "resume-key", "n": 0, "data": b"data"}
    )

    result = consolidate_adira_into_resume_ats(client, drop_legacy=True)

    assert result["verified"] is True
    assert result["missing_documents"] == 0
    assert result["legacy_dropped"] is True
    assert "adira" not in client.list_database_names()
    assert canonical["candidates"].find_one({"_id": "legacy-candidate"})
    assert canonical["candidates"].find_one({"_id": "shared-candidate"})["name"] == "Newer source value"
    assert canonical["users"].find_one({"_id": "canonical-admin"})["name"] == "Legacy Admin"
    assert canonical["resumes.files"].find_one({"_id": "resume-key"})
    assert canonical["resumes.chunks"].find_one({"files_id": "resume-key"})["data"] == b"data"
