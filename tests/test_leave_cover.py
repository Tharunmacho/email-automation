"""Leave cover: a colleague works the leave-taker's queue for the day.

Completed work stays with the cover; whatever is still pending when the day
ends goes back to the staff member who was on leave.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import mongomock
import pytest

from app.api.routes import app as _app  # noqa: F401  (import order, see other attendance tests)
from fastapi import HTTPException

from app.attendance.api import (
    decide_permission,
    nominate_cover,
    request_permission,
    respond_to_cover,
)
from app.attendance.cover import run_cover_sweep
from app.attendance.models import CoverNomination, CoverResponse, PermissionDecision, PermissionRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.db.repository import CandidateRepository
from app.db.users import FINANCE_MANAGER_ROLE, MANAGER_ROLE, STAFF_ROLE, User

LEAVE_DAY = date(2026, 10, 5)
DURING = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)   # 11:30 IST on the day
AFTER = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)    # next day

ASHA = {"id": "asha", "role": STAFF_ROLE}
BALA = {"id": "bala", "role": STAFF_ROLE}
MANAGER = {"id": "mgr", "role": MANAGER_ROLE}
FINANCE = {"id": "fin", "role": FINANCE_MANAGER_ROLE}


class FakeUsers:
    def __init__(self):
        self.members = {
            "asha": User(id="asha", email="asha@example.com", name="Asha", role=STAFF_ROLE, branch="Mount Road"),
            "bala": User(id="bala", email="bala@example.com", name="Bala", role=STAFF_ROLE, branch="Mount Road"),
            "ravi": User(id="ravi", email="ravi@example.com", name="Ravi", role=STAFF_ROLE, branch="Royapettah"),
            "mgr": User(id="mgr", email="mgr@example.com", name="Mgr", role=MANAGER_ROLE, branch="Mount Road"),
            "fin": User(id="fin", email="fin@example.com", name="Fin", role=FINANCE_MANAGER_ROLE, branch="Royapettah"),
        }

    def get(self, user_id):
        return self.members.get(user_id)

    def list_employees(self, include_inactive=False):
        return [m for m in self.members.values() if m.id != "fin"]

    def list_staff(self, include_inactive=False):
        return [m for m in self.members.values() if m.role == STAFF_ROLE]

    def list_managers(self):
        return [self.members["mgr"], self.members["fin"]]

    def list_finance_managers(self):
        return [self.members["fin"]]

    def list_admins(self):
        return []


@pytest.fixture()
def env():
    db = mongomock.MongoClient()["leave-cover"]
    attendance = AttendanceRepository(db)
    candidates = CandidateRepository(collection=db["candidates"])
    fake_users = FakeUsers()
    for index in range(4):
        db["candidates"].insert_one({
            "_id": f"c{index}", "assigned_staff_id": "asha", "assigned_staff_name": "Asha",
            "evaluation_status": "pending", "manually_assigned": False,
        })
    # Already judged before the leave: never lent out.
    db["candidates"].insert_one({
        "_id": "done", "assigned_staff_id": "asha", "assigned_staff_name": "Asha",
        "evaluation_status": "shortlisted",
    })
    with patch("app.attendance.api.users", fake_users), \
         patch("app.attendance.api.service", return_value=AttendanceService(attendance)), \
         patch("app.attendance.api.AttendanceRepository", return_value=attendance), \
         patch("app.attendance.api.NotificationRepository"), \
         patch("app.attendance.api._try_start_covers"), \
         patch("app.attendance.cover._notify"):
        yield {"db": db, "attendance": attendance, "candidates": candidates, "users": fake_users}


def _sweep(env, now):
    return run_cover_sweep(attendance=env["attendance"], candidates=env["candidates"], users=env["users"], now=now)


def _request_leave(cover="bala"):
    return request_permission(
        PermissionRequest(attendance_date=LEAVE_DAY, kind="paid_leave", reason="Family function",
                          cover_employee_id=cover),
        user=ASHA,
    )["permission"]


def test_the_whole_cover_day(env):
    leave = _request_leave()
    assert leave["cover_status"] == "requested"
    assert leave["cover_employee_name"] == "Bala"

    respond_to_cover(leave["id"], CoverResponse(accepted=True), user=BALA)
    decide_permission(leave["id"], PermissionDecision(approved=True, reason="ok"), admin=MANAGER)
    decide_permission(leave["id"], PermissionDecision(approved=True, reason="ok"), admin=FINANCE)

    started = _sweep(env, DURING)["started"]
    assert started == [{"permission_id": leave["id"], "handed_over": 4}]
    coll = env["db"]["candidates"]
    assert coll.count_documents({"assigned_staff_id": "bala"}) == 4
    assert coll.find_one({"_id": "done"})["assigned_staff_id"] == "asha"

    # Bala finishes half of it.
    coll.update_many({"_id": {"$in": ["c0", "c1"]}}, {"$set": {"evaluation_status": "shortlisted"}})

    # A second sweep on the same day changes nothing.
    assert _sweep(env, DURING) == {"settled": [], "started": []}

    settled = _sweep(env, AFTER)["settled"]
    assert settled == [{"permission_id": leave["id"], "completed": 2, "returned": 2, "released": 0}]
    assert {row["_id"] for row in coll.find({"assigned_staff_id": "bala"})} == {"c0", "c1"}
    assert {row["_id"] for row in coll.find({"assigned_staff_id": "asha"})} == {"c2", "c3", "done"}
    assert coll.count_documents({"leave_cover": {"$exists": True}}) == 0
    # The pin placed for the day is lifted again.
    assert coll.find_one({"_id": "c2"})["manually_assigned"] is False

    record = env["attendance"].permission(leave["id"])
    assert record["cover_handover"]["status"] == "settled"


def test_everything_completed_means_nothing_comes_back(env):
    leave = _request_leave()
    respond_to_cover(leave["id"], CoverResponse(accepted=True), user=BALA)
    decide_permission(leave["id"], PermissionDecision(approved=True, reason="ok"), admin=MANAGER)
    decide_permission(leave["id"], PermissionDecision(approved=True, reason="ok"), admin=FINANCE)
    _sweep(env, DURING)
    env["db"]["candidates"].update_many({"assigned_staff_id": "bala"}, {"$set": {"evaluation_status": "rejected"}})

    settled = _sweep(env, AFTER)["settled"][0]
    assert settled["completed"] == 4 and settled["returned"] == 0


def test_the_manager_cannot_decide_before_the_cover_accepts(env):
    leave = _request_leave()
    assert leave["status"] == "awaiting_cover"
    with pytest.raises(HTTPException) as early:
        decide_permission(leave["id"], PermissionDecision(approved=True, reason="ok"), admin=MANAGER)
    assert early.value.status_code == 409

    respond_to_cover(leave["id"], CoverResponse(accepted=False, note="Busy"), user=BALA)
    declined = env["attendance"].permission(leave["id"])
    assert declined["cover_status"] == "declined"
    assert declined["status"] == "awaiting_cover"  # still not with the manager
    assert _sweep(env, DURING)["started"] == []


def test_acceptance_moves_the_leave_to_the_manager(env):
    leave = _request_leave()
    respond_to_cover(leave["id"], CoverResponse(accepted=True), user=BALA)
    assert env["attendance"].permission(leave["id"])["status"] == "pending"
    assert _sweep(env, DURING)["started"] == []  # accepted but not yet approved


def test_leave_without_a_cover_is_refused(env):
    with pytest.raises(HTTPException) as missing:
        request_permission(
            PermissionRequest(attendance_date=LEAVE_DAY, kind="unpaid_leave", reason="Trip"),
            user=ASHA,
        )
    assert missing.value.status_code == 422


def test_a_declined_cover_can_be_replaced(env):
    env["users"].members["cara"] = User(id="cara", email="cara@example.com", name="Cara",
                                        role=STAFF_ROLE, branch="Mount Road")
    leave = _request_leave()
    respond_to_cover(leave["id"], CoverResponse(accepted=False), user=BALA)
    renamed = nominate_cover(leave["id"], CoverNomination(cover_employee_id="cara"), user=ASHA)
    assert renamed["permission"]["cover_employee_id"] == "cara"
    assert renamed["permission"]["cover_status"] == "requested"


def test_only_the_named_colleague_can_answer(env):
    leave = _request_leave()
    with pytest.raises(HTTPException) as refused:
        respond_to_cover(leave["id"], CoverResponse(accepted=True), user={"id": "ravi", "role": STAFF_ROLE})
    assert refused.value.status_code == 409


def test_cover_must_be_a_colleague_from_the_same_branch(env):
    with pytest.raises(HTTPException) as other_branch:
        _request_leave(cover="ravi")
    assert other_branch.value.status_code == 422
    with pytest.raises(HTTPException) as self_cover:
        _request_leave(cover="asha")
    assert self_cover.value.status_code == 422
