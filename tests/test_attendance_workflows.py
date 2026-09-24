"""The attendance workflows an employee actually performs, end to end.

Three of them, each of which had a hole before:

Early check-in — the backend refused an unapproved early punch and the request
that would have approved it could not be made from the app at all.

Extra OT — the approval chain existed and nothing could reach it, and only
approved minutes may ever be paid.

Planned duty — rostering one Sunday used to roster every later Sunday too, and
mark each one absent.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from unittest.mock import patch

import mongomock
import pytest

# The attendance routes import their auth dependencies from the main API, which
# registers the attendance router at the end of its own import. Load the owner
# first so this module does not enter from the circular end.
from app.api.routes import app as _app  # noqa: F401
from fastapi import HTTPException

from app.attendance.api import (
    create_duty_plan,
    decide_extra_ot as decide_extra_ot_route,
    delete_duty_plan as delete_duty_plan_route,
    list_duty_plans,
    list_extra_ot,
    record_punch,
    request_extra_ot as request_extra_ot_route,
)
from app.attendance.models import (
    DutyPlanRequest,
    ExtraOTDecision,
    ExtraOTRequest,
    PermissionDecision,
    PermissionRequest,
    PunchRequest,
    Shift,
)
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceError, AttendanceService
from app.db.users import ADMIN_ROLE, MANAGER_ROLE, STAFF_ROLE, User

STAFF = {"id": "staff-1", "role": STAFF_ROLE}
MANAGER = {"id": "manager-1", "role": MANAGER_ROLE}
ADMIN = {"id": "admin-1", "role": ADMIN_ROLE}


class FakeUsers:
    def __init__(self):
        self.members = {
            "staff-1": User(id="staff-1", email="one@example.com", name="One Person",
                            role=STAFF_ROLE, staff_code="AE001"),
            "manager-1": User(id="manager-1", email="mgr@example.com", name="Manager One",
                              role=MANAGER_ROLE, staff_code="AE900"),
            "admin-1": User(id="admin-1", email="admin@example.com", name="Admin",
                            role=ADMIN_ROLE),
        }

    def get(self, user_id):
        return self.members.get(user_id)

    def list_employees(self, include_inactive=False):
        return [m for m in self.members.values() if m.role in {STAFF_ROLE, MANAGER_ROLE}]

    def list_staff(self, include_inactive=False):
        return [self.members["staff-1"]]

    def list_assignable_staff(self):
        return [self.members["staff-1"]]


@pytest.fixture()
def env():
    repository = AttendanceRepository(mongomock.MongoClient()["attendance-workflows"])
    service = AttendanceService(repository)
    with patch("app.attendance.api.users", FakeUsers()), \
         patch("app.attendance.api.service", return_value=service), \
         patch("app.attendance.api.AttendanceRepository", return_value=repository):
        yield service


# --------------------------------------------------------------------------- #
#  Workflow A — early check-in
# --------------------------------------------------------------------------- #
def test_an_early_punch_is_refused_without_an_approved_request(env):
    """The rule the UI could not previously satisfy."""
    early = PunchRequest(
        action="check_in", idempotency_key="early-1",
        occurred_at=datetime(2026, 10, 5, 3, 30, tzinfo=timezone.utc),  # 09:00 IST
    )
    with pytest.raises(AttendanceError, match="early check-in requires approved permission"):
        env.punch("staff-1", early, allow_recorded_time=True)


def test_the_full_early_check_in_workflow(env):
    """Request -> approve -> punch early -> credited as coverage."""
    permission = env.request_permission("staff-1", PermissionRequest(
        attendance_date=date(2026, 10, 5),
        kind="early_check_in",
        requested_minutes=60,
        reason="Client interview at 09:00",
    ))
    assert permission["status"] == "pending"
    # The minutes survive: `early_check_in` is a timed permission, not a full day.
    assert permission["requested_minutes"] == 60

    decided = env.decide_permission(
        permission["id"], PermissionDecision(approved=True, reason="Approved"), "manager-1",
    )
    assert decided["status"] == "approved"

    event, created = env.punch(
        "staff-1",
        PunchRequest(action="check_in", idempotency_key="early-ok",
                     occurred_at=datetime(2026, 10, 5, 3, 30, tzinfo=timezone.utc)),
        allow_recorded_time=True,
    )
    assert created is True
    env.punch(
        "staff-1",
        PunchRequest(action="check_out", idempotency_key="early-out",
                     occurred_at=datetime(2026, 10, 5, 12, 30, tzinfo=timezone.utc)),
        allow_recorded_time=True,
    )

    day = env.day("staff-1", date(2026, 10, 5))
    # 09:00 to 18:00 IST, all of it credited because the early hour was approved.
    assert day["actual_covered_minutes"] == 540
    assert day["uncovered_minutes"] == 0


def test_an_early_check_in_must_be_requested_before_the_shift_starts(env):
    """Approval after the fact is not pre-approval."""
    with patch("app.attendance.service.datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
        with pytest.raises(AttendanceError, match="before shift start"):
            env.request_permission("staff-1", PermissionRequest(
                attendance_date=date(2026, 10, 5), kind="early_check_in",
                requested_minutes=30, reason="Too late to ask",
            ))


# --------------------------------------------------------------------------- #
#  Workflow B — extra OT
# --------------------------------------------------------------------------- #
def test_a_staff_request_is_approved_by_a_manager(env):
    created = request_extra_ot_route(
        ExtraOTRequest(attendance_date=date(2026, 10, 5), requested_minutes=60,
                       reason="Shipment deadline"),
        user=STAFF,
    )
    assert created["status"] == "pending"

    decided = decide_extra_ot_route(
        created["request"]["id"],
        ExtraOTDecision(approved=True, reason="Approved for payroll"),
        approver=MANAGER,
    )
    assert decided["request"]["status"] == "approved"
    assert decided["request"]["decided_by"] == "manager-1"


def test_a_manager_request_needs_an_administrator(env):
    """The escalation requirement 7 asks for, enforced on the decision."""
    created = request_extra_ot_route(
        ExtraOTRequest(attendance_date=date(2026, 10, 5), requested_minutes=90,
                       reason="Weekend release"),
        user=MANAGER,
    )
    with pytest.raises(HTTPException) as refused:
        decide_extra_ot_route(
            created["request"]["id"],
            ExtraOTDecision(approved=True, reason="Approving my own"),
            approver=MANAGER,
        )
    assert refused.value.status_code == 403

    decided = decide_extra_ot_route(
        created["request"]["id"],
        ExtraOTDecision(approved=True, reason="Approved"),
        approver=ADMIN,
    )
    assert decided["request"]["status"] == "approved"


def test_only_approved_minutes_are_counted_for_payroll(env):
    for minutes, decision in ((60, True), (90, None), (30, False)):
        created = request_extra_ot_route(
            ExtraOTRequest(attendance_date=date(2026, 10, 5), requested_minutes=minutes,
                           reason="Work"),
            user=STAFF,
        )
        if decision is not None:
            decide_extra_ot_route(
                created["request"]["id"],
                ExtraOTDecision(approved=decision, reason="Reviewed"),
                approver=MANAGER,
            )

    counted = env.repository.approved_extra_ot_minutes(
        "staff-1", date(2026, 10, 1), date(2026, 10, 31),
    )
    assert counted == 60


def test_a_decision_cannot_be_taken_twice(env):
    created = request_extra_ot_route(
        ExtraOTRequest(attendance_date=date(2026, 10, 5), requested_minutes=60, reason="Work"),
        user=STAFF,
    )
    decide_extra_ot_route(
        created["request"]["id"], ExtraOTDecision(approved=True, reason="Yes"), approver=MANAGER,
    )
    with pytest.raises(HTTPException) as refused:
        decide_extra_ot_route(
            created["request"]["id"], ExtraOTDecision(approved=False, reason="Changed my mind"),
            approver=MANAGER,
        )
    assert refused.value.status_code == 409


def test_staff_see_their_own_requests_and_managers_see_the_roster(env):
    request_extra_ot_route(
        ExtraOTRequest(attendance_date=date(2026, 10, 5), requested_minutes=60, reason="Work"),
        user=STAFF,
    )
    request_extra_ot_route(
        ExtraOTRequest(attendance_date=date(2026, 10, 6), requested_minutes=45, reason="Work"),
        user=MANAGER,
    )

    own = list_extra_ot(2026, 10, employee_id=None, user=STAFF)
    assert {row["employee_id"] for row in own["items"]} == {"staff-1"}

    roster = list_extra_ot(2026, 10, employee_id=None, user=ADMIN)
    assert {row["employee_id"] for row in roster["items"]} == {"staff-1", "manager-1"}


# --------------------------------------------------------------------------- #
#  Workflow D — planned Sunday duty
# --------------------------------------------------------------------------- #
def test_planning_a_sunday_makes_only_that_sunday_a_working_day(env):
    env.repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})

    create_duty_plan(
        DutyPlanRequest(employee_id="staff-1", attendance_date=date(2026, 9, 6),
                        reason="Peak week"),
        admin=ADMIN,
    )

    assert env.day("staff-1", date(2026, 9, 6))["status"] != "WO"
    for untouched in (date(2026, 9, 13), date(2026, 9, 20), date(2026, 10, 4)):
        assert env.day("staff-1", untouched)["status"] == "WO", untouched


def test_a_planned_sunday_is_punchable_and_fully_calculated(env):
    env.repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})
    create_duty_plan(
        DutyPlanRequest(employee_id="staff-1", attendance_date=date(2026, 9, 6), reason="Peak"),
        admin=ADMIN,
    )

    record_punch(
        PunchRequest(action="check_in", idempotency_key="sun-in", employee_id="staff-1",
                     occurred_at=datetime(2026, 9, 6, 4, 30, tzinfo=timezone.utc)),
        user=ADMIN,
    )
    record_punch(
        PunchRequest(action="check_out", idempotency_key="sun-out", employee_id="staff-1",
                     occurred_at=datetime(2026, 9, 6, 13, 30, tzinfo=timezone.utc)),
        user=ADMIN,
    )

    day = env.day("staff-1", date(2026, 9, 6))
    assert day["status"] == "P"
    assert day["required_shift_minutes"] == 480
    assert day["actual_covered_minutes"] == 540
    assert day["uncovered_minutes"] == 0


def test_a_duty_plan_can_carry_its_own_hours(env):
    create_duty_plan(
        DutyPlanRequest(
            employee_id="staff-1", attendance_date=date(2026, 9, 6),
            shift=Shift(start=time(8, 0), end=time(14, 0), break_minutes=30),
            reason="Half day",
        ),
        admin=ADMIN,
    )
    assert env.day("staff-1", date(2026, 9, 6))["required_shift_minutes"] == 330


def test_a_duty_plan_is_listed_and_removable(env):
    env.repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})
    created = create_duty_plan(
        DutyPlanRequest(employee_id="staff-1", attendance_date=date(2026, 9, 6), reason="Peak"),
        admin=ADMIN,
    )
    listed = list_duty_plans(2026, 9, employee_id="staff-1", user=ADMIN)
    assert [row["id"] for row in listed["items"]] == [created["duty_plan"]["id"]]

    delete_duty_plan_route(created["duty_plan"]["id"], admin=ADMIN)
    assert list_duty_plans(2026, 9, employee_id="staff-1", user=ADMIN)["items"] == []
    # And the date goes back to being a weekly off.
    assert env.day("staff-1", date(2026, 9, 6))["status"] == "WO"


def test_removing_a_plan_that_does_not_exist_is_a_404(env):
    with pytest.raises(HTTPException) as refused:
        delete_duty_plan_route("no-such-plan", admin=ADMIN)
    assert refused.value.status_code == 404


def test_replanning_a_date_supersedes_the_previous_plan(env):
    create_duty_plan(
        DutyPlanRequest(employee_id="staff-1", attendance_date=date(2026, 9, 6), reason="First"),
        admin=ADMIN,
    )
    create_duty_plan(
        DutyPlanRequest(employee_id="staff-1", attendance_date=date(2026, 9, 6), reason="Revised"),
        admin=ADMIN,
    )
    plans = list_duty_plans(2026, 9, employee_id="staff-1", user=ADMIN)["items"]
    assert len(plans) == 1
    assert plans[0]["reason"] == "Revised"


def test_a_rolling_shift_change_is_not_a_duty_plan(env):
    """The regression: a shift change must not roster every later weekly off."""
    env.repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})
    env.repository.assign_shift({
        "employee_id": "staff-1",
        "effective_from": "2026-09-01",
        "shift": Shift().model_dump(),
        "reason": "New hours",
    })
    for sunday in (date(2026, 9, 6), date(2026, 9, 13), date(2026, 12, 27)):
        assert env.day("staff-1", sunday)["status"] == "WO", sunday
    # And it is not listed as planned duty, because it is not.
    assert list_duty_plans(2026, 9, employee_id="staff-1", user=ADMIN)["items"] == []
