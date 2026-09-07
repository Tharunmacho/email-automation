from datetime import date, datetime, time, timezone

import pytest

from app.attendance.engine import AttendancePolicy, calculate_day, calculate_month, lop_amount
from app.attendance.models import AttendanceStatus, Shift
from app.attendance.models import PermissionRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService

import mongomock


def utc(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def test_default_shift_is_calculated_in_kolkata_without_grace():
    # 10:20 IST and 18:35 IST: 20 late + 25 early.
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": utc(4, 50)},
            {"action": "check_out", "occurred_at": utc(13, 5)},
        ],
        now=utc(15),
    )
    assert result["late_minutes"] == 20
    assert result["early_minutes"] == 25
    assert result["unapproved_minutes"] == 45
    assert result["status"] == "LT"


def test_late_and_early_approvals_share_minutes_and_count_as_two_occasions():
    day = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": utc(4, 50)},
            {"action": "check_out", "occurred_at": utc(13, 5)},
        ],
        approved_permissions=[
            {"kind": "late", "requested_minutes": 20},
            {"kind": "early_exit", "requested_minutes": 25},
        ],
        now=utc(15),
    )
    third = {"date": date(2026, 9, 3), "approved_actual_minutes": 30, "approved_occurrence_minutes": [30], "unapproved_minutes": 0}
    rows = calculate_month([day, third])
    assert rows[0]["paid_permission_minutes"] == 45
    assert rows[0]["permission_minutes_remaining"] == 15
    assert rows[0]["permission_occasions_used"] == 2
    assert rows[1]["paid_permission_minutes"] == 0
    assert rows[1]["unpaid_minutes"] == 30


def test_missing_punch_is_provisional_then_becomes_actual_absence():
    punches = [{"action": "check_in", "occurred_at": utc(4, 30)}]
    provisional = calculate_day(date(2026, 9, 2), punches, now=utc(14))
    expired = calculate_day(date(2026, 9, 2), punches, now=utc(14, day=5))
    assert provisional["status"] == "MP"
    assert provisional["unpaid_minutes"] == 0
    assert provisional["provisional"] is True
    assert expired["status"] == "A"
    assert expired["unpaid_minutes"] == 480


def test_five_minute_work_session_uses_grace_then_deducts_uncovered_shift():
    day = calculate_day(
        date(2026, 9, 8),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 8, 4, 30, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 8, 4, 35, tzinfo=timezone.utc)},
        ],
        now=datetime(2026, 9, 8, 4, 36, tzinfo=timezone.utc),
    )
    calculated = calculate_month([day])[0]

    assert calculated["status"] == "EE"
    assert calculated["early_minutes"] == 535
    assert calculated["grace_minutes_applied"] == 60
    assert calculated["unpaid_minutes"] == 415


def test_alternate_friday_replaces_sunday_as_the_weekly_off():
    repository = AttendanceRepository(mongomock.MongoClient()["weekly-off-choice"])
    repository.set_employee_policy("staff-1", {
        "monthly_salary": 30000,
        "weekly_off_pattern": "alternate_friday",
        "alternate_friday_parity": 0,
    })
    attendance = AttendanceService(repository)

    assert attendance.day("staff-1", date(2026, 9, 4))["status"] == "WO"
    assert attendance.day("staff-1", date(2026, 9, 6))["status"] != "WO"
    assert attendance.day("staff-1", date(2026, 9, 11))["status"] != "WO"


def test_full_day_permission_never_carries_minutes():
    leave = PermissionRequest(
        attendance_date=date(2026, 9, 9),
        kind="paid_leave",
        requested_minutes=45,
        reason="Personal leave",
    )
    late = PermissionRequest(
        attendance_date=date(2026, 9, 9),
        kind="late",
        requested_minutes=45,
        reason="Appointment",
    )

    assert leave.requested_minutes == 0
    assert late.requested_minutes == 45


def test_od_and_calendar_status_do_not_deduct():
    od = calculate_day(date(2026, 9, 2), [], approved_permissions=[{"kind": "official_duty"}], now=utc(14, day=5))
    holiday = calculate_day(date(2026, 9, 2), [], non_working_status=AttendanceStatus.HOLIDAY, now=utc(14, day=5))
    assert (od["status"], od["unpaid_minutes"]) == ("OD", 0)
    assert (holiday["status"], holiday["unpaid_minutes"]) == ("H", 0)


def test_unpaid_leave_deducts_one_full_shift_in_minutes():
    leave = calculate_day(
        date(2026, 9, 2),
        [],
        non_working_status=AttendanceStatus.UNPAID_LEAVE,
        now=utc(14, day=5),
    )
    assert (leave["status"], leave["unpaid_minutes"]) == ("UL", 480)
    assert calculate_month([leave])[0]["unpaid_minutes"] == 480


def test_assigned_overnight_shift_and_lop_formula():
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 3, 1, 30, tzinfo=timezone.utc)},
        ],
        shift=Shift(start=time(22, 30), end=time(7, 0)),
        now=datetime(2026, 9, 3, 2, tzinfo=timezone.utc),
    )
    assert result["late_minutes"] == 0
    assert result["early_minutes"] == 0
    assert lop_amount(30, 30000, 10800) == 83.33


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError, match="timezone"):
        calculate_day(date(2026, 9, 2), [{"action": "check_in", "occurred_at": datetime(2026, 9, 2, 10)}])


def test_monthly_grace_automatically_covers_first_sixty_late_or_early_minutes():
    days = [
        {"date": date(2026, 9, 2), "unapproved_minutes": 45},
        {"date": date(2026, 9, 3), "unapproved_minutes": 30},
    ]
    rows = calculate_month(days)
    assert rows[0]["grace_minutes_applied"] == 45
    assert rows[0]["unpaid_minutes"] == 0
    assert rows[1]["grace_minutes_applied"] == 15
    assert rows[1]["unpaid_minutes"] == 15
