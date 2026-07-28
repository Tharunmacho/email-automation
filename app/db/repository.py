"""Persistence for candidate records + duplicate lookups.

The pipeline talks only to this repository, never to PyMongo directly, so the
storage engine could change without touching business logic.
"""
from __future__ import annotations

from typing import List, Optional

from pymongo.errors import DuplicateKeyError

from app.core.models import CandidateProfile, CandidateRecord
from app.db.mongo import get_candidates_collection
from app.logging_config import get_logger

log = get_logger(__name__)


class CandidateRepository:
    def __init__(self, collection=None):
        self._coll = collection or get_candidates_collection()

    # ---- lookups ---------------------------------------------------------- #
    def find_by_message_id(self, message_id: str) -> Optional[CandidateRecord]:
        doc = self._coll.find_one({"source_email.message_id": message_id})
        return CandidateRecord.from_mongo(doc) if doc else None

    def find_by_resume_hash(self, resume_hash: str) -> Optional[CandidateRecord]:
        doc = self._coll.find_one({"resume_hash": resume_hash})
        return CandidateRecord.from_mongo(doc) if doc else None

    def find_by_email_or_phone(
        self, email_key: Optional[str], phone_key: Optional[str]
    ) -> Optional[CandidateRecord]:
        ors = []
        if email_key:
            ors.append({"email_key": email_key})
        if phone_key:
            ors.append({"phone_key": phone_key})
        if not ors:
            return None
        doc = self._coll.find_one({"$or": ors})
        return CandidateRecord.from_mongo(doc) if doc else None

    # ---- writes ----------------------------------------------------------- #
    def insert(self, record: CandidateRecord) -> str:
        try:
            self._coll.insert_one(record.to_mongo())
        except DuplicateKeyError:
            # Lost a race on resume_hash uniqueness — treat as duplicate.
            existing = self.find_by_resume_hash(record.resume_hash)
            if existing:
                return existing.id
            raise
        log.info("Inserted candidate %s (%s)", record.id, record.profile.full_name)
        return record.id

    def update_status(self, candidate_id: str, status: str, duplicate_of: Optional[str] = None) -> None:
        from app.core.models import utcnow

        self._coll.update_one(
            {"_id": candidate_id},
            {"$set": {"status": status, "duplicate_of": duplicate_of, "updated_at": utcnow()}},
        )

    def update_profile(self, candidate_id: str, profile: CandidateProfile) -> None:
        from app.core.models import utcnow
        from app.db.dedup import normalize_email, normalize_phone

        email_key = normalize_email(profile.email)
        phone_key = normalize_phone(profile.phone)

        self._coll.update_one(
            {"_id": candidate_id},
            {
                "$set": {
                    "profile": profile.model_dump(mode="python"),
                    "email_key": email_key,
                    "phone_key": phone_key,
                    "updated_at": utcnow(),
                }
            },
        )

    def mark_auto_reply_sent(self, candidate_id: str) -> None:
        from app.core.models import utcnow

        self._coll.update_one(
            {"_id": candidate_id},
            {"$set": {"auto_reply_sent": True, "updated_at": utcnow()}},
        )


    # ---- read APIs (extension seam for search/dashboard) ------------------ #
    def list_candidates(self, limit: int = 50, skip: int = 0) -> List[CandidateRecord]:
        cursor = self._coll.find().sort("created_at", -1).skip(skip).limit(limit)
        return [CandidateRecord.from_mongo(d) for d in cursor]

    def get(self, candidate_id: str) -> Optional[CandidateRecord]:
        doc = self._coll.find_one({"_id": candidate_id})
        return CandidateRecord.from_mongo(doc) if doc else None

    def count(self) -> int:
        return self._coll.count_documents({})
