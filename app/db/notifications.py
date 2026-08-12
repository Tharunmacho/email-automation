"""Durable notifications, so a pop-up missed is not a pop-up lost.

The WebSocket push is the *fast* path, not the record. It only ever reaches a
socket that happens to be open at the instant the event fires, which is the
minority case in practice: résumés arrive on a Gmail poll all day, and the staff
member they are allocated to is at lunch, on another screen, or has not logged
in yet. Pushing to nobody and keeping no record meant the allocation simply
never announced itself — the profile appeared in the queue as though it had
always been there, and "I never saw a notification" was correct.

So every notification is written here first and pushed second. The bell reads
this collection, which makes the feed survive a logout, a restart, and Redis
being down, and makes the unread count mean something.

One document per user per event: a fan-out to three admins is three rows. That
is what lets one admin read it without clearing it for the others.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from pymongo import ASCENDING, DESCENDING

from app.core.models import utcnow
from app.db.mongo import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

NOTIFICATIONS_COLLECTION = "notifications"

# Event kinds. Mirrored in the frontend's notification types.
CANDIDATE_ASSIGNED = "candidate_assigned"
CANDIDATE_INGESTED = "candidate_ingested"
SLA_ALERT = "sla_alert"

# A feed nobody has read in a month is noise, and this collection is written on
# every single ingest. Expiring is a TTL index rather than a cleanup job so it
# cannot be forgotten, and it is generous enough that a fortnight's leave does
# not erase the queue history someone came back for.
RETENTION_DAYS = 30


def get_notifications_collection():
    return get_db()[NOTIFICATIONS_COLLECTION]


def ensure_notification_indexes() -> None:
    """Called from `app.db.mongo.ensure_indexes` on startup.

    The bell polls this collection every minute per open tab, and nothing ever
    deletes from it — so an unindexed feed query and a missing TTL are both the
    kind of problem that only shows up once the collection is large.
    """
    from app.db.mongo import ensure_index

    coll = get_notifications_collection()
    # The bell's only query: this user's feed, newest first.
    ensure_index(coll, [("user_id", ASCENDING), ("created_at", DESCENDING)], "user_feed_idx")
    # The badge count, which is read far more often than the feed itself.
    ensure_index(coll, [("user_id", ASCENDING), ("read_at", ASCENDING)], "user_unread_idx")
    # Expiry, so the feed cannot grow without bound.
    ensure_index(
        coll,
        [("created_at", ASCENDING)],
        "notification_ttl_idx",
        expireAfterSeconds=int(timedelta(days=RETENTION_DAYS).total_seconds()),
    )


@dataclass
class Notification:
    id: str
    user_id: str
    type: str
    title: str
    message: str
    candidate_id: Optional[str] = None
    candidate_name: Optional[str] = None
    created_at: object = None
    read_at: object = None

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "message": self.message,
            "candidate_id": self.candidate_id,
            "candidate_name": self.candidate_name,
            "created_at": self.created_at,
            "read": self.read_at is not None,
        }


class NotificationRepository:
    def __init__(self, collection=None):
        self._coll = collection if collection is not None else get_notifications_collection()

    # ---- writing ---------------------------------------------------------- #
    def record(
        self,
        user_id: str,
        *,
        type: str,
        title: str,
        message: str,
        candidate_id: str | None = None,
        candidate_name: str | None = None,
    ) -> Notification:
        note = Notification(
            id=uuid.uuid4().hex,
            user_id=user_id,
            type=type,
            title=title,
            message=message,
            candidate_id=candidate_id,
            candidate_name=candidate_name,
            created_at=utcnow(),
            read_at=None,
        )
        self._coll.insert_one(
            {
                "_id": note.id,
                "user_id": note.user_id,
                "type": note.type,
                "title": note.title,
                "message": note.message,
                "candidate_id": note.candidate_id,
                "candidate_name": note.candidate_name,
                "created_at": note.created_at,
                "read_at": None,
            }
        )
        return note

    # ---- reading ---------------------------------------------------------- #
    def list_for(self, user_id: str, *, limit: int = 30, unread_only: bool = False) -> List[dict]:
        query: dict = {"user_id": user_id}
        if unread_only:
            query["read_at"] = None
        cursor = (
            self._coll.find(query)
            .sort("created_at", DESCENDING)
            .limit(max(1, min(limit, 100)))
        )
        return [self._to_public(doc) for doc in cursor]

    def unread_count(self, user_id: str) -> int:
        return self._coll.count_documents({"user_id": user_id, "read_at": None})

    # ---- marking ---------------------------------------------------------- #
    def mark_read(self, user_id: str, ids: List[str]) -> int:
        """Scoped by user id as well as notification id, always.

        The id alone would be enough to find the row, and that is exactly why it
        is not enough to update it: a notification id names another person's
        feed just as well as your own.
        """
        if not ids:
            return 0
        result = self._coll.update_many(
            {"_id": {"$in": list(ids)}, "user_id": user_id, "read_at": None},
            {"$set": {"read_at": utcnow()}},
        )
        return result.modified_count

    def mark_all_read(self, user_id: str) -> int:
        result = self._coll.update_many(
            {"user_id": user_id, "read_at": None},
            {"$set": {"read_at": utcnow()}},
        )
        return result.modified_count

    @staticmethod
    def _to_public(doc: dict) -> dict:
        return {
            "id": doc["_id"],
            "type": doc.get("type", ""),
            "title": doc.get("title", ""),
            "message": doc.get("message", ""),
            "candidate_id": doc.get("candidate_id"),
            "candidate_name": doc.get("candidate_name"),
            "created_at": doc.get("created_at"),
            "read": doc.get("read_at") is not None,
        }
