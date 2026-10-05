from datetime import date, datetime, time, timezone

import pytest

from app.attendance.engine import IST, AttendancePolicy, calculate_day, calculate_month, lop_amount
from app.attendance.models import AttendanceStatus, DutyPlanRequest, PermissionDecision, PermissionRequest, PunchRequest, Shift
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceError, AttendanceService

import mongomock


def utc(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def test_default_shift_is_calculated_in_kolkata_without_grace():
    # Flexible hours: the 10:20 IST start is compensated by the 19:20 checkout.
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": utc(4, 50)},
            {"action": "check_out", "occurred_at": utc(13, 50)},
        ],
        now=utc(15),
    )
    assert result["late_minutes"] == 20
    assert result["early_minutes"] == 0
    assert result["uncovered_minutes"] == 0
    assert result["status"] == "P"


def test_late_check_in_and_late_checkout_have_no_uncovered_time():
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 54, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 13, 54, tzinfo=timezone.utc)},
        ],
        now=utc(15),
    )
    # 10:24 to 19:24 IST: nine hours present, eight worked after the break.
    assert result["actual_covered_minutes"] == 480
    assert result["uncovered_minutes"] == 0


def test_exact_required_duration_has_no_uncovered_time():
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 13, 30, tzinfo=timezone.utc)},
        ],
        now=utc(15),
    )
    assert result["required_shift_minutes"] == 480
    assert result["actual_covered_minutes"] == 480
    assert result["uncovered_minutes"] == 0


def test_shorter_work_session_reports_only_the_shortage():
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc)},
        ],
        now=utc(15),
    )
    # 10:00 to 18:00 IST is seven hours of work after the break.
    assert result["uncovered_minutes"] == 60


def test_working_beyond_required_duration_has_no_uncovered_time():
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)},
        ],
        now=utc(15),
    )
    assert result["uncovered_minutes"] == 0


def test_approved_permission_reduces_uncovered_time():
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 13, 30, tzinfo=timezone.utc)},
        ],
        approved_permissions=[{"kind": "late", "requested_minutes": 30}],
        now=utc(15),
    )
    assert result["uncovered_minutes"] == 0
    assert result["approved_actual_minutes"] == 30


def test_extra_overtime_does_not_create_uncovered_time():
    result = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)},
        ],
        now=utc(15),
    )
    # 10:00 to 20:00 IST: nine hours worked, one more than required, no charge.
    assert result["actual_covered_minutes"] == 540
    assert result["uncovered_minutes"] == 0


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
    assert calculated["unpaid_minutes"] == 420


def test_a_chosen_friday_replaces_that_weeks_sunday():
    repository = AttendanceRepository(mongomock.MongoClient()["weekly-off-choice"])
    # Week of Monday 31 Aug: Friday chosen. Week of Monday 7 Sep: nothing chosen.
    repository.set_weekly_off_choice("staff-1", date(2026, 8, 31), "friday")
    attendance = AttendanceService(repository)

    assert attendance.day("staff-1", date(2026, 9, 4))["status"] == "WO"   # the chosen Friday
    assert attendance.day("staff-1", date(2026, 9, 6))["status"] != "WO"   # its Sunday is working
    assert attendance.day("staff-1", date(2026, 9, 11))["status"] != "WO"  # next Friday: default
    assert attendance.day("staff-1", date(2026, 9, 13))["status"] == "WO"  # default Sunday off


def test_planned_sunday_duty_overrides_weekly_off():
    repository = AttendanceRepository(mongomock.MongoClient()["planned-sunday"])
    repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})
    repository.assign_shift({
        "employee_id": "staff-1",
        "effective_from": "2026-09-06",
        "shift": Shift().model_dump(),
        "kind": "planned_duty",
    })
    repository.append_event({
        "employee_id": "staff-1", "action": "check_in", "local_date": "2026-09-06",
        "occurred_at": datetime(2026, 9, 6, 4, 30, tzinfo=timezone.utc), "idempotency_key": "sun-in",
    })
    repository.append_event({
        "employee_id": "staff-1", "action": "check_out", "local_date": "2026-09-06",
        "occurred_at": datetime(2026, 9, 6, 13, 30, tzinfo=timezone.utc), "idempotency_key": "sun-out",
    })
    sunday = AttendanceService(repository).day("staff-1", date(2026, 9, 6))
    assert sunday["status"] == "P"
    assert sunday["required_shift_minutes"] == 480
    assert sunday["uncovered_minutes"] == 0


