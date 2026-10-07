"""Leave, permission and Extra OT requests go to the requester's branch manager.

Royapettah is the Singapore and Malaysia desk, managed by Noorul. Mount Road is
every other destination, managed by Rafi. Each manager is told about, lists and
decides only their own branch's requests; an administrator can decide any.

Noorul is also the finance manager for both branches, so paid leave, LOP and
Extra OT from Mount Road need Rafi *and* Noorul.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import mongomock
import pytest
from fastapi import HTTPException

from app.api.routes import app as _app  # noqa: F401  (resolves the routes/attendance import cycle)
from app.attendance.api import (
    decide_extra_ot,
    decide_permission,
    list_extra_ot,
    list_permissions,
    request_extra_ot,
    request_permission,
    respond_to_cover,
)
from app.attendance.models import CoverResponse, ExtraOTDecision, ExtraOTRequest, PermissionDecision, PermissionRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.branches import MOUNT_ROAD, ROYAPETTAH, branch_of
from app.db.users import ADMIN_ROLE, EMPLOYEE_ROLES, FINANCE_MANAGER_ROLE, MANAGER_ROLE, MANAGER_ROLES, STAFF_ROLE, User

NOORUL = User(id="noorul", email="noorul.adira@gmail.com", name="Noorul", role=FINANCE_MANAGER_ROLE)
RAFI = User(id="rafi", email="hr@findurjob.com", name="Rafi", role=MANAGER_ROLE)
SREYA = User(id="sreya", email="sreya.adira@gmail.com", name="Sreya", role=STAFF_ROLE)
RAVI = User(id="ravi", email="ravi@adira.test", name="Ravi", role=STAFF_ROLE)
ADMIN = User(id="admin", email="admin@adira.test", name="Admin", role=ADMIN_ROLE)
EVERYONE = [NOORUL, RAFI, SREYA, RAVI, ADMIN]


class Users:
    def get(self, user_id):
        return next((u for u in EVERYONE if u.id == user_id), None)

    def list_managers(self, include_inactive=False):
        return [u for u in EVERYONE if u.role in MANAGER_ROLES]

    def list_finance_managers(self, include_inactive=False):
        return [u for u in EVERYONE if u.role == FINANCE_MANAGER_ROLE]

    def list_admins(self, include_inactive=False):
        return [ADMIN]

    def list_staff(self, include_inactive=False):
        return [u for u in EVERYONE if u.role == STAFF_ROLE]

    def list_employees(self, include_inactive=False):
        return [u for u in EVERYONE if u.role in EMPLOYEE_ROLES]


class Notifications:
    sent: list[str] = []

    def record(self, user_id, **_values):
        type(self).sent.append(user_id)


def as_user(user):
    return {"id": user.id, "role": user.role}


@pytest.fixture()
def repository():
    repo = AttendanceRepository(mongomock.MongoClient()["branch-routing"])
    Notifications.sent = []
    with patch("app.attendance.api.users", Users()), \
         patch("app.attendance.api.service", return_value=AttendanceService(repo)), \
         patch("app.attendance.api.AttendanceRepository", return_value=repo), \
         patch("app.attendance.api.NotificationRepository", return_value=Notifications()):
        yield repo


#: Who covers whose leave day in these tests.
COVERS = {"sreya": "noorul", "ravi": "rafi"}  # the only same-branch colleague


def leave(user):
    """A leave day, accepted by the cover, so it has reached the approver."""
    permission = request_permission(
        PermissionRequest(attendance_date=date(2026, 10, 10), kind="paid_leave", reason="Family",
                          cover_employee_id=COVERS[user.id]),
        user=as_user(user),
    )["permission"]
    Notifications.sent = []
    respond_to_cover(permission["id"], CoverResponse(accepted=True),
                     user={"id": COVERS[user.id], "role": MANAGER_ROLE})
    # Drop the "your cover accepted" note to the requester; keep the approvers.
    Notifications.sent = [sent for sent in Notifications.sent if sent != user.id]
    return repository_permission(permission["id"])


def repository_permission(permission_id):
    from app.attendance.api import AttendanceRepository as Repo

    return Repo().permission(permission_id)


def overtime(user):
    return request_extra_ot(
        ExtraOTRequest(attendance_date=date(2026, 9, 20), requested_minutes=90, reason="Deadline"),
        user=as_user(user),
    )["request"]


def test_the_branches_follow_the_desks():
    assert branch_of(SREYA) == branch_of(NOORUL) == ROYAPETTAH
    assert branch_of(RAVI) == branch_of(RAFI) == MOUNT_ROAD


def test_singapore_malaysia_leave_goes_to_noorul(repository):
    leave(SREYA)
    assert Notifications.sent == ["noorul"]


def test_other_destination_leave_goes_to_rafi_and_the_finance_manager(repository):
    leave(RAVI)
    assert Notifications.sent == ["rafi", "noorul"]


def test_extra_ot_goes_to_the_branch_manager_and_the_finance_manager(repository):
    overtime(SREYA)
    overtime(RAVI)
    # Noorul is Sreya's branch manager and the finance manager: told once.
    assert Notifications.sent == ["noorul", "rafi", "noorul"]


def test_a_permission_that_moves_no_money_needs_only_the_branch_manager(repository):
    late = request_permission(
        PermissionRequest(attendance_date=date(2030, 10, 10), kind="late", requested_minutes=30, reason="Traffic"),
        user=as_user(RAVI),
    )["permission"]
    assert Notifications.sent == ["rafi"]
    decided = decide_permission(late["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(RAFI))
    assert decided["status"] == "approved"


def test_mount_road_leave_needs_rafi_and_noorul(repository):
    permission = leave(RAVI)
    first = decide_permission(permission["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(RAFI))
    assert first["status"] == "pending"
    assert first["permission"]["awaiting_stages"] == ["finance"]
    assert first["permission"]["can_decide"] is False
    assert repository.calendar_day("ravi", date(2026, 10, 10)) is None

    with pytest.raises(HTTPException) as again:
        decide_permission(permission["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(RAFI))
    assert again.value.status_code == 409

    second = decide_permission(permission["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(NOORUL))
    assert second["status"] == "approved"
    assert set(second["permission"]["approvals"]) == {"manager", "finance"}
    assert repository.calendar_day("ravi", date(2026, 10, 10))["status"] == "PL"


def test_the_finance_manager_may_sign_first(repository):
    request = overtime(RAVI)
    first = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(NOORUL))
    assert first["status"] == "pending"
    assert first["request"]["awaiting_stages"] == ["manager"]
    second = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(RAFI))
    assert second["request"]["status"] == "approved"


def test_one_rejection_rejects_a_dual_approval_request(repository):
    request = overtime(RAVI)
    decided = decide_extra_ot(request["id"], ExtraOTDecision(approved=False, reason="No"), approver=as_user(NOORUL))
    assert decided["request"]["status"] == "rejected"
    assert decided["request"]["approvals"]["finance"]["approved"] is False


def test_lop_from_another_branch_also_needs_finance(repository):
    lop = request_permission(
        PermissionRequest(attendance_date=date(2026, 10, 11), kind="unpaid_leave", reason="Travel",
                          cover_employee_id="rafi"),
        user=as_user(RAVI),
    )["permission"]
    respond_to_cover(lop["id"], CoverResponse(accepted=True), user=as_user(RAFI))
    decided = decide_permission(lop["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(RAFI))
    assert decided["status"] == "pending"


def test_a_managers_own_request_goes_to_the_admin_and_the_finance_manager(repository):
    request = overtime(RAFI)
    assert Notifications.sent == ["admin", "noorul"]
    first = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(ADMIN))
    assert first["status"] == "pending"
    second = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(NOORUL))
    assert second["request"]["status"] == "approved"


def test_the_finance_managers_own_request_is_signed_by_the_admin(repository):
    request = overtime(NOORUL)
    assert Notifications.sent == ["admin"]
    with pytest.raises(HTTPException):
        decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="Mine"), approver=as_user(NOORUL))
    decided = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(ADMIN))
    assert decided["request"]["status"] == "approved"


def test_the_finance_manager_lists_money_requests_from_every_branch(repository):
    leave(SREYA)
    leave(RAVI)
    request_permission(
        PermissionRequest(attendance_date=date(2030, 10, 10), kind="late", requested_minutes=30, reason="Traffic"),
        user=as_user(RAVI),
    )
    overtime(RAVI)

    permissions = list_permissions(2026, 10, employee_id=None, user=as_user(NOORUL))["items"]
    assert {(row["employee_id"], row["kind"]) for row in permissions} == {("sreya", "paid_leave"), ("ravi", "paid_leave")}
    ravi = next(row for row in permissions if row["employee_id"] == "ravi")
    assert ravi["employee_name"] == "Ravi" and ravi["can_decide"] is True
    # A late permission from Mount Road is for Rafi alone.
    assert list_permissions(2030, 10, employee_id=None, user=as_user(NOORUL))["items"] == []
    assert [row["employee_id"] for row in list_extra_ot(2026, 9, employee_id=None, user=as_user(NOORUL))["items"]] == ["ravi"]


def test_a_manager_cannot_decide_the_other_branchs_leave(repository):
    permission = leave(SREYA)
    with pytest.raises(HTTPException) as refused:
        decide_permission(permission["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(RAFI))
    assert refused.value.status_code == 403

    decided = decide_permission(permission["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(NOORUL))
    assert decided["status"] == "approved"


def test_a_manager_cannot_decide_the_other_branchs_ot(repository):
    request = overtime(SREYA)
    with pytest.raises(HTTPException) as refused:
        decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(RAFI))
    assert refused.value.status_code == 403

    decided = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(NOORUL))
    assert decided["request"]["status"] == "approved"


def test_an_administrator_can_decide_either_branchs_manager_stage(repository):
    request = overtime(SREYA)
    decided = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(ADMIN))
    # The finance manager still has to sign.
    assert decided["status"] == "pending"
    assert decided["request"]["awaiting_stages"] == ["finance"]


def test_each_manager_lists_only_their_own_branch(repository):
    leave(SREYA)
    leave(RAVI)
    overtime(SREYA)
    overtime(RAVI)

    # Noorul, as finance manager, also sees Mount Road's money requests (above).
    permissions = list_permissions(2026, 10, employee_id=None, user=as_user(RAFI))["items"]
    extra_ot = list_extra_ot(2026, 9, employee_id=None, user=as_user(RAFI))["items"]
    assert {row["employee_id"] for row in permissions} == {"ravi"}
    assert {row["employee_id"] for row in extra_ot} == {"ravi"}


# --------------------------------------------------------------------------- #
#  Visibility: each manager sees only their own branch
# --------------------------------------------------------------------------- #
from app.attendance.api import attendance_day, attendance_employees  # noqa: E402
from app.payroll import _visible_employees, update_employee_payroll, EmployeePayrollPolicy  # noqa: E402


def test_each_manager_sees_only_their_branch_in_payroll():
    with patch("app.payroll.users", Users()):
        assert {e.id for e in _visible_employees(as_user(NOORUL))} == {"noorul", "sreya"}
        assert {e.id for e in _visible_employees(as_user(RAFI))} == {"rafi", "ravi"}
        assert {e.id for e in _visible_employees(as_user(ADMIN))} == {"noorul", "rafi", "sreya", "ravi"}


def test_a_manager_cannot_change_the_other_branchs_salary(repository):
    with patch("app.payroll.users", Users()), \
         patch("app.payroll.AttendanceRepository", return_value=repository):
        with pytest.raises(HTTPException):
            update_employee_payroll("sreya", EmployeePayrollPolicy(monthly_salary=1), _user=as_user(RAFI))
        update_employee_payroll("sreya", EmployeePayrollPolicy(monthly_salary=25000), _user=as_user(NOORUL))
    assert repository.employee_policy("sreya")["monthly_salary"] == 25000


def test_each_manager_sees_only_their_branch_in_attendance(repository):
    assert [e["id"] for e in attendance_employees(user=as_user(NOORUL))["items"]] == ["sreya"]
    assert [e["id"] for e in attendance_employees(user=as_user(RAFI))["items"]] == ["ravi"]


def test_a_manager_cannot_open_the_other_branchs_attendance(repository):
    day = date(2026, 9, 1)
    assert attendance_day(day, employee_id="sreya", user=as_user(NOORUL))["employee_id"] == "sreya"
    with pytest.raises(HTTPException) as refused:
        attendance_day(day, employee_id="sreya", user=as_user(RAFI))
    assert refused.value.status_code == 404
    # Nor another manager's.
    with pytest.raises(HTTPException):
        attendance_day(day, employee_id="noorul", user=as_user(RAFI))
