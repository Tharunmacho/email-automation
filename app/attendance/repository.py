"""Mongo persistence for append-only attendance records."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Sequence

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db.mongo import ensure_index, get_db

EVENTS = "attendance_events"
ADJUSTMENTS = "attendance_adjustments"
PERMISSIONS = "attendance_permissions"
SHIFTS = "attendance_shift_assignments"
CALENDAR = "attendance_calendar"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _public(doc: dict | None) -> dict | None:
    if not doc:
        return None
    value = dict(doc)
    value["id"] = str(value.pop("_id"))
    return value


class AttendanceRepository:
    def __init__(self, db=None):
        db = db if db is not None else get_db()
        self.events = db[EVENTS]
        self.adjustments = db[ADJUSTMENTS]
        self.permissions = db[PERMISSIONS]
        self.shifts = db[SHIFTS]
        self.calendar = db[CALENDAR]

    def append_event(self, event: dict) -> tuple[dict, bool]:
        """Insert once. A repeated webhook returns the original event."""
        doc = dict(event)
        doc.setdefault("_id", uuid.uuid4().hex)
        doc.setdefault("recorded_at", _utcnow())
        try:
            self.events.insert_one(doc)
            return _public(doc), True
        except DuplicateKeyError:
            existing = self.events.find_one({"idempotency_key": doc["idempotency_key"]})
            if existing:
                return _public(existing), False
            raise

    def event_by_idempotency_key(self, key: str) -> dict | None:
        return _public(self.events.find_one({"idempotency_key": key}))

    def events_for_day(self, employee_id: str, day: date) -> list[dict]:
        rows = self.events.find({"employee_id": employee_id, "local_date": day.isoformat()}).sort("occurred_at", ASCENDING)
        return [_public(row) for row in rows]

    def effective_punches_for_day(self, employee_id: str, day: date) -> list[dict]:
        punches = self.events_for_day(employee_id, day)
        additions = self.adjustments.find({
            "employee_id": employee_id,
            "attendance_date": day.isoformat(),
            "kind": "add_punch",
            "approved": True,
        }).sort("created_at", ASCENDING)
        punches.extend({"action": row["action"], "occurred_at": row["occurred_at"], "adjustment_id": str(row["_id"])} for row in additions)
        return sorted(punches, key=lambda row: row["occurred_at"])

    def append_adjustment(self, adjustment: dict) -> dict:
        doc = dict(adjustment)
        doc.setdefault("_id", uuid.uuid4().hex)
        doc.setdefault("created_at", _utcnow())
        doc["approved"] = True
        self.adjustments.insert_one(doc)
        return _public(doc)

    def adjustments_for_day(self, employee_id: str, day: date) -> list[dict]:
        rows = self.adjustments.find({"employee_id": employee_id, "attendance_date": day.isoformat(), "approved": True})
        return [_public(row) for row in rows]

    def create_permission(self, request: dict) -> dict:
        doc = dict(request)
        doc.setdefault("_id", uuid.uuid4().hex)
        doc.update(status="pending", created_at=_utcnow(), updated_at=_utcnow())
        self.permissions.insert_one(doc)
        return _public(doc)

    def permission(self, permission_id: str) -> dict | None:
        return _public(self.permissions.find_one({"_id": permission_id}))

    def decide_permission(self, permission_id: str, decision: dict) -> dict | None:
        updates = dict(decision)
        updates["status"] = "approved" if updates.pop("approved") else "rejected"
        # Keep the employee's request reason intact; the administrator's
        # explanation is a separate fact shown beside the decision.
        updates["decision_reason"] = updates.pop("reason")
        updates["updated_at"] = _utcnow()
        result = self.permissions.find_one_and_update(
            {"_id": permission_id, "status": "pending"},
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )
        return _public(result)

    def approved_permission(self, employee_id: str, day: date) -> dict | None:
        row = self.permissions.find_one(
            {"employee_id": employee_id, "attendance_date": day.isoformat(), "status": "approved"},
            sort=[("updated_at", DESCENDING)],
        )
        return _public(row)

    def approved_permissions(self, employee_id: str, day: date) -> list[dict]:
        rows = self.permissions.find(
            {"employee_id": employee_id, "attendance_date": day.isoformat(), "status": "approved"}
        ).sort("updated_at", ASCENDING)
        return [_public(row) for row in rows]

    def permissions_for_period(
        self,
        employee_ids: str | Sequence[str],
        start: date,
        end: date,
    ) -> list[dict]:
        """Permission requests for one employee or an admin-visible roster."""
        if isinstance(employee_ids, str):
            employee_query: object = employee_ids
        else:
            employee_query = {"$in": list(employee_ids)}
        rows = self.permissions.find({
            "employee_id": employee_query,
            "attendance_date": {
                "$gte": start.isoformat(),
                "$lte": end.isoformat(),
            },
        }).sort([("attendance_date", DESCENDING), ("created_at", DESCENDING)])
        return [_public(row) for row in rows]

    def assign_shift(self, assignment: dict) -> dict:
        doc = dict(assignment)
        doc.setdefault("_id", uuid.uuid4().hex)
        doc.setdefault("created_at", _utcnow())
        self.shifts.insert_one(doc)
        return _public(doc)

    def shift_for_day(self, employee_id: str, day: date) -> dict | None:
        row = self.shifts.find_one(
            {"employee_id": employee_id, "effective_from": {"$lte": day.isoformat()}},
            sort=[("effective_from", DESCENDING), ("created_at", DESCENDING)],
        )
        return _public(row)

    def set_calendar_day(self, value: dict) -> dict:
        doc = dict(value)
        doc.setdefault("_id", uuid.uuid4().hex)
        doc.setdefault("created_at", _utcnow())
        self.calendar.insert_one(doc)
        return _public(doc)

    def calendar_day(self, employee_id: str, day: date) -> dict | None:
        return _public(self.calendar.find_one(
            {"employee_id": employee_id, "attendance_date": day.isoformat()},
            sort=[("created_at", DESCENDING)],
        ))


def ensure_attendance_indexes() -> None:
    db = get_db()
    ensure_index(db[EVENTS], [("idempotency_key", ASCENDING)], "attendance_event_idempotency_unique", unique=True)
    ensure_index(db[EVENTS], [("employee_id", ASCENDING), ("local_date", ASCENDING), ("occurred_at", ASCENDING)], "attendance_employee_day")
    ensure_index(db[ADJUSTMENTS], [("employee_id", ASCENDING), ("attendance_date", ASCENDING)], "attendance_adjustment_day")
    ensure_index(db[PERMISSIONS], [("employee_id", ASCENDING), ("attendance_date", ASCENDING), ("status", ASCENDING)], "attendance_permission_day")
    ensure_index(db[SHIFTS], [("employee_id", ASCENDING), ("effective_from", DESCENDING)], "attendance_shift_effective")
    ensure_index(db[CALENDAR], [("employee_id", ASCENDING), ("attendance_date", ASCENDING), ("created_at", DESCENDING)], "attendance_calendar_day")
