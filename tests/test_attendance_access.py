"""Role boundaries and permission history for attendance."""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import mongomock

# Attendance routes import authentication dependencies from the main API, and
# the main API registers the attendance router at the end of its import. Load
# that owner first so this test does not enter the module from the circular end.
from app.api.routes import app as _app  # noqa: F401
from fastapi import HTTPException

from app.attendance.api import (
    WhatsAppAttendanceEvent,
    decide_permission as decide_permission_route,
    list_permissions,
    request_permission as request_permission_route,
    update_weekly_off as update_weekly_off_route,
    whatsapp_attendance_directory,
    whatsapp_private_attendance,
)
from app.attendance.models import PermissionDecision, PermissionRequest, WeeklyOffRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.db.users import ADMIN_ROLE, MANAGER_ROLE, STAFF_ROLE, User
from app.payroll import EmployeePayrollPolicy, _visible_employees, update_employee_payroll


class FakeUsers:
    def __init__(self):
        self.members = {
            "staff-1": User(
                id="staff-1",
                email="one@example.com",
                name="One Person",
                role=STAFF_ROLE,
                staff_code="AE001",
                phone="+91 98765 43210",
            ),
            "staff-2": User(
                id="staff-2",
                email="two@example.com",
                name="Two Person",
                role=STAFF_ROLE,
                staff_code="AE002",
                phone="+91 91234 56789",
            ),
        }

    def get(self, employee_id):
        return self.members.get(employee_id)

    def list_assignable_staff(self):
        return list(self.members.values())

    def list_employees(self, include_inactive=False):
        return [member for member in self.members.values() if include_inactive or member.active]


class RecordingRepository:
    requested = None

    def permissions_for_period(self, employee_ids, start, end):
        type(self).requested = (employee_ids, start, end)
        return []


class ApprovalUsers(FakeUsers):
    def __init__(self):
        super().__init__()
        self.members.update({
            "manager-1": User(id="manager-1", email="manager@example.com", name="Manager One", role=MANAGER_ROLE),
            "manager-2": User(id="manager-2", email="manager2@example.com", name="Manager Two", role=MANAGER_ROLE),
            "admin-1": User(id="admin-1", email="admin@example.com", name="Super Admin", role=ADMIN_ROLE),
        })

    def list_admins(self, include_inactive=False):
        return [self.members["admin-1"]]

    def list_managers(self, include_inactive=False):
        return [self.members["manager-1"], self.members["manager-2"]]

    def list_staff(self, include_inactive=False):
        return [self.members["staff-1"], self.members["staff-2"]]


class CapturedNotifications:
    recipients = []

    def record(self, user_id, **_values):
        type(self).recipients.append(user_id)


def test_manager_leave_request_notifies_only_super_admin():
    repository = AttendanceRepository(mongomock.MongoClient()["manager-request"])
    CapturedNotifications.recipients = []
    payload = PermissionRequest(
        attendance_date=date(2026, 9, 10),
        kind="paid_leave",
        reason="Personal leave",
    )
    with patch("app.attendance.api.users", ApprovalUsers()), patch(
        "app.attendance.api.service", return_value=AttendanceService(repository)
    ), patch("app.attendance.api.NotificationRepository", return_value=CapturedNotifications()):
        request_permission_route(payload, user={"id": "manager-1", "role": MANAGER_ROLE})

    assert CapturedNotifications.recipients == ["admin-1"]


def test_manager_cannot_approve_another_manager_request():
    repository = AttendanceRepository(mongomock.MongoClient()["manager-approval-boundary"])
    request = repository.create_permission({
        "employee_id": "manager-1",
        "attendance_date": "2026-09-10",
        "kind": "paid_leave",
        "requested_minutes": 0,
        "reason": "Personal leave",
    })
    with patch("app.attendance.api.users", ApprovalUsers()), patch(
        "app.attendance.api.service", return_value=AttendanceService(repository)
    ):
        try:
            decide_permission_route(
                request["id"],
                PermissionDecision(approved=True, reason="Approved"),
                admin={"id": "manager-2", "role": MANAGER_ROLE},
            )
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("A manager request must require administrator approval")


