"""The monthly three-hour paid permission and the separate 60-minute grace.

From November 2026 approved late-arrival / early-leaving permission is paid
from its own three hours a month, and the automatic 60-minute grace only
forgives late or early time no permission paid for. Earlier months keep the
old rule: one shared 60-minute balance and at most two approved occasions.
"""
from datetime import date, datetime, timezone

from app.attendance.engine import (
    PAID_PERMISSION_START,
    SHARED_POOL_POLICY,
    AttendancePolicy,
    calculate_day,
    calculate_month,
    policy_for_month,
)


def nov(hour: int, minute: int = 0, day: int = 3) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def approved(day: int, minutes: int, month: int = 11) -> dict:
    """A day whose shortfall was exactly covered by an approved permission."""
    return {"date": date(2026, month, day), "approved_actual_minutes": minutes,
            "approved_occurrence_minutes": [minutes], "uncovered_minutes": 0}


def late(day: int, minutes: int, month: int = 11) -> dict:
    """A day short by ``minutes`` with no permission."""
    return {"date": date(2026, month, day), "approved_actual_minutes": 0, "uncovered_minutes": minutes}


def test_the_new_rule_starts_in_november_2026():
    assert PAID_PERMISSION_START == date(2026, 11, 1)
    assert policy_for_month(date(2026, 10, 31)) is SHARED_POOL_POLICY
    current = policy_for_month(date(2026, 11, 1))
    assert current.monthly_permission_minutes == 180
    assert current.monthly_grace_minutes == 60
    assert current.shared_pool is False


def test_an_approved_late_permission_is_paid_from_the_three_hours_not_the_grace():
    # 11:30 IST arrival (90 min late), 19:00 IST departure, 90 min approved.
    day = calculate_day(
        date(2026, 11, 3),
        [
            {"action": "check_in", "occurred_at": nov(6, 0)},
            {"action": "check_out", "occurred_at": nov(13, 30)},
        ],
        approved_permissions=[{"kind": "late", "requested_minutes": 90}],
        now=nov(15),
    )
    row = calculate_month([day])[0]
    assert row["approved_permission_minutes"] == 90
    assert row["grace_minutes_applied"] == 0
    assert row["unpaid_minutes"] == 0
    assert row["status"] == "PP"
    assert row["permission_minutes_remaining"] == 90
    assert row["grace_minutes_remaining"] == 60


def test_late_time_without_permission_uses_the_grace_and_leaves_the_three_hours():
    rows = calculate_month([late(3, 40), late(4, 40)])
    assert [row["grace_minutes_applied"] for row in rows] == [40, 20]
    assert [row["unpaid_minutes"] for row in rows] == [0, 20]
    assert rows[-1]["grace_minutes_remaining"] == 0
    assert rows[-1]["permission_minutes_remaining"] == 180


def test_both_balances_are_used_in_the_same_month():
    rows = calculate_month([approved(3, 120), late(4, 30), approved(5, 60), late(6, 45)])
    assert sum(row["approved_permission_minutes"] for row in rows) == 180
    assert sum(row["grace_minutes_applied"] for row in rows) == 60
    assert [row["unpaid_minutes"] for row in rows] == [0, 0, 0, 15]


def test_permission_beyond_three_hours_falls_to_the_grace_then_is_deducted():
    rows = calculate_month([approved(3, 80), approved(4, 80), approved(5, 80), approved(6, 30)])
    assert [row["approved_permission_minutes"] for row in rows] == [80, 80, 20, 0]
    assert [row["grace_minutes_applied"] for row in rows] == [0, 0, 60, 0]
    assert [row["unpaid_minutes"] for row in rows] == [0, 0, 0, 30]
    # Overtime may cancel this: it is late time, not a missing day.
    assert rows[-1]["late_unpaid_minutes"] == 30
    assert rows[-1]["permission_exhausted"] is True


def test_there_is_no_two_occasion_limit_any_more():
    rows = calculate_month([approved(day, 15) for day in range(3, 9)])
    assert all(row["approved_permission_minutes"] == 15 for row in rows)
    assert rows[-1]["permission_occasions_used"] == 6
    assert rows[-1]["permission_minutes_remaining"] == 90
    assert all(row["unpaid_minutes"] == 0 for row in rows)


def test_october_keeps_the_old_shared_sixty_minutes():
    rows = calculate_month([approved(5, 30, month=10), late(6, 40, month=10)])
    assert rows[0]["approved_permission_minutes"] == 30
    # Only 30 of the shared 60 is left for the late day.
    assert rows[1]["grace_minutes_applied"] == 30
    assert rows[1]["unpaid_minutes"] == 10
    assert rows[1]["permission_minutes_remaining"] == 0


def test_the_same_october_days_under_the_new_rule():
    rows = calculate_month([approved(5, 30, month=10), late(6, 40, month=10)], policy=AttendancePolicy())
    assert rows[1]["grace_minutes_applied"] == 40
    assert rows[1]["unpaid_minutes"] == 0
