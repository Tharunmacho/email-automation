"""Government holidays declared from Data Management."""
from __future__ import annotations

from datetime import date

import mongomock
import pytest

from app import holidays
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService


@pytest.fixture
def holiday_db(monkeypatch):
    db = mongomock.MongoClient()["holidays"]
    monkeypatch.setattr(holidays, "get_db", lambda: db)
    holidays._invalidate()
    yield db
    holidays._invalidate()


def _service() -> AttendanceService:
    db = mongomock.MongoClient()["attendance"]
    db["users"].insert_one({"_id": "emp", "email": "someone.adira@gmail.com"})
    return AttendanceService(AttendanceRepository(db))


def test_a_declared_date_is_a_holiday_with_nothing_owed(holiday_db):
    day = date(2026, 10, 21)
    assert _service().day("emp", day)["status"] != "H"

    holidays.add_holiday(day, "Election Day", "admin")
    result = _service().day("emp", day)
    assert result["status"] == "H"
    assert result["required_shift_minutes"] == 0
    assert result["unpaid_minutes"] == 0


def test_a_date_cannot_be_declared_twice_and_can_be_removed(holiday_db):
    day = date(2026, 10, 21)
    added = holidays.add_holiday(day, "Election Day", "admin")
    with pytest.raises(ValueError):
        holidays.add_holiday(day, "Again", "admin")
    assert holidays.remove_holiday(added["id"])
    assert holidays.government_holiday(day) is None
