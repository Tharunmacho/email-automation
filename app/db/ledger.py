"""Durable record of what has already been ingested.

Why this exists
---------------
Every dedup check used to run against the *candidates* collection:

    find_by_message_id / find_by_resume_hash / find_by_email_or_phone

That works right up until someone deletes a candidate. Deleting the candidate
also deletes the only evidence that the message and attachment were ever seen,
so the next Gmail poll re-fetches the same mail, finds no duplicate, and
re-ingests the profile the user just removed.

The ledger keeps that evidence separately. It is append-only from the
pipeline's point of view and is never cleaned up when a candidate is deleted —
instead the entry is marked ``suppressed``, which means "seen before, and the
user does not want it back".

One document per (message_id, resume_hash) pair, so an email carrying two
different resumes is tracked as two entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pymongo import ASCENDING

from app.core.models import utcnow
from app.db.mongo import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

LEDGER_COLLECTION = "ingest_ledger"


def get_ledger_collection():
    return get_db()[LEDGER_COLLECTION]


@dataclass
class LedgerEntry:
    message_id: str
    resume_hash: str
    candidate_id: Optional[str]
    suppressed: bool
    reason: str = ""


def _key(message_id: str, resume_hash: str) -> str:
    return f"{message_id}:{resume_hash}"


class IngestLedger:
    """Append-only 'have we seen this before' index."""

    def __init__(self, collection=None):
        self._coll = collection if collection is not None else get_ledger_collection()

    # ---- reads ------------------------------------------------------------ #
    def find(self, message_id: str, resume_hash: str) -> Optional[LedgerEntry]:
        doc = self._coll.find_one({"_id": _key(message_id, resume_hash)})
        return self._to_entry(doc) if doc else None

    def find_by_hash(self, resume_hash: str) -> Optional[LedgerEntry]:
        """Same file arriving from a *different* email still counts as seen."""
        doc = self._coll.find_one({"resume_hash": resume_hash})
        return self._to_entry(doc) if doc else None

    def is_suppressed(self, resume_hash: str) -> bool:
        """True when the user deleted a candidate that came from this file."""
        return self._coll.count_documents(
            {"resume_hash": resume_hash, "suppressed": True}, limit=1
        ) > 0

    def message_seen(self, message_id: str) -> bool:
        return self._coll.count_documents({"message_id": message_id}, limit=1) > 0

    @staticmethod
    def _to_entry(doc) -> LedgerEntry:
        return LedgerEntry(
            message_id=doc.get("message_id", ""),
            resume_hash=doc.get("resume_hash", ""),
            candidate_id=doc.get("candidate_id"),
            suppressed=bool(doc.get("suppressed", False)),
            reason=doc.get("reason", ""),
        )

    # ---- writes ----------------------------------------------------------- #
    def record(
        self,
        message_id: str,
        resume_hash: str,
        candidate_id: Optional[str],
        status: str,
        detail: str = "",
    ) -> None:
        """Note that this attachment was handled. Never overwrites a suppression."""
        now = utcnow()
        self._coll.update_one(
            {"_id": _key(message_id, resume_hash)},
            {
                "$set": {
                    "message_id": message_id,
                    "resume_hash": resume_hash,
                    "candidate_id": candidate_id,
                    "status": status,
                    "detail": detail,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now, "suppressed": False},
            },
            upsert=True,
        )

    def suppress_candidate(self, candidate_id: str, reason: str = "deleted by user") -> int:
        """Tombstone every ledger entry for a candidate the user removed.

        Returns the number of entries marked, so callers can log whether the
        deletion will actually stick.
        """
        res = self._coll.update_many(
            {"candidate_id": candidate_id},
            {"$set": {"suppressed": True, "reason": reason, "updated_at": utcnow()}},
        )
        log.info(
            "Ledger: suppressed %d entr(ies) for candidate %s (%s)",
            res.modified_count, candidate_id, reason,
        )
        return res.modified_count

    def suppress_hash(self, resume_hash: str, reason: str = "deleted by user") -> None:
        """Fallback for records that predate the ledger and have no entry yet."""
        self._coll.update_one(
            {"_id": _key("__manual__", resume_hash)},
            {
                "$set": {
                    "message_id": "__manual__",
                    "resume_hash": resume_hash,
                    "candidate_id": None,
                    "suppressed": True,
                    "reason": reason,
                    "updated_at": utcnow(),
                },
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )


def ensure_ledger_indexes() -> None:
    coll = get_ledger_collection()
    coll.create_index([("resume_hash", ASCENDING)], name="ledger_hash_idx")
    coll.create_index([("message_id", ASCENDING)], name="ledger_msg_idx")
    coll.create_index([("candidate_id", ASCENDING)], name="ledger_candidate_idx", sparse=True)
