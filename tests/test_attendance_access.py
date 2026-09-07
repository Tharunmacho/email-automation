"""Role boundaries and permission history for attendance."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import mongomock

# Attendance routes import authentication dependencies from the main API, and
# the main API registers the attendance router at the end of its import. Load
# that owner first so this test does not enter the module from the circular end.
from app.api.routes import app as _app  # noqa: F401
from app.attendance.api import list_permissions
from app.attendance.repository import AttendanceRepository
from app.db.users import STAFF_ROLE, User


class FakeUsers:
    def __init__(self):
        self.members = {
            "staff-1": User(id="staff-1", email="one@example.com", name="One", role=STAFF_ROLE),
            "staff-2": User(id="staff-2", email="two@example.com", name="Two", role=STAFF_ROLE),
        }

    def get(self, employee_id):
        return self.members.get(employee_id)

    def list_assignable_staff(self):
        return list(self.members.values())


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