def test_an_early_check_in_is_recorded_without_permission():
    """Hours are flexible: 08:37 IST is recorded, and counts from 09:00."""
    repository = AttendanceRepository(mongomock.MongoClient()["early-check-in"])
    attendance = AttendanceService(repository)
    early = PunchRequest(action="check_in", idempotency_key="early", occurred_at=datetime(2026, 10, 5, 3, 7, tzinfo=timezone.utc))
    event, created = attendance.punch("staff-1", early, allow_recorded_time=True)
    assert created is True
    assert event["action"] == "check_in"

    attendance.punch("staff-1", PunchRequest(
        action="check_out", idempotency_key="out", occurred_at=datetime(2026, 10, 5, 12, 46, tzinfo=timezone.utc),
    ), allow_recorded_time=True)
    day = attendance.day("staff-1", date(2026, 10, 5))
    # 09:00 to 18:16 IST: 556 minutes present, 496 worked.
    assert day["actual_covered_minutes"] == 496
    assert day["uncovered_minutes"] == 0


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


# --------------------------------------------------------------------------- #
#  Coverage equation regressions
#
#  uncovered = max(0, required - covered - approved_permission - recovered)
#
#  Each test below pins one term of that equation. The 10:24 -> 19:21 case that
#  motivated the rewrite lives in
#  `test_late_check_in_and_late_checkout_have_no_uncovered_time` above.
# --------------------------------------------------------------------------- #
def test_the_reported_case_credits_a_full_day_and_deducts_nothing():
    """10:24 IST in, 19:21 IST out: three minutes short, absorbed by the grace."""
    day = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 54, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 13, 51, tzinfo=timezone.utc)},
        ],
        now=utc(15),
    )
    assert day["required_shift_minutes"] == 480
    assert day["actual_covered_minutes"] == 477
    assert day["uncovered_minutes"] == 3
    assert day["late_minutes"] == 24
    # The monthly grace absorbs it, so no deduction appears.
    rolled = calculate_month([day])[0]
    assert rolled["unpaid_minutes"] == 0
    assert rolled["grace_minutes_applied"] == 3


def test_a_time_recovery_adjustment_still_cancels_uncovered_time():
    """Recovered minutes are a term in the equation, not a forgotten field."""
    punches = [
        {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)},
        {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc)},
    ]
    without = calculate_day(date(2026, 9, 2), punches, now=utc(15))
    with_recovery = calculate_day(date(2026, 9, 2), punches, recovered_minutes=60, now=utc(15))

    assert without["uncovered_minutes"] == 60
    assert with_recovery["recovered_minutes"] == 60
    assert with_recovery["uncovered_minutes"] == 0
    # And the month must not charge for what was recovered.
    assert calculate_month([with_recovery])[0]["unpaid_minutes"] == 0


def test_coverage_spans_the_first_check_in_to_the_last_check_out():
    """Several punches in a day are one presence interval, not several."""
    day = calculate_day(
        date(2026, 9, 2),
        [
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)},
            {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)},
            {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 13, 30, tzinfo=timezone.utc)},
        ],
        now=utc(15),
    )
    assert day["actual_covered_minutes"] == 480
    assert day["uncovered_minutes"] == 0


def test_the_requirement_follows_the_shift_break_length():
    """Required duty is the span minus its break, for any configured shift."""
    punches = [
        {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)},
        {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)},
    ]
    half_hour_break = calculate_day(
        date(2026, 9, 2), punches, shift=Shift(break_minutes=30), now=utc(15),
    )
    no_break = calculate_day(
        date(2026, 9, 2), punches, shift=Shift(break_minutes=0), now=utc(15),
    )
    # 450 minutes present; the same break comes off presence as off the duty.
    assert half_hour_break["required_shift_minutes"] == 510
    assert half_hour_break["uncovered_minutes"] == 90
    assert no_break["required_shift_minutes"] == 540
    assert no_break["uncovered_minutes"] == 90


def test_approved_early_arrival_is_credited_as_coverage():
    """Before 09:00 only an approved early check-in earns the time."""
    punches = [
        {"action": "check_in", "occurred_at": datetime(2026, 9, 2, 2, 30, tzinfo=timezone.utc)},
        {"action": "check_out", "occurred_at": datetime(2026, 9, 2, 11, 30, tzinfo=timezone.utc)},
    ]
    approved = calculate_day(
        date(2026, 9, 2), punches,
        approved_permissions=[{"kind": "early_check_in", "requested_minutes": 60}],
        now=utc(15),
    )
    unapproved = calculate_day(date(2026, 9, 2), punches, now=utc(15))

    assert approved["actual_covered_minutes"] == 480
    assert approved["uncovered_minutes"] == 0
    # Without the permission the clock only starts when the flexible window
    # opens at 09:00, so the hour before it earns nothing.
    assert unapproved["actual_covered_minutes"] == 420
    assert unapproved["uncovered_minutes"] == 60