def test_super_admin_can_approve_manager_request():
    repository = AttendanceRepository(mongomock.MongoClient()["admin-manager-approval"])
    request = repository.create_permission({
        "employee_id": "manager-1",
        "attendance_date": "2026-09-10",
        "kind": "paid_leave",
        "requested_minutes": 0,
        "reason": "Personal leave",
    })
    with patch("app.attendance.api.users", ApprovalUsers()), patch(
        "app.attendance.api.service", return_value=AttendanceService(repository)
    ):
        result = decide_permission_route(
            request["id"],
            PermissionDecision(approved=True, reason="Approved by super admin"),
            admin={"id": "admin-1", "role": ADMIN_ROLE},
        )

    assert result["status"] == "approved"


def test_manager_approval_inbox_contains_staff_only():
    RecordingRepository.requested = None
    with patch("app.attendance.api.users", ApprovalUsers()), patch(
        "app.attendance.api.AttendanceRepository", RecordingRepository
    ):
        list_permissions(2026, 9, employee_id=None, user={"id": "manager-1", "role": MANAGER_ROLE})

    employee_ids, _start, _end = RecordingRepository.requested
    assert employee_ids == ["staff-1", "staff-2"]


def test_staff_payroll_is_scoped_to_the_signed_in_employee():
    with patch("app.payroll.users", FakeUsers()):
        visible = _visible_employees({"id": "staff-2", "role": "staff"})

    assert [employee.id for employee in visible] == ["staff-2"]


def test_staff_selects_only_their_own_weekly_off_from_attendance():
    repository = AttendanceRepository(mongomock.MongoClient()["self-weekly-off"])
    with patch("app.attendance.api.users", FakeUsers()), patch(
        "app.attendance.api.AttendanceRepository", return_value=repository
    ):
        result = update_weekly_off_route(
            WeeklyOffRequest(weekly_off_pattern="alternate_friday"),
            user={"id": "staff-2", "role": STAFF_ROLE},
        )

    assert result["employee_id"] == "staff-2"
    assert repository.employee_policy("staff-2")["weekly_off_pattern"] == "alternate_friday"
    assert repository.employee_policy("staff-1")["weekly_off_pattern"] == "sunday"


def test_saving_salary_does_not_overwrite_staff_weekly_off():
    repository = AttendanceRepository(mongomock.MongoClient()["salary-keeps-weekly-off"])
    repository.set_employee_policy("staff-1", {"weekly_off_pattern": "alternate_friday"})
    with patch("app.payroll.users", FakeUsers()), patch(
        "app.payroll.AttendanceRepository", return_value=repository
    ):
        update_employee_payroll(
            "staff-1",
            EmployeePayrollPolicy(monthly_salary=32000),
            _user={"id": "manager-1", "role": MANAGER_ROLE},
        )

    policy = repository.employee_policy("staff-1")
    assert policy["monthly_salary"] == 32000
    assert policy["weekly_off_pattern"] == "alternate_friday"


def test_staff_permission_history_ignores_another_employee_id():
    user = {"id": "staff-1", "role": "staff"}
    with patch("app.attendance.api.users", FakeUsers()), patch(
        "app.attendance.api.AttendanceRepository", RecordingRepository
    ):
        result = list_permissions(2026, 9, employee_id="staff-2", user=user)

    assert result["count"] == 0
    assert RecordingRepository.requested == (
        "staff-1",
        date(2026, 9, 1),
        date(2026, 9, 30),
    )


def test_admin_permission_history_covers_the_active_roster():
    with patch("app.attendance.api.users", FakeUsers()), patch(
        "app.attendance.api.AttendanceRepository", RecordingRepository
    ):
        list_permissions(2026, 9, employee_id=None, user={"id": "admin", "role": "admin"})

    employee_ids, start, end = RecordingRepository.requested
    assert set(employee_ids) == {"staff-1", "staff-2"}
    assert (start, end) == (date(2026, 9, 1), date(2026, 9, 30))


