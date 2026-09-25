"""Leave, permission and Extra OT requests go to the requester's branch manager.

Royapettah is the Singapore and Malaysia desk, managed by Noorul. Mount Road is
every other destination, managed by Rafi. Each manager is told about, lists and
decides only their own branch's requests; an administrator can decide any.
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
)
from app.attendance.models import ExtraOTDecision, ExtraOTRequest, PermissionDecision, PermissionRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.branches import MOUNT_ROAD, ROYAPETTAH, branch_of
from app.db.users import ADMIN_ROLE, MANAGER_ROLE, STAFF_ROLE, User

NOORUL = User(id="noorul", email="noorul.adira@gmail.com", name="Noorul", role=MANAGER_ROLE)
RAFI = User(id="rafi", email="hr@findurjob.com", name="Rafi", role=MANAGER_ROLE)
SREYA = User(id="sreya", email="sreya.adira@gmail.com", name="Sreya", role=STAFF_ROLE)
RAVI = User(id="ravi", email="ravi@adira.test", name="Ravi", role=STAFF_ROLE)
ADMIN = User(id="admin", email="admin@adira.test", name="Admin", role=ADMIN_ROLE)
EVERYONE = [NOORUL, RAFI, SREYA, RAVI, ADMIN]


class Users:
    def get(self, user_id):
        return next((u for u in EVERYONE if u.id == user_id), None)

    def list_managers(self, include_inactive=False):
        return [u for u in EVERYONE if u.role == MANAGER_ROLE]

    def list_admins(self, include_inactive=False):
        return [ADMIN]

    def list_staff(self, include_inactive=False):
        return [u for u in EVERYONE if u.role == STAFF_ROLE]

    def list_employees(self, include_inactive=False):
        return [u for u in EVERYONE if u.role in {STAFF_ROLE, MANAGER_ROLE}]


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


def leave(user):
    return request_permission(
        PermissionRequest(attendance_date=date(2026, 10, 10), kind="paid_leave", reason="Family"),
        user=as_user(user),
    )["permission"]


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


def test_other_destination_leave_goes_to_rafi(repository):
    leave(RAVI)
    assert Notifications.sent == ["rafi"]


def test_extra_ot_goes_to_the_branch_manager(repository):
    overtime(SREYA)
    overtime(RAVI)
    assert Notifications.sent == ["noorul", "rafi"]


def test_a_manager_cannot_decide_the_other_branchs_leave(repository):
    permission = leave(SREYA)
    with pytest.raises(HTTPException) as refused:
        decide_permission(permission["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(RAFI))
    assert refused.value.status_code == 403

    decided = decide_permission(permission["id"], PermissionDecision(approved=True, reason="OK"), admin=as_user(NOORUL))
    assert decided["status"] == "approved"


def test_a_manager_cannot_decide_the_other_branchs_ot(repository):
    request = overtime(RAVI)
    with pytest.raises(HTTPException) as refused:
        decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(NOORUL))
    assert refused.value.status_code == 403

    decided = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(RAFI))
    assert decided["request"]["status"] == "approved"


def test_an_administrator_can_decide_either_branch(repository):
    request = overtime(SREYA)
    decided = decide_extra_ot(request["id"], ExtraOTDecision(approved=True, reason="OK"), approver=as_user(ADMIN))
    assert decided["request"]["status"] == "approved"


def test_each_manager_lists_only_their_own_branch(repository):
    leave(SREYA)
    leave(RAVI)
    overtime(SREYA)
    overtime(RAVI)

    for manager, own in ((NOORUL, "sreya"), (RAFI, "ravi")):
        permissions = list_permissions(2026, 10, employee_id=None, user=as_user(manager))["items"]
        extra_ot = list_extra_ot(2026, 9, employee_id=None, user=as_user(manager))["items"]
        assert {row["employee_id"] for row in permissions} == {own}
        assert {row["employee_id"] for row in extra_ot} == {own}
