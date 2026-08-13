"""Persistence for candidate records + duplicate lookups.

The pipeline talks only to this repository, never to PyMongo directly, so the
storage engine could change without touching business logic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

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
    # Allocation and verdict: the staff queue sorts and colours rows by these,
    # and the SLA countdown in the list is `assigned_at` against `viewed_at`.
    "assigned_staff_id": 1,
    "assigned_staff_name": 1,
    "assigned_at": 1,
    "viewed_at": 1,
    "evaluation_status": 1,
    "evaluation_score": 1,
    "evaluated_at": 1,
    # When the résumé arrived, so a row that has never been allocated can still
    # show how long it has been waiting.
    "ingested_at": 1,
    "processed_at": 1,
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

# What the balancer needs to level the collection: who holds each profile and
# whether anyone has touched it. Nothing else — a rebalance reads every document
# in the collection, and the OCR payload would make that a multi-megabyte scan.
REBALANCE_PROJECTION = {
    "_id": 1,
    "assigned_staff_id": 1,
    "assigned_staff_name": 1,
    "viewed_at": 1,
    "evaluation_status": 1,
    "created_at": 1,
}

# What an SLA alert is written from.
SLA_PROJECTION = {
    "_id": 1,
    "assigned_staff_id": 1,
    "assigned_staff_name": 1,
    "assigned_at": 1,
    "ingested_at": 1,
    "created_at": 1,
    "viewed_at": 1,
    "evaluation_status": 1,
    "profile.full_name": 1,
    "profile.email": 1,
}

# The clock every SLA calculation runs from, in Mongo's own expression language:
# when this profile started waiting. `assigned_at` is the honest answer once
# someone owns it — allocation is what creates the obligation — and the arrival
# time is the fallback, so a profile nobody has been given is not silently
# exempt from the window it has already been sitting in.
SLA_CLOCK_EXPR = {"$ifNull": ["$assigned_at", {"$ifNull": ["$ingested_at", "$created_at"]}]}


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


def _scoped(query: Optional[dict], staff_id: Optional[str]) -> dict:
    """Narrow a query to one staff member's own candidates.

    `staff_id` is None for an administrator, who sees the whole collection — so
    the admin path adds no filter rather than a filter that happens to match
    everything.

    Always returns a new dict. The projections and queries in this module are
    shared constants, and scoping one in place would leak one staff member's
    filter into every request that came after it.
    """
    scoped = dict(query or {})
    if staff_id:
        scoped["assigned_staff_id"] = staff_id
    return scoped


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
        self,
        limit: int = 50,
        skip: int = 0,
        minimal: bool = False,
        query: Optional[dict] = None,
        staff_id: Optional[str] = None,
    ) -> List[dict]:
        """A page of list rows, projected in the database.

        Returns plain dicts rather than `CandidateRecord`s, and deliberately so:
        a projected document is not a whole record, and validating it back into
        one would either fail on the missing required fields or force them to be
        made optional everywhere. The shape mirrors the record — `id` where
        Mongo has `_id` — so a list row reads the same as a detail response for
        the fields it does carry.

        `staff_id` narrows the page to that person's own candidates; None is an
        administrator and sees everything.
        """
        projection = MINIMAL_PROJECTION if minimal else LIST_PROJECTION
        cursor = (
            self._coll.find(_scoped(query, staff_id), projection)
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

    def count(self, query: Optional[dict] = None, staff_id: Optional[str] = None) -> int:
        return self._coll.count_documents(_scoped(query, staff_id))

    # ---- allocation -------------------------------------------------------- #
    def assign(self, candidate_id: str, staff_id: str, staff_name: Optional[str] = None) -> bool:
        """Hand one profile to a staff member, clearing any prior review state.

        The clear is deliberate and is what makes a profile "locked" to its
        current owner once someone has opened or judged it: moving it would
        throw that work away, so the balancer refuses to (see
        `app.assignment.balancer._is_locked`). Anything that calls this on a
        reviewed profile is asking for the verdict to be reset.
        """
        from app.core.models import utcnow

        now = utcnow()
        res = self._coll.update_one(
            {"_id": candidate_id},
            {"$set": {
                "assigned_staff_id": staff_id,
                "assigned_staff_name": staff_name,
                "assigned_at": now,
                "viewed_at": None,
                "evaluation_status": "pending",
                "evaluation_score": None,
                "evaluation_notes": None,
                "evaluated_at": None,
                "evaluated_by": None,
                "updated_at": now,
            }},
        )
        return res.matched_count > 0

    def reassign(self, candidate_id: str, staff_id: str, staff_name: Optional[str] = None) -> bool:
        """Change who owns a profile, keeping everything already done to it.

        The counterpart to `assign`, and the difference is the whole point:
        `assign` clears the verdict because it is placing fresh work, while this
        moves work that has already been started. It exists for re-homing
        profiles stranded on a deleted account — that profile has been read and
        judged, and handing it to someone else must not throw that away and
        restart the clock, or deleting an account would quietly destroy the
        evaluations that account had produced.

        `viewed_at` is deliberately left alone too, so the SLA record of when it
        was first opened survives the move.
        """
        from app.core.models import utcnow

        now = utcnow()
        res = self._coll.update_one(
            {"_id": candidate_id},
            {"$set": {
                "assigned_staff_id": staff_id,
                "assigned_staff_name": staff_name,
                "reassigned_at": now,
                "updated_at": now,
            }},
        )
        return res.matched_count > 0

    def list_owned_by(self, staff_id: str) -> List[dict]:
        """One staff member's profiles, oldest first, projected for allocation."""
        cursor = (
            self._coll.find({"assigned_staff_id": staff_id}, REBALANCE_PROJECTION)
            .sort("created_at", 1)
        )
        return list(cursor)

    def list_unassigned(self) -> List[dict]:
        """Profiles nobody owns, oldest first — the queue auto-allocation drains."""
        cursor = (
            self._coll.find({"assigned_staff_id": None}, REBALANCE_PROJECTION)
            .sort("created_at", 1)
        )
        return list(cursor)

    def list_orphaned(self, known_staff_ids: Sequence[str]) -> List[dict]:
        """Profiles pointing at an account that no longer exists, oldest first.

        The row form of `orphaned_count`, and it has to use the same predicate:
        the console reports the count and then re-homes what this returns, so a
        banner that says three and a re-home that finds two would never clear.
        """
        cursor = (
            self._coll.find(
                {"assigned_staff_id": {"$nin": [None, *known_staff_ids]}},
                REBALANCE_PROJECTION,
            )
            .sort("created_at", 1)
        )
        return list(cursor)

    def workload_counts(self) -> Dict[str, Dict[str, int]]:
        """`{staff_id: {assigned, evaluated, unviewed}}`, aggregated in Mongo.

        Only staff who hold something appear. A staff member with no candidates
        has no documents to group, so callers must read a missing key as zero
        rather than as unknown — a brand-new account is exactly the one every
        incoming résumé should be going to.
        """
        pipeline = [
            {"$match": {"assigned_staff_id": {"$ne": None}}},
            {"$group": {
                "_id": "$assigned_staff_id",
                "assigned": {"$sum": 1},
                # Anything that is not "pending" is a recorded verdict.
                "evaluated": {"$sum": {"$cond": [
                    {"$eq": [{"$ifNull": ["$evaluation_status", "pending"]}, "pending"]}, 0, 1,
                ]}},
                "unviewed": {"$sum": {"$cond": [{"$ifNull": ["$viewed_at", False]}, 0, 1]}},
            }},
        ]
        counts: Dict[str, Dict[str, int]] = {}
        for row in self._coll.aggregate(pipeline):
            counts[row["_id"]] = {
                "assigned": row.get("assigned", 0),
                "evaluated": row.get("evaluated", 0),
                "unviewed": row.get("unviewed", 0),
            }
        return counts

    def list_for_rebalance(self) -> List[dict]:
        """Every candidate, oldest first, projected down to the allocation fields.

        Oldest first because a rebalance deals the movable profiles out in order
        and the result has to be reproducible; an unordered scan would place the
        same collection differently on every run.
        """
        cursor = self._coll.find({}, REBALANCE_PROJECTION).sort("created_at", 1)
        return list(cursor)

    def unassigned_count(self) -> int:
        """Profiles nobody owns — invisible to every staff dashboard."""
        return self._coll.count_documents({"assigned_staff_id": None})

    def orphaned_count(self, known_staff_ids: Sequence[str]) -> int:
        """Profiles pointing at an account that no longer exists.

        A deactivated staff member still owns their work, so only ids that are
        gone from the roster entirely count here. Nothing but a rebalance clears
        these, which is why the console surfaces the number.
        """
        return self._coll.count_documents(
            {"assigned_staff_id": {"$nin": [None, *known_staff_ids]}}
        )

    def staff_workload(self, staff: Sequence[Any]) -> List[dict]:
        """One workload row per staff member, zero-filled, for the admin console."""
        counts = self.workload_counts()
        rows = []
        for member in staff:
            tally = counts.get(member.id, {})
            assigned = tally.get("assigned", 0)
            evaluated = tally.get("evaluated", 0)
            row = member.to_public()
            row.update({
                "assigned": assigned,
                "evaluated": evaluated,
                "unviewed": tally.get("unviewed", 0),
                "pending": assigned - evaluated,
                # Precomputed so the progress bar cannot divide by zero.
                "progress": round(evaluated / assigned * 100) if assigned else 0,
            })
            rows.append(row)
        return rows

    # ---- evaluation -------------------------------------------------------- #
    def mark_viewed(self, candidate_id: str, staff_id: Optional[str] = None) -> bool:
        """Stamp the first open. Returns True only the first time.

        `viewed_at: None` is part of the query rather than a read-then-write, so
        two tabs opening the same profile cannot both claim to be the first and
        the SLA stop-clock records when the owner actually looked at it.
        """
        from app.core.models import utcnow

        now = utcnow()
        res = self._coll.update_one(
            _scoped({"_id": candidate_id, "viewed_at": None}, staff_id),
            {"$set": {"viewed_at": now, "updated_at": now}},
        )
        return res.modified_count > 0

    def save_evaluation(
        self,
        candidate_id: str,
        staff_id: Optional[str],
        status: str,
        score: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> Optional[CandidateRecord]:
        """Record a verdict, and count it as a view if the profile was never opened.

        Returns None when the id does not exist *or* belongs to someone else —
        the caller cannot tell which, and that is the isolation rule: a staff
        member must not be able to probe for other people's candidates.
        """
        from app.core.models import utcnow

        now = utcnow()
        updates = {
            "evaluation_status": status,
            "evaluation_score": score,
            "evaluation_notes": notes,
            "evaluated_at": now,
            "evaluated_by": staff_id,
            "updated_at": now,
        }
        res = self._coll.update_one(_scoped({"_id": candidate_id}, staff_id), {"$set": updates})
        if res.matched_count == 0:
            return None

        # Judging a profile is looking at it; without this the SLA sweep would
        # keep reporting an evaluated profile as never opened.
        self._coll.update_one(
            {"_id": candidate_id, "viewed_at": None}, {"$set": {"viewed_at": now}}
        )
        return self.get(candidate_id)

    def find_sla_breaches(self, cutoff: datetime) -> List[dict]:
        """Assigned profiles, waiting since before `cutoff`, unopened or unjudged.

        "Waiting since" is `SLA_CLOCK_EXPR`, not `assigned_at` alone. A record
        allocated by an older build carries no `assigned_at`, and matching on
        that field directly excluded exactly the profiles that had been sitting
        longest — the sweep reported them as fine because it could not see when
        their clock had started.

        Still restricted to profiles somebody owns: an SLA is a promise a named
        person has not kept, and an alert against nobody names nobody to chase.
        Unallocated profiles are counted separately, on the admin console.
        """
        return list(self._coll.find(
            {
                "assigned_staff_id": {"$ne": None},
                "$expr": {"$lte": [SLA_CLOCK_EXPR, cutoff]},
                "$or": [
                    {"viewed_at": None},
                    {"evaluation_status": {"$in": [None, "pending"]}},
                ],
            },
            SLA_PROJECTION,
        ))