def test_admin_decision_keeps_the_staff_reason_separate():
    database = mongomock.MongoClient()["attendance-test"]
    repository = AttendanceRepository(database)
    created = repository.create_permission({
        "employee_id": "staff-1",
        "attendance_date": "2026-09-07",
        "kind": "late",
        "requested_minutes": 15,
        "reason": "Doctor appointment",
    })

    decided = repository.decide_permission(created["id"], {
        "approved": True,
        "reason": "Approved by administrator",
    })

    assert decided["reason"] == "Doctor appointment"
    assert decided["decision_reason"] == "Approved by administrator"
    assert decided["status"] == "approved"


def test_attendance_period_starts_at_first_real_employee_activity():
    repository = AttendanceRepository(mongomock.MongoClient()["attendance-start"])
    assert repository.attendance_start_date("staff-1") is None
    repository.append_event({
        "employee_id": "staff-1",
        "action": "check_in",
        "occurred_at": datetime(2026, 9, 8, 4, 30, tzinfo=timezone.utc),
        "local_date": "2026-09-08",
        "source": "web",
        "idempotency_key": "first-punch",
        "evidence": {},
    })
    repository.set_calendar_day({
        "employee_id": "staff-1",
        "attendance_date": "2026-09-10",
        "status": "PL",
    })
    assert repository.attendance_start_date("staff-1") == date(2026, 9, 8)


def test_approved_leave_request_updates_calendar_and_second_paid_leave_becomes_unpaid():
    repository = AttendanceRepository(mongomock.MongoClient()["leave-approval"])
    service = AttendanceService(repository)
    for day, expected in ((7, "PL"), (8, "UL")):
        request = service.request_permission("staff-1", PermissionRequest(
            attendance_date=date(2026, 9, day),
            kind="paid_leave",
            reason="Personal leave",
        ))
        decided = service.decide_permission(
            request["id"],
            PermissionDecision(approved=True, reason="Approved by manager"),
            "manager-1",
        )
        assert decided["calendar_status"] == expected


def test_private_whatsapp_attendance_matches_phone_and_first_name():
    repository = AttendanceRepository(mongomock.MongoClient()["whatsapp-attendance"])
    payload = WhatsAppAttendanceEvent(
        message_id="wamid.check-in-1",
        sender_phone="919876543210",
        stated_name="One",
        action="check_in",
        occurred_at=datetime(2026, 9, 8, 4, 45, tzinfo=timezone.utc),
    )

    with patch("app.attendance.api.users", FakeUsers()), patch(
        "app.attendance.api.service", return_value=AttendanceService(repository)
    ):
        result = whatsapp_private_attendance(payload)
        duplicate = whatsapp_private_attendance(payload)

    assert result["status"] == "recorded"
    assert result["event"]["employee_id"] == "staff-1"
    assert result["event"]["source"] == "whatsapp"
    assert result["event"]["occurred_at"] == payload.occurred_at
    assert result["attendance"]["check_in"] == payload.occurred_at
    assert result["attendance"]["provisional"] is True
    assert duplicate["status"] == "duplicate"


def test_private_whatsapp_attendance_rejects_wrong_name():
    payload = WhatsAppAttendanceEvent(
        message_id="wamid.wrong-name",
        sender_phone="919876543210",
        stated_name="Two",
        action="check_in",
        occurred_at=datetime(2026, 9, 8, 4, 45, tzinfo=timezone.utc),
    )

    with patch("app.attendance.api.users", FakeUsers()):
        try:
            whatsapp_private_attendance(payload)
        except HTTPException as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError("mismatched employee name must be rejected")


def test_whatsapp_attendance_directory_returns_active_employee_phones():
    with patch("app.attendance.api.users", FakeUsers()):
        result = whatsapp_attendance_directory()

    assert result["count"] == 2
    assert result["contacts"][0]["phone"] == "+91 98765 43210"