def test_non_working_days_owe_nothing_and_report_no_shortfall():
    for status in (AttendanceStatus.WEEKLY_OFF, AttendanceStatus.HOLIDAY, AttendanceStatus.PAID_LEAVE):
        day = calculate_day(date(2026, 9, 6), [], non_working_status=status, now=utc(15, day=6))
        assert day["required_shift_minutes"] == 0, status
        assert day["uncovered_minutes"] == 0, status
        assert day["unpaid_minutes"] == 0, status


def test_unpaid_leave_is_charged_once_and_not_again_by_the_month():
    day = calculate_day(
        date(2026, 9, 7), [], non_working_status=AttendanceStatus.UNPAID_LEAVE, now=utc(15, day=7),
    )
    assert day["unpaid_minutes"] == 480
    assert day["uncovered_minutes"] == 0
    rolled = calculate_month([day])[0]
    assert rolled["unpaid_minutes"] == 480
    # A full-day absence must not consume the monthly grace balance.
    assert rolled["grace_minutes_applied"] == 0
    assert rolled["permission_minutes_remaining"] == 60


def test_an_expired_missing_punch_is_charged_once_and_spends_no_grace():
    day = calculate_day(
        date(2026, 9, 2),
        [{"action": "check_in", "occurred_at": utc(4, 30)}],
        now=utc(14, day=5),
    )
    assert day["status"] == "A"
    assert day["unpaid_minutes"] == 480
    assert day["uncovered_minutes"] == 0
    rolled = calculate_month([day])[0]
    assert rolled["unpaid_minutes"] == 480
    assert rolled["grace_minutes_applied"] == 0


def test_a_rolling_shift_assignment_does_not_cancel_every_later_weekly_off():
    """Regression: one rostered Sunday used to make every Sunday a working day.

    A recurring assignment sets the hours from a date onwards. Reading it as a
    duty plan meant the Sundays that followed were scheduled days nobody had
    scheduled, each one then marked absent and deducted from salary.
    """
    repository = AttendanceRepository(mongomock.MongoClient()["rolling-shift"])
    repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})
    repository.assign_shift({
        "employee_id": "staff-1",
        "effective_from": "2026-09-01",
        "shift": Shift(start=time(9, 0), end=time(18, 0)).model_dump(),
        "reason": "New office hours",
    })
    attendance = AttendanceService(repository)

    # The hours moved...
    monday = attendance.day("staff-1", date(2026, 9, 7))
    assert monday["shift_start"].astimezone(IST).hour == 9
    # ...but every Sunday is still the weekly off it always was.
    for sunday in (date(2026, 9, 6), date(2026, 9, 13), date(2026, 12, 27)):
        assert attendance.day("staff-1", sunday)["status"] == "WO", sunday


def test_only_the_planned_date_becomes_a_working_day():
    repository = AttendanceRepository(mongomock.MongoClient()["one-sunday"])
    repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})
    AttendanceService(repository).plan_duty(
        DutyPlanRequest(
            employee_id="staff-1",
            attendance_date=date(2026, 9, 6),
            reason="Client delivery",
        ),
        "admin-1",
    )
    attendance = AttendanceService(repository)
    assert attendance.day("staff-1", date(2026, 9, 6))["status"] != "WO"
    assert attendance.day("staff-1", date(2026, 9, 13))["status"] == "WO"


def test_a_planned_sunday_is_worked_paid_and_then_removable():
    repository = AttendanceRepository(mongomock.MongoClient()["sunday-duty-cycle"])
    repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})
    service = AttendanceService(repository)
    plan = service.plan_duty(
        DutyPlanRequest(
            employee_id="staff-1", attendance_date=date(2026, 9, 6), reason="Peak week",
        ),
        "admin-1",
    )
    repository.append_event({
        "employee_id": "staff-1", "action": "check_in", "local_date": "2026-09-06",
        "occurred_at": datetime(2026, 9, 6, 4, 30, tzinfo=timezone.utc), "idempotency_key": "sun-in",
    })
    repository.append_event({
        "employee_id": "staff-1", "action": "check_out", "local_date": "2026-09-06",
        "occurred_at": datetime(2026, 9, 6, 13, 30, tzinfo=timezone.utc), "idempotency_key": "sun-out",
    })

    worked = service.day("staff-1", date(2026, 9, 6))
    assert worked["status"] == "P"
    assert worked["required_shift_minutes"] == 480
    assert worked["actual_covered_minutes"] == 480
    assert worked["uncovered_minutes"] == 0
    assert calculate_month([worked])[0]["unpaid_minutes"] == 0

    # Removing the plan hands the day back to the weekly-off pattern. The
    # punches stay on file; they are simply no longer a scheduled duty.
    assert service.remove_duty_plan(plan["id"]) is True
    assert service.day("staff-1", date(2026, 9, 6))["status"] == "WO"


