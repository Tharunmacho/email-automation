from datetime import date, datetime, time, timezone

import pytest

from app.attendance.engine import AttendancePolicy, calculate_day, calculate_month, lop_amount
from app.attendance.models import AttendanceStatus, Shift


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
    assert expired["unpaid_minutes"] == 540


def test_od_and_calendar_status_do_not_deduct():
    od = calculate_day(date(2026, 9, 2), [], approved_permissions=[{"kind": "official_duty"}], now=utc(14, day=5))
    holiday = calculate_day(date(2026, 9, 2), [], non_working_status=AttendanceStatus.HOLIDAY, now=utc(14, day=5))
    assert (od["status"], od["unpaid_minutes"]) == ("OD", 0)
    assert (holiday["status"], holiday["unpaid_minutes"]) == ("H", 0)


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
