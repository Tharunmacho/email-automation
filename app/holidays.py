"""Government holidays declared from Data Management.

A date stored here is a holiday for every employee in every branch: attendance
marks it "H" (no duty, never absent) and payroll treats it exactly like the
built-in festival holidays — no deduction for the day.

The attendance service asks about one employee-day at a time, and a payroll run
asks for every employee across a whole month, so the list is cached briefly in
process rather than read from Mongo on every call. Adding or removing a holiday
clears the cache in this process straight away.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import date

from app.core.models import utcnow
from app.db.mongo import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

HOLIDAYS_COLLECTION = "government_holidays"
_CACHE_SECONDS = 60

_lock = threading.Lock()
_cache: dict[str, str] | None = None
_cache_at = 0.0


def _collection(db=None):
    return (db if db is not None else get_db())[HOLIDAYS_COLLECTION]


def _invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def _holiday_map() -> dict[str, str]:
    """ISO date -> holiday name, cached for a minute."""
    global _cache, _cache_at
    with _lock:
        if _cache is not None and time.monotonic() - _cache_at < _CACHE_SECONDS:
            return _cache
    try:
        rows = _collection().find({}, {"date": 1, "name": 1})
        fresh = {row["date"]: row.get("name") or "Government holiday" for row in rows if row.get("date")}
    except Exception as exc:  # noqa: BLE001 — attendance must still compute without the list
        log.warning("Government holiday list unavailable: %s", exc)
        return _cache or {}
    with _lock:
        _cache, _cache_at = fresh, time.monotonic()
    return fresh


def government_holiday(day: date) -> str | None:
    """The government holiday declared for this date, if any."""
    return _holiday_map().get(day.isoformat())


def list_holidays(db=None) -> list[dict]:
    rows = _collection(db).find({}).sort("date", 1)
    return [
        {"id": row["_id"], "date": row["date"], "name": row.get("name") or "Government holiday"}
        for row in rows
    ]


def add_holiday(day: date, name: str, actor_id: str, db=None) -> dict:
    """Declare a holiday. Raises ValueError for a blank name or a date already declared."""
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Holiday name is required.")
    iso = day.isoformat()
    from app.attendance.office_calendar import festival_holiday

    builtin = festival_holiday(day)
    if builtin:
        raise ValueError(f"{iso} is already a holiday ({builtin}) on the 2026 list.")
    if _collection(db).find_one({"date": iso}):
        raise ValueError(f"{iso} is already declared as a holiday.")
    row = {"_id": uuid.uuid4().hex, "date": iso, "name": name, "created_by": actor_id, "created_at": utcnow()}
    _collection(db).insert_one(row)
    _invalidate()
    return {"id": row["_id"], "date": iso, "name": name}


def remove_holiday(holiday_id: str, db=None) -> bool:
    removed = _collection(db).delete_one({"_id": holiday_id}).deleted_count > 0
    _invalidate()
    return removed
