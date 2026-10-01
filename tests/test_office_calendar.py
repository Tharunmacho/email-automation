"""Festival holidays and standard shift hours applied office-wide."""
from __future__ import annotations

from datetime import date, time

import mongomock

from app.attendance.models import CalendarDayRequest, DutyPlanRequest, Shift, ShiftAssignmentRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService


def _service(email: str = "someone.adira@gmail.com") -> AttendanceService:
    db = mongomock.MongoClient()["office-calendar"]
    db["users"].insert_one({"_id": "emp", "email": email})
    return AttendanceService(AttendanceRepository(db))


def test_festival_holidays_from_october_are_holidays_for_everyone():
    service = _service()
    for day in (date(2026, 10, 2), date(2026, 10, 19), date(2026, 12, 25)):
        result = service.day("emp", day)
        assert result["status"] == "H"
        assert result["required_shift_minutes"] == 0


def test_holidays_before_october_are_left_as_recorded():
    assert _service().day("emp", date(2026, 9, 14))["status"] != "H"


def test_an_explicit_calendar_entry_or_duty_plan_overrides_the_holiday():
    service = _service()
    service.set_calendar_day(CalendarDayRequest(
        employee_id="emp", attendance_date=date(2026, 10, 2), status="PL", reason="leave"), "admin")
    assert service.day("emp", date(2026, 10, 2))["status"] == "PL"
    service.plan_duty(DutyPlanRequest(employee_id="emp", attendance_date=date(2026, 10, 19), reason="rostered"), "admin")
    assert service.day("emp", date(2026, 10, 19))["status"] != "H"


def test_royapettah_staff_work_nine_thirty_to_six_thirty():
    for email in ("sreya.adira@gmail.com", "bakkimamal.adira@gmail.com", "DSharmila.adira@gmail.com"):
        result = _service(email).day("emp", date(2026, 10, 5))
        assert _ist(result["shift_start"]) == time(9, 30)
        assert _ist(result["shift_end"]) == time(18, 30)
        assert result["required_shift_minutes"] == 480


def test_other_staff_keep_the_default_shift_and_assignments_still_win():
    assert _ist(_service().day("emp", date(2026, 10, 5))["shift_start"]) == time(10, 0)
    service = _service("sreya.adira@gmail.com")
    service.assign_shift(ShiftAssignmentRequest(
        employee_id="emp", effective_from=date(2026, 10, 1),
        shift=Shift(start=time(11, 0), end=time(20, 0)), reason="late shift"), "admin")
    assert _ist(service.day("emp", date(2026, 10, 5))["shift_start"]) == time(11, 0)


def _ist(value) -> time:
    from zoneinfo import ZoneInfo
    return value.astimezone(ZoneInfo("Asia/Kolkata")).time().replace(tzinfo=None)
