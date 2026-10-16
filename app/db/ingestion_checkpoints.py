"""Resume intake retries, separate from the OCR job's own lifecycle.

A paid extraction can succeed while the candidate write fails. Keep its parsed
answer for retries and put a ceiling on failed intake attempts. Quarantine
metadata survives cache expiry so expiry can never restart a broken email.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from pymongo import ASCENDING, ReturnDocument

from app.core.models import CandidateProfile, ExtractedDocument, utcnow
from app.db.mongo import get_db

CHECKPOINT_COLLECTION = "ingestion_checkpoints"
EXTRACTION_COLLECTION = "ingestion_extractions"
MAX_ATTEMPTS = 3
CACHE_DAYS = 30


def get_checkpoint_collection():
    return get_db()[CHECKPOINT_COLLECTION]


def get_extraction_collection():
    return get_db()[EXTRACTION_COLLECTION]


@dataclass
class IntakeClaim:
    id: str
    token: str = ""
    row: Optional[dict] = None

    @property
    def acquired(self) -> bool:
        return bool(self.token)


class IngestionCheckpointStore:
    def __init__(self, collection=None, extractions=None):
        self._coll = collection if collection is not None else get_checkpoint_collection()
        self._extractions = extractions if extractions is not None else get_extraction_collection()

    def begin(self, account_id: str, message_id: str, attachment_id: str,
              sha256: str, filename: str, *, lease_seconds: float = 1800) -> IntakeClaim:
        now = utcnow()
        identity = "\x1f".join(("email", account_id, message_id, attachment_id, sha256))
        id_ = hashlib.sha256(identity.encode()).hexdigest()
        self._coll.update_one({"_id": id_}, {"$setOnInsert": {
            "account_id": account_id, "message_id": message_id,
            "attachment_id": attachment_id, "sha256": sha256, "filename": filename,
            "status": "retry", "attempts": 0, "last_error": "",
            "received_at": now, "updated_at": now,
            "next_retry_at": now, "lease_until": now,
        }}, upsert=True)
        # A worker that died spends one attempt too. Only an expired lease can
        # quarantine it; a healthy third attempt must be allowed to finish.
        self._coll.update_one({"_id": id_, "status": {"$in": ["retry", "processing"]},
                               "attempts": {"$gte": MAX_ATTEMPTS},
                               "lease_until": {"$lte": now}}, {"$set": {
            "status": "quarantined", "updated_at": now,
        }})
        self._coll.update_one({"_id": id_, "status": "quarantined", "last_error": ""},
                              {"$set": {"last_error": "Intake worker lease expired after maximum attempts"}})
        token = uuid.uuid4().hex
        row = self._coll.find_one_and_update({
            "_id": id_, "status": {"$in": ["retry", "processing"]},
            "attempts": {"$lt": MAX_ATTEMPTS}, "next_retry_at": {"$lte": now},
            "lease_until": {"$lte": now},
        }, {"$set": {
            "status": "processing", "lease_token": token,
            "lease_until": now + timedelta(seconds=max(60, lease_seconds)),
            "updated_at": now,
        }, "$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
        return IntakeClaim(id_, token if row else "", row or self._coll.find_one({"_id": id_}))

    def load_extraction(self, claim: IntakeClaim, sha256: str):
        if claim.acquired:
            current = self._coll.find_one({
                "_id": claim.id, "lease_token": claim.token, "status": "processing",
                "lease_until": {"$gt": utcnow()},
            })
            if current is None:
                raise RuntimeError("Intake lease was lost before reading saved extraction")
            claim.row = current
        extraction_id = (claim.row or {}).get("extraction_id") or claim.id
        doc = self._extractions.find_one({"_id": extraction_id, "sha256": sha256,
                                          "version": 1, "expires_at": {"$gt": utcnow()}})
        if not doc:
            if (claim.row or {}).get("extraction_saved") and (claim.row or {}).get("status") != "complete":
                raise RuntimeError("Saved extraction expired or is missing; operator review is required")
            return None
        # Invalid cached data is an intake error, never permission to spend on
        # another extraction. An operator can inspect/reset the checkpoint.
        return (CandidateProfile.model_validate(doc["profile"]),
                ExtractedDocument.model_validate(doc["extracted"]))

    def save_extraction(self, claim: IntakeClaim, sha256: str,
                        profile: CandidateProfile, extracted: ExtractedDocument) -> None:
        profile_data = CandidateProfile.model_validate(profile).model_dump(mode="json")
        extracted_data = ExtractedDocument.model_validate(extracted).model_dump(mode="json")
        now = utcnow()
        marked = self._coll.update_one({"_id": claim.id, "lease_token": claim.token,
                                        "status": "processing", "lease_until": {"$gt": now}},
                                       {"$set": {"extraction_saved": True}})
        if not marked.matched_count:
            raise RuntimeError("Intake lease was lost before checkpointing extraction")
        # Mark first: if persisting the payload fails, a retry must stop for
        # review rather than interpret a missing cache as permission to pay again.
        # Each lease writes its own immutable document. A delayed expired
        # worker cannot overwrite a later owner's payload, even if its write
        # reaches Mongo after that owner has finished saving.
        extraction_id = f"{claim.id}:{claim.token}"
        self._extractions.update_one({"_id": extraction_id}, {"$setOnInsert": {
            "checkpoint_id": claim.id, "version": 1, "sha256": sha256, "profile": profile_data,
            "extracted": extracted_data, "expires_at": now + timedelta(days=CACHE_DAYS),
        }}, upsert=True)
        published = self._coll.update_one({
            "_id": claim.id, "lease_token": claim.token, "status": "processing",
            "lease_until": {"$gt": utcnow()},
        }, {"$set": {"extraction_id": extraction_id}})
        if not published.matched_count:
            self._extractions.delete_one({"_id": extraction_id})
            raise RuntimeError("Intake lease was lost before publishing saved extraction")
        if claim.row is not None:
            claim.row["extraction_id"] = extraction_id
            claim.row["extraction_saved"] = True

    def finish(self, claim: IntakeClaim, result) -> None:
        settled = self._coll.find_one_and_update({
            "_id": claim.id, "lease_token": claim.token, "status": "processing",
            "lease_until": {"$gt": utcnow()},
        }, {"$set": {
            "status": "complete", "outcome": result.status,
            "candidate_id": result.candidate_id, "detail": result.detail[:2000],
            "last_error": "", "updated_at": utcnow(),
        }, "$unset": {"lease_token": ""}}, return_document=ReturnDocument.AFTER)
        if settled is None:
            raise RuntimeError("Intake lease was lost before completing checkpoint")
        # Once settled there is no reason to retain a second profile copy.
        # Supporting-document reads can still be needed by a sibling CV on a
        # later retry of this message. Their copies remain subject to the TTL.
        if result.source is None:
            self._extractions.delete_one({"_id": settled.get("extraction_id") or claim.id})

    def fail(self, claim: IntakeClaim, error: str, *, pending_ocr: bool = False) -> str:
        attempts = int((claim.row or {}).get("attempts", 1))
        status = "retry" if pending_ocr or attempts < MAX_ATTEMPTS else "quarantined"
        now = utcnow()
        update: dict[str, Any] = {"$set": {
            "status": status, "last_error": error[:2000], "updated_at": now,
            "lease_until": now,
            "next_retry_at": now + timedelta(seconds=90 * (2 ** max(0, attempts - 1))),
        }, "$unset": {"lease_token": ""}}
        if pending_ocr:
            # Waiting for an accepted job is not a failed intake attempt.
            update["$inc"] = {"attempts": -1}
        settled = self._coll.update_one({
            "_id": claim.id, "lease_token": claim.token, "status": "processing",
            "lease_until": {"$gt": now},
        }, update)
        if not settled.matched_count:
            current = self._coll.find_one({"_id": claim.id}) or {}
            return current.get("status", "retry")
        return status

    def review_queue(self, limit: int = 100) -> list[dict]:
        projection = {"lease_token": 0, "sha256": 0, "attachment_id": 0, "extraction_id": 0}
        rows = self._coll.find({"status": "quarantined"}, projection).sort(
            "updated_at", ASCENDING).limit(max(1, min(1000, int(limit))))
        return [{"checkpoint_id": row.pop("_id"), **row} for row in rows]

    def reset_for_retry(self, id_: str) -> bool:
        now = utcnow()
        row = self._coll.find_one({"_id": id_, "status": "quarantined"})
        if row is None:
            return False
        update = {"$set": {
            "status": "retry", "attempts": 0, "last_error": "",
            "next_retry_at": now, "lease_until": now, "updated_at": now,
        }}
        # Retain an unexpired answer. An explicit operator retry after its
        # retention period can acquire a fresh answer instead.
        extraction_id = row.get("extraction_id") or id_
        if not self._extractions.find_one({"_id": extraction_id, "expires_at": {"$gt": now}}):
            update["$unset"] = {"extraction_saved": "", "extraction_id": ""}
        result = self._coll.update_one({"_id": id_, "status": "quarantined"}, update)
        return result.matched_count == 1


def ensure_ingestion_checkpoint_indexes() -> None:
    from app.db.mongo import ensure_index

    ensure_index(get_checkpoint_collection(), [("status", ASCENDING), ("updated_at", ASCENDING)],
                 "intake_review_idx")
    extractions = get_extraction_collection()
    created = ensure_index(extractions, [("expires_at", ASCENDING)],
                           "intake_extraction_expiry", expireAfterSeconds=0)
    # The general helper tolerates an equivalent index with another name. TTL
    # retention needs its options verified too: a plain expires_at index or a
    # conflicting TTL value is not an expiry guarantee.
    valid = any(list(index.get("key", {}).items()) == [("expires_at", ASCENDING)]
                and index.get("expireAfterSeconds") == 0
                for index in extractions.list_indexes()) if created else False
    if not valid:
        raise RuntimeError("Intake extraction retention requires expires_at TTL expireAfterSeconds=0")
