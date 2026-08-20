"""The ingestion state machine: one row per attachment *per OCR mode*.

Why this is not `ingest_ledger`
-------------------------------
`app.db.ledger` answers one question — "have we seen this email / this file
before?" — and it answers it with one row per ``(message_id, resume_hash)``.
That was enough while an attachment meant exactly one thing: a résumé.

A multipass bundle is three pieces of work. The same 60-page PDF holds a CV on
pages 52-53, an Aadhaar card on page 54 and a passport on page 55, and each goes
to a different OCR endpoint, succeeds or fails on its own, and is retried on its
own. Collapsing that onto one row means a passport that failed cannot be retried
without re-running the résumé extraction that already succeeded — and worse,
the retry would look like a duplicate and be dropped.

So the unit here is ``(provider, account_id, message_id, attachment_id,
ocr_mode)``, which is exactly the unique constraint. The ``_id`` is derived from
those five fields, so the uniqueness holds even on a cluster where the index has
not been built yet: a second insert of the same tuple is a duplicate ``_id``,
not a second row.

The states
----------
::

    received ──▶ submitting ──▶ running ──▶ succeeded
        ▲            │             │
        └── failed ◀─┴─────────────┘
                     │
                     └──▶ abandoned   (attempts exhausted → operator review)

``failed`` is retryable and is what the reconciler picks up. ``abandoned`` is
terminal and deliberately visible: it means a human has to look, and nothing in
the system will quietly try again.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING

from app.core.models import utcnow
from app.db.mongo import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

INGESTION_STATE_COLLECTION = "ingestion_state"

# ---- providers ----
PROVIDER_EMAIL = "email"

# ---- OCR modes. Mirrors the Veris job API's `mode` enum. ----
MODE_RESUME = "resume"
MODE_AADHAAR = "aadhaar"
MODE_PASSPORT = "passport"
MODE_DOCUMENT = "document"
VALID_MODES = (MODE_RESUME, MODE_AADHAAR, MODE_PASSPORT, MODE_DOCUMENT)

# ---- states ----
RECEIVED = "received"
SUBMITTING = "submitting"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
ABANDONED = "abandoned"

#: States from which the reconciler may legitimately re-drive a row.
RESUMABLE = (RECEIVED, SUBMITTING, RUNNING, FAILED)
#: States nothing will touch again on its own.
TERMINAL = (SUCCEEDED, ABANDONED)


def get_ingestion_state_collection():
    return get_db()[INGESTION_STATE_COLLECTION]


def row_id(
    provider: str,
    account_id: str,
    message_id: str,
    attachment_id: str,
    ocr_mode: str,
) -> str:
    """The deterministic ``_id`` for one unit of work.

    Hashed rather than concatenated because a Gmail attachment handle is a
    900-character base64 blob and Mongo caps ``_id`` well below that.
    """
    raw = "\x1f".join((provider, account_id, message_id, attachment_id, ocr_mode))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def idempotency_key(
    provider: str,
    account_id: str,
    message_id: str,
    attachment_id: str,
    ocr_mode: str,
) -> str:
    """The key sent to the OCR service as ``Idempotency-Key``.

    Same five fields as the row, in the human-readable form the OCR service's
    own logs show, so a job can be traced back to the mail it came from without
    a database lookup. The attachment handle is truncated — it is unique within
    a message long before its 900th character, and the header has to stay a
    sane length.
    """
    handle = (attachment_id or "")[:64]
    return f"{provider}/{account_id}/{message_id}/{handle}/{ocr_mode}"


@dataclass
class IngestionRow:
    """One attachment, in one OCR mode, at one point in its life."""

    id: str
    provider: str
    account_id: str
    message_id: str
    attachment_id: str
    ocr_mode: str
    status: str = RECEIVED
    storage_key: str = ""
    sha256: str = ""
    filename: str = ""
    pages: List[int] = field(default_factory=list)
    ocr_job_id: Optional[str] = None
    idempotency_key: str = ""
    attempts: int = 0
    last_error: str = ""
    candidate_id: Optional[str] = None
    result_id: Optional[str] = None
    received_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_doc(cls, doc: Dict[str, Any]) -> "IngestionRow":
        return cls(
            id=doc.get("_id", ""),
            provider=doc.get("provider", PROVIDER_EMAIL),
            account_id=doc.get("account_id", ""),
            message_id=doc.get("message_id", ""),
            attachment_id=doc.get("attachment_id", ""),
            ocr_mode=doc.get("ocr_mode", ""),
            status=doc.get("status", RECEIVED),
            storage_key=doc.get("storage_key", "") or "",
            sha256=doc.get("sha256", "") or "",
            filename=doc.get("filename", "") or "",
            pages=list(doc.get("pages") or []),
            ocr_job_id=doc.get("ocr_job_id"),
            idempotency_key=doc.get("idempotency_key", "") or "",
            attempts=int(doc.get("attempts", 0) or 0),
            last_error=doc.get("last_error", "") or "",
            candidate_id=doc.get("candidate_id"),
            result_id=doc.get("result_id"),
            received_at=doc.get("received_at"),
            submitted_at=doc.get("submitted_at"),
            completed_at=doc.get("completed_at"),
            updated_at=doc.get("updated_at"),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


class IngestionStateStore:
    """Reads and writes over the ingestion state collection.

    Every write is a targeted `update_one` rather than a read-modify-write, so
    two workers racing on the same row cannot lose each other's transition. The
    one transition that must not happen twice — claiming a row for submission —
    is a conditional update whose matched count is the answer.
    """

    def __init__(self, collection=None):
        self._coll = collection if collection is not None else get_ingestion_state_collection()

    # ---- reads ------------------------------------------------------------ #
    def get(self, id_: str) -> Optional[IngestionRow]:
        doc = self._coll.find_one({"_id": id_})
        return IngestionRow.from_doc(doc) if doc else None

    def find(
        self,
        provider: str,
        account_id: str,
        message_id: str,
        attachment_id: str,
        ocr_mode: str,
    ) -> Optional[IngestionRow]:
        return self.get(row_id(provider, account_id, message_id, attachment_id, ocr_mode))

    def rows_for_message(self, message_id: str) -> List[IngestionRow]:
        return [IngestionRow.from_doc(d) for d in self._coll.find({"message_id": message_id})]

    def rows_for_candidate(self, candidate_id: str) -> List[IngestionRow]:
        return [IngestionRow.from_doc(d) for d in self._coll.find({"candidate_id": candidate_id})]

    def find_stuck(
        self,
        stuck_after_seconds: int,
        limit: int = 50,
        now: Optional[datetime] = None,
    ) -> List[IngestionRow]:
        """Rows that should have finished by now and have not.

        "Should have finished" is measured from `updated_at`, not from
        `received_at`: a job that is genuinely still running keeps its row
        touched by the poller, and re-submitting a healthy long job would double
        the OCR bill for nothing.
        """
        cutoff = (now or utcnow()) - timedelta(seconds=max(1, int(stuck_after_seconds)))
        cursor = (
            self._coll.find(
                {
                    "status": {"$in": list(RESUMABLE)},
                    "updated_at": {"$lt": cutoff},
                }
            )
            .sort("updated_at", ASCENDING)
            .limit(max(1, int(limit)))
        )
        return [IngestionRow.from_doc(d) for d in cursor]

    def review_queue(self, limit: int = 100) -> List[IngestionRow]:
        """What a human has to deal with: rows that exhausted their retries."""
        cursor = (
            self._coll.find({"status": ABANDONED})
            .sort("completed_at", ASCENDING)
            .limit(max(1, int(limit)))
        )
        return [IngestionRow.from_doc(d) for d in cursor]

    def status_counts(self) -> Dict[str, int]:
        """`{status: count}` for the ops endpoint. Missing states read as zero."""
        counts = {s: 0 for s in (RECEIVED, SUBMITTING, RUNNING, SUCCEEDED, FAILED, ABANDONED)}
        for group in self._coll.aggregate(
            [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
        ):
            counts[group["_id"]] = group["n"]
        return counts

    # ---- writes ----------------------------------------------------------- #
    def open_row(
        self,
        provider: str,
        account_id: str,
        message_id: str,
        attachment_id: str,
        ocr_mode: str,
        *,
        sha256: str = "",
        storage_key: str = "",
        filename: str = "",
        pages: Optional[List[int]] = None,
        candidate_id: Optional[str] = None,
    ) -> IngestionRow:
        """Register this unit of work, or return the row that already exists.

        Idempotent by construction. Re-processing the same mail re-opens the
        same row and, crucially, does *not* reset a row that already succeeded —
        the `$setOnInsert` is what keeps a completed passport extraction from
        being thrown away by a redelivery of the email that carried it.
        """
        if ocr_mode not in VALID_MODES:
            raise ValueError(f"Unknown OCR mode {ocr_mode!r}; expected one of {VALID_MODES}")

        id_ = row_id(provider, account_id, message_id, attachment_id, ocr_mode)
        now = utcnow()
        # Facts about the attachment are refreshed; the state machine's own
        # fields are only ever written on insert.
        self._coll.update_one(
            {"_id": id_},
            {
                "$set": {
                    "provider": provider,
                    "account_id": account_id,
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "ocr_mode": ocr_mode,
                    "sha256": sha256,
                    "storage_key": storage_key,
                    "filename": filename,
                    "pages": list(pages or []),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "status": RECEIVED,
                    "attempts": 0,
                    "last_error": "",
                    "ocr_job_id": None,
                    "candidate_id": candidate_id,
                    "result_id": None,
                    "idempotency_key": idempotency_key(
                        provider, account_id, message_id, attachment_id, ocr_mode
                    ),
                    "received_at": now,
                    "submitted_at": None,
                    "completed_at": None,
                },
            },
            upsert=True,
        )
        row = self.get(id_)
        assert row is not None  # just upserted
        return row

    def claim_for_submit(self, id_: str, max_attempts: int) -> bool:
        """Take exclusive ownership of a row in order to submit it.

        Returns False when another worker got there first, when the row is
        already finished, or when it has spent its attempts — the caller's
        correct response to all three is "leave it alone".
        """
        now = utcnow()
        res = self._coll.update_one(
            {
                "_id": id_,
                "status": {"$in": [RECEIVED, FAILED]},
                "attempts": {"$lt": max(1, int(max_attempts))},
            },
            {
                "$set": {"status": SUBMITTING, "updated_at": now},
                "$inc": {"attempts": 1},
            },
        )
        return res.matched_count == 1

    def mark_submitted(self, id_: str, job_id: str, status: str = RUNNING) -> None:
        now = utcnow()
        self._coll.update_one(
            {"_id": id_},
            {
                "$set": {
                    "ocr_job_id": job_id,
                    "status": status,
                    "submitted_at": now,
                    "updated_at": now,
                    "last_error": "",
                }
            },
        )

    def touch(self, id_: str) -> None:
        """Note that this job is still alive, so the sweep leaves it alone."""
        self._coll.update_one({"_id": id_}, {"$set": {"updated_at": utcnow()}})

    def mark_succeeded(
        self,
        id_: str,
        result_id: Optional[str] = None,
        candidate_id: Optional[str] = None,
    ) -> None:
        now = utcnow()
        update: Dict[str, Any] = {
            "status": SUCCEEDED,
            "completed_at": now,
            "updated_at": now,
            "last_error": "",
        }
        if result_id is not None:
            update["result_id"] = result_id
        if candidate_id is not None:
            update["candidate_id"] = candidate_id
        self._coll.update_one({"_id": id_}, {"$set": update})

    def mark_failed(
        self,
        id_: str,
        error: str,
        max_attempts: int,
        *,
        clear_job: bool = False,
    ) -> str:
        """Record a failure, and decide whether anything will try again.

        Returns the state the row landed in, so the caller can log the
        difference between "will retry" and "a human now owns this".

        ``clear_job`` forgets the job id as well, for the one case where the id
        itself is the problem: the service no longer retains that job, so the
        only way forward is a fresh submission — and a row that still carries a
        dead id would be polled forever instead of re-submitted.
        """
        now = utcnow()
        row = self.get(id_)
        exhausted = bool(row and row.attempts >= max(1, int(max_attempts)))
        status = ABANDONED if exhausted else FAILED
        update: Dict[str, Any] = {
            "status": status,
            "last_error": (error or "")[:2000],
            "updated_at": now,
        }
        if clear_job:
            update["ocr_job_id"] = None
        if exhausted:
            update["completed_at"] = now
        self._coll.update_one({"_id": id_}, {"$set": update})
        if exhausted:
            log.warning(
                "Ingestion row %s (%s) abandoned after %s attempt(s): %s",
                id_, row.ocr_mode if row else "?", row.attempts if row else "?", error,
            )
        return status

    def set_candidate(self, id_: str, candidate_id: str) -> None:
        """Link this row to the candidate the résumé pass produced.

        The ID passes run after the candidate exists, so their rows are opened
        before there is anything to link them to.
        """
        self._coll.update_one(
            {"_id": id_},
            {"$set": {"candidate_id": candidate_id, "updated_at": utcnow()}},
        )

    def reset_for_retry(self, id_: str) -> bool:
        """Put an abandoned row back in the queue, at an operator's request."""
        res = self._coll.update_one(
            {"_id": id_, "status": ABANDONED},
            {
                "$set": {
                    "status": RECEIVED,
                    "attempts": 0,
                    "completed_at": None,
                    "updated_at": utcnow(),
                }
            },
        )
        return res.matched_count == 1


def ensure_ingestion_state_indexes() -> None:
    from app.db.mongo import ensure_index

    coll = get_ingestion_state_collection()
    # The constraint the whole design rests on. `_id` already enforces it, so
    # this is belt *and* braces — and it is what makes the tuple queryable.
    ensure_index(
        coll,
        [
            ("provider", ASCENDING),
            ("account_id", ASCENDING),
            ("message_id", ASCENDING),
            ("attachment_id", ASCENDING),
            ("ocr_mode", ASCENDING),
        ],
        "ingestion_state_unique",
        unique=True,
    )
    # The reconciler's sweep: unfinished rows, oldest first.
    ensure_index(coll, [("status", ASCENDING), ("updated_at", ASCENDING)], "ingestion_state_sweep_idx")
    ensure_index(coll, [("message_id", ASCENDING)], "ingestion_state_msg_idx")
    ensure_index(coll, [("candidate_id", ASCENDING)], "ingestion_state_candidate_idx", sparse=True)
    ensure_index(coll, [("ocr_job_id", ASCENDING)], "ingestion_state_job_idx", sparse=True)