def test_a_planned_duty_overrides_a_declared_holiday_but_never_approved_leave():
    repository = AttendanceRepository(mongomock.MongoClient()["duty-vs-calendar"])
    service = AttendanceService(repository)
    for day, status in ((date(2026, 9, 6), "H"), (date(2026, 9, 13), "PL")):
        repository.set_calendar_day({
            "employee_id": "staff-1", "attendance_date": day.isoformat(),
            "status": status, "reason": "declared",
        })
        service.plan_duty(
            DutyPlanRequest(employee_id="staff-1", attendance_date=day, reason="Rostered"),
            "admin-1",
        )

    # A holiday can be worked when somebody is explicitly rostered for it.
    assert service.day("staff-1", date(2026, 9, 6))["status"] != "H"
    # Granted leave outranks a roster.
    assert service.day("staff-1", date(2026, 9, 13))["status"] == "PL"


def _approve_weekly_off(attendance: AttendanceService, employee_id: str, day: date) -> dict:
    permission = attendance.request_permission(
        employee_id, PermissionRequest(attendance_date=day, kind="weekly_off", reason="Rotational weekly off"),
    )
    return attendance.decide_permission(
        permission["id"], PermissionDecision(approved=True, reason="ok"), "manager-1",
    )


def test_an_approved_rotational_weekly_off_makes_that_weeks_sunday_a_working_day():
    repository = AttendanceRepository(mongomock.MongoClient()["rotational-weekly-off"])
    attendance = AttendanceService(repository)

    # Week of Monday 28 Sep 2026: Saturday 3 Oct taken instead of Sunday 4 Oct.
    decided = _approve_weekly_off(attendance, "staff-1", date(2026, 10, 3))

    assert decided["status"] == "approved"
    assert decided["weekly_off_day"] == "saturday"
    assert attendance.day("staff-1", date(2026, 10, 3))["status"] == "WO"
    assert attendance.day("staff-1", date(2026, 10, 4))["status"] != "WO"   # Sunday is working
    assert attendance.day("staff-1", date(2026, 10, 11))["status"] == "WO"  # next week: default Sunday


def test_a_pending_rotational_weekly_off_changes_nothing_until_approved():
    repository = AttendanceRepository(mongomock.MongoClient()["rotational-pending"])
    attendance = AttendanceService(repository)
    attendance.request_permission(
        "staff-1", PermissionRequest(attendance_date=date(2026, 10, 6), kind="weekly_off", reason="Personal"),
    )

    assert attendance.day("staff-1", date(2026, 10, 6))["status"] != "WO"
    assert attendance.day("staff-1", date(2026, 10, 11))["status"] == "WO"


def test_a_rejected_rotational_weekly_off_keeps_sunday():
    repository = AttendanceRepository(mongomock.MongoClient()["rotational-rejected"])
    attendance = AttendanceService(repository)
    permission = attendance.request_permission(
        "staff-1", PermissionRequest(attendance_date=date(2026, 10, 7), kind="weekly_off", reason="Personal"),
    )
    attendance.decide_permission(permission["id"], PermissionDecision(approved=False, reason="busy"), "manager-1")

    assert attendance.day("staff-1", date(2026, 10, 7))["status"] != "WO"
    assert attendance.day("staff-1", date(2026, 10, 11))["status"] == "WO"


def test_only_one_rotational_weekly_off_per_week_and_never_on_sunday():
    repository = AttendanceRepository(mongomock.MongoClient()["rotational-limits"])
    attendance = AttendanceService(repository)
    _approve_weekly_off(attendance, "staff-1", date(2026, 10, 7))

    with pytest.raises(AttendanceError, match="already been requested"):
        attendance.request_permission(
            "staff-1", PermissionRequest(attendance_date=date(2026, 10, 9), kind="weekly_off", reason="again"),
        )
    with pytest.raises(AttendanceError, match="Sunday"):
        attendance.request_permission(
            "staff-1", PermissionRequest(attendance_date=date(2026, 10, 18), kind="weekly_off", reason="sunday"),
        )
    # A different week is unaffected.
    _approve_weekly_off(attendance, "staff-1", date(2026, 10, 13))
    assert attendance.day("staff-1", date(2026, 10, 13))["status"] == "WO"
    assert attendance.day("staff-1", date(2026, 10, 18))["status"] != "WO"
