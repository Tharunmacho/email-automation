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
    list_permissions,
    whatsapp_attendance_directory,
    whatsapp_private_attendance,
)
from app.attendance.models import PermissionDecision, PermissionRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.db.users import STAFF_ROLE, User


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
