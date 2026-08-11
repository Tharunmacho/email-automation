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


# --------------------------------------------------------------------------- #
#  Listing projections
# --------------------------------------------------------------------------- #
# A candidate document carries the OCR provider's verbatim response under
# `raw_ocr`, and a second copy of it under `profile.raw_ocr`. That payload is
# the whole document text plus per-page text — tens to hundreds of kilobytes
# each, so a page of 200 rows was moving megabytes out of Atlas, through
# Pydantic, and over the wire every few seconds. Nothing in a list row reads it.
#
# The fields are named rather than excluded on purpose: an allow-list cannot be
# defeated by someone adding a new blob to the schema tomorrow. Anything not
# named here is available from `GET /candidates/{id}`.
LIST_PROJECTION = {
    "_id": 1,
    "status": 1,
    "duplicate_of": 1,
    "auto_reply_sent": 1,
    "email_key": 1,
    "phone_key": 1,
    "resume_hash": 1,
    "created_at": 1,
    "updated_at": 1,
    # Identity and the columns the directory sorts, searches and filters on.
    "profile.full_name": 1,
    "profile.email": 1,
    "profile.phone": 1,
    "profile.confidence": 1,
    "profile.location": 1,
    "profile.skills": 1,
    "profile.technical_skills": 1,
    "profile.languages": 1,
    "profile.current_designation": 1,
    "profile.current_company": 1,
    "profile.total_experience_years": 1,
    "profile.work_experience": 1,
    "profile.resume_summary": 1,
    # Enough of the attachment to name it and report how it was read.
    "resume.original_filename": 1,
    "resume.extraction_method": 1,
    "resume.ocr_used": 1,
    # Who sent it — the fallback for a résumé with no name or address in it.
    "source_email.from_name": 1,
    "source_email.from_addr": 1,
    "source_email.subject": 1,
}

# The narrowest useful row: identity, state, and when it arrived. For callers
# that only need to enumerate candidates — counts, pickers, exports — and want
# nothing they did not ask for.
MINIMAL_PROJECTION = {
    "_id": 1,
    "status": 1,
    "created_at": 1,
    "profile.full_name": 1,
    "profile.email": 1,
    "profile.phone": 1,
    "profile.confidence": 1,
}


def _minimal_row(doc: dict) -> dict:
    """Flatten a minimally-projected document into the listing contract."""
    profile = doc.get("profile") or {}
    return {
        "id": doc["_id"],
        "full_name": profile.get("full_name"),
        "email": profile.get("email"),
        "phone": profile.get("phone"),
        "status": doc.get("status"),
        "confidence": profile.get("confidence"),
        "created_at": doc.get("created_at"),
    }


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

        # Ensure work_experience items have both title and designation set
        if profile.work_experience:
            for exp in profile.work_experience:
                if exp.designation and not exp.title:
                    exp.title = exp.designation
                elif exp.title and not exp.designation:
                    exp.designation = exp.title

        profile_dump = profile.model_dump(mode="python")

        # `raw_ocr` is the extractor's verbatim output, not a mirror of the
        # edited profile. It used to be rewritten here on every save — the
        # profile was copied into `raw_ocr["profile"]`, which on the next save
        # was copied in again *inside* that copy, so the document grew a nested
        # doll of itself and the Raw JSON tab no longer showed what Veris
        # returned. The stored payload is now immutable: an edit changes
        # `profile`, never `raw_ocr`.
        existing_doc = self._coll.find_one({"_id": candidate_id})
        stored_raw = existing_doc.get("raw_ocr") if existing_doc else None
        if not isinstance(stored_raw, dict) or not stored_raw:
            stored_raw = getattr(profile, "raw_ocr", None)

        # Both copies of the payload (record-level and profile-level) must stay
        # byte-identical; the incoming profile carries whatever the browser sent
        # back, so the stored one wins.
        profile_dump["raw_ocr"] = stored_raw if isinstance(stored_raw, dict) and stored_raw else None

        update_dict = {
            "profile": profile_dump,
            "email_key": email_key,
            "phone_key": phone_key,
            "updated_at": utcnow(),
        }
        if isinstance(stored_raw, dict) and stored_raw:
            update_dict["raw_ocr"] = stored_raw

        self._coll.update_one(
            {"_id": candidate_id},
            {"$set": update_dict},
        )

    def mark_auto_reply_sent(self, candidate_id: str) -> None:
        from app.core.models import utcnow

        self._coll.update_one(
            {"_id": candidate_id},
            {"$set": {"auto_reply_sent": True, "updated_at": utcnow()}},
        )


    # ---- read APIs (extension seam for search/dashboard) ------------------ #
    def list_candidates(self, limit: int = 50, skip: int = 0) -> List[CandidateRecord]:
        """Whole documents, validated. For the CLI and scripts, not for the API.

        A list page served from this pays for the OCR payload twice over —
        once out of Atlas, once through Pydantic. `list_summaries` is what the
        `/candidates` endpoint uses.
        """
        cursor = self._coll.find().sort("created_at", -1).skip(skip).limit(limit)
        return [CandidateRecord.from_mongo(d) for d in cursor]

    def list_summaries(
        self, limit: int = 50, skip: int = 0, minimal: bool = False
    ) -> List[dict]:
        """A page of list rows, projected in the database.

        Returns plain dicts rather than `CandidateRecord`s, and deliberately so:
        a projected document is not a whole record, and validating it back into
        one would either fail on the missing required fields or force them to be
        made optional everywhere. The shape mirrors the record — `id` where
        Mongo has `_id` — so a list row reads the same as a detail response for
        the fields it does carry.
        """
        projection = MINIMAL_PROJECTION if minimal else LIST_PROJECTION
        cursor = (
            self._coll.find({}, projection)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        if minimal:
            return [_minimal_row(doc) for doc in cursor]

        rows = []
        for doc in cursor:
            doc["id"] = doc.pop("_id")
            rows.append(doc)
        return rows

    def get(self, candidate_id: str) -> Optional[CandidateRecord]:
        doc = self._coll.find_one({"_id": candidate_id})
        return CandidateRecord.from_mongo(doc) if doc else None

    def delete(self, candidate_id: str) -> bool:
        res = self._coll.delete_one({"_id": candidate_id})
        return res.deleted_count > 0

    def count(self) -> int:
        return self._coll.count_documents({})
