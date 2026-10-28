"""What recruiters decided about matched candidates.

The ranking itself is not stored: it is computed from the live records in
well under a second (`matcher`), so it is never stale and there is nothing to
re-run. What *is* kept is the recruiter's decision on a candidate for an
order — shortlisted, rejected, needs a call — because that is a person's
judgement, it must survive the order being edited and the candidate being
re-ranked, and it is what `evaluation` measures the ranking against.

    candidate_match_reviews   one decision per (order, candidate), with the
                              last twenty changes as history

Original candidate records are never written to from here.

Collections from the earlier Jev-based matcher (`candidate_matches`,
`candidate_match_runs`, `candidate_match_activity`) are no longer read or
written. They are left in place, untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pymongo import ASCENDING

REVIEWS = "candidate_match_reviews"

REVIEW_STATES = ("pending", "shortlisted", "rejected", "manual_review")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from app.db.mongo import get_db

    return get_db()


def ensure_indexes() -> None:
    """Called from `app.db.mongo.ensure_indexes`. Safe to run repeatedly."""
    from app.db.mongo import ensure_index

    ensure_index(_db()[REVIEWS], [("job_order_id", ASCENDING), ("candidate_id", ASCENDING)],
                 "match_review_pair_unique", unique=True)


def reviews_for_order(job_order_id: str) -> Dict[str, Dict[str, Any]]:
    return {doc["candidate_id"]: doc
            for doc in _db()[REVIEWS].find({"job_order_id": job_order_id}, {"_id": 0})}


def review_for(job_order_id: str, candidate_id: str) -> Optional[Dict[str, Any]]:
    return _db()[REVIEWS].find_one({"job_order_id": job_order_id, "candidate_id": candidate_id}, {"_id": 0})


def save_review(job_order_id: str, candidate_id: str, status: str, note: str, user: Dict[str, Any]) -> Dict[str, Any]:
    if status not in REVIEW_STATES:
        raise ValueError(f"Unknown review status {status!r}")
    now = _now()
    entry = {
        "status": status, "note": note, "at": now,
        "by_id": user.get("id"), "by_name": user.get("name") or user.get("email"),
    }
    _db()[REVIEWS].update_one(
        {"job_order_id": job_order_id, "candidate_id": candidate_id},
        {"$set": {**entry, "updated_at": now},
         "$setOnInsert": {"created_at": now},
         # The trail, because "who rejected this and when" is the question a
         # client asks after the fact.
         "$push": {"history": {"$each": [entry], "$slice": -20}}},
        upsert=True,
    )
    return review_for(job_order_id, candidate_id) or {}
