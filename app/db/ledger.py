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

# Stands in for the resume hash on a "this email is dead" tombstone, so the row
# is keyed by message alone and never matches a real file. See retire_candidate.
DELETED_SENTINEL = "__deleted__"


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

    def is_message_suppressed(self, message_id: str) -> bool:
        """True when this exact email belonged to a candidate the user deleted.

        The durable half of the delete: the Gmail label hides the message from
        future searches, but Gmail's index lags by a minute or more, so this is
        what actually stops a re-ingest in the meantime.
        """
        return self._coll.count_documents(
            {"message_id": message_id, "suppressed": True}, limit=1
        ) > 0

    def message_ids_for_candidate(
        self, candidate_id: str, resume_hash: Optional[str] = None
    ) -> list[str]:
        """Every email that carried this candidate's resume.

        A resume often arrives more than once (forwarded, re-sent, cc'd), and the
        candidate record only remembers the message it was created from. Deleting
        the candidate has to un-label *all* of them, or the polls keep skipping
        the leftovers as already processed.

        Call this before clearing the ledger — `unsuppress_candidate` removes the
        rows this reads.
        """
        query: list[dict] = [{"candidate_id": candidate_id}]
        if resume_hash:
            query.append({"resume_hash": resume_hash})
        ids = self._coll.distinct("message_id", {"$or": query})
        return [m for m in ids if m]

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

    def retire_candidate(
        self,
        candidate_id: str,
        message_ids: list[str],
        resume_hash: Optional[str] = None,
    ) -> int:
        """Record that these *emails* are dead, while freeing the *file*.

        Deleting a candidate has to satisfy two opposite rules:

        * the emails it came from must never be ingested again — even though the
          Gmail label that hides them takes a while to reach Gmail's search
          index, so a poll seconds later still returns them;
        * the exact same resume arriving on a *new* email must ingest as a new
          candidate.

        Both hold if the hash-keyed rows go away and a message-keyed tombstone
        takes their place: `is_message_suppressed` blocks the old emails, while
        every hash lookup comes up empty for the file itself.

        Returns the number of tombstones written.
        """
        # Ordered so the tombstones survive: the delete would otherwise remove
        # the rows we are about to write.
        query: list[dict] = [{"candidate_id": candidate_id}]
        if resume_hash:
            query.append({"resume_hash": resume_hash})
            query.append({"_id": _key("__manual__", resume_hash)})
        for mid in message_ids:
            query.append({"message_id": mid})
        cleared = self._coll.delete_many({"$or": query}).deleted_count

        now = utcnow()
        for mid in message_ids:
            self._coll.update_one(
                {"_id": _key(mid, DELETED_SENTINEL)},
                {
                    "$set": {
                        "message_id": mid,
                        # Deliberately NOT the resume hash: this tombstone must
                        # not match the file when it arrives on a new email.
                        "resume_hash": DELETED_SENTINEL,
                        "candidate_id": candidate_id,
                        "status": "deleted",
                        "suppressed": True,
                        "reason": "candidate deleted by user",
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )

        log.info(
            "Ledger: cleared %d row(s) and tombstoned %d message(s) for deleted candidate %s",
            cleared, len(message_ids), candidate_id,
        )
        return len(message_ids)

    def unsuppress_candidate(
        self,
        candidate_id: str,
        resume_hash: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> int:
        """Clear ledger entries for a deleted candidate so resending their resume can re-ingest."""
        query = [{"candidate_id": candidate_id}]
        if resume_hash:
            query.append({"resume_hash": resume_hash})
            query.append({"_id": _key("__manual__", resume_hash)})
        if message_id:
            query.append({"message_id": message_id})
        res = self._coll.delete_many({"$or": query})
        log.info("Ledger: unsuppressed / cleared %d entr(ies) for candidate %s", res.deleted_count, candidate_id)
        return res.deleted_count

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
