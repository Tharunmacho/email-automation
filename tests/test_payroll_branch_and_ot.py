"""Payroll: branch assignment, branch filtering, and what extra OT is paid.

Two rules are load-bearing here.

Branch: payroll is viewed one branch at a time, and the filter is built from
what is actually assigned to employees. An employee with no branch stays valid
and simply cannot be narrowed to.

Extra OT: only *approved* minutes are paid. A pending request is not a promise
and a rejected one is not a debt, so neither may move a salary.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.attendance.engine import IST
from app.attendance.repository import AttendanceRepository

YEAR, MONTH = 2026, 8
LAST_DAY = calendar.monthrange(YEAR, MONTH)[1]

ADMIN = {"id": "admin-1", "email": "admin@adira.test", "name": "Admin", "role": "admin"}
STAFF = {"id": "staff-1", "email": "ravi@adira.test", "name": "Ravi", "role": "staff"}


class FakeEmployee:
    def __init__(self, user_id, name, branch, role="staff", active=True, email=""):
        self.id, self.name, self.branch = user_id, name, branch
        self.role, self.active = role, active
        self.staff_code = f"ADR-{user_id[-1]}"
        self.phone = ""
        # Desk membership is by email (see `app.assignment.balancer`), and
        # payroll falls back to the desk when no branch is set explicitly.
        self.email = email or f"{name.lower()}@adira.test"


ROSTER = [
    FakeEmployee("staff-1", "Ravi", "Mount Road"),
    FakeEmployee("staff-2", "Priya", "Mount Road"),
    FakeEmployee("staff-3", "Anand", "Singapore Desk"),
    # No explicit branch, and not on the Singapore/Malaysia desk: falls to
    # Mount Road rather than to nothing.
    FakeEmployee("staff-4", "Meera", ""),
    # Deliberately a different spelling of an existing branch.
    FakeEmployee("staff-5", "Karthik", "mount  road"),
    # No explicit branch, and on the Singapore/Malaysia desk by email.
    FakeEmployee("staff-6", "Sreya", "", email="sreya.adira@gmail.com"),
]


class FakeUsers:
    def list_employees(self, include_inactive=False):
        return [e for e in ROSTER if include_inactive or e.active]

    def get(self, user_id):
        return next((e for e in ROSTER if e.id == user_id), None)


@pytest.fixture()
def api():
    database = mongomock.MongoClient()["payroll-test"]
    attendance = AttendanceRepository(database)
    for employee in ROSTER:
        attendance.set_employee_policy(employee.id, {"monthly_salary": 30000})
        # Give everyone a tracked day so the period is non-empty.
        attendance.set_calendar_day({
            "employee_id": employee.id,
            "attendance_date": date(YEAR, MONTH, 1).isoformat(),
            "status": "H",
            "reason": "Tracking start",
        })

    app.dependency_overrides[current_user] = lambda: ADMIN
    with patch("app.payroll.users", FakeUsers()), \
         patch("app.payroll.get_db", return_value=database), \
         patch("app.payroll.AttendanceRepository", return_value=attendance):
        client = TestClient(app)
        client.attendance = attendance
        yield client
    app.dependency_overrides.clear()


def payroll(client, branch=None):
    # Encoded, because a branch name is free text an administrator typed.
    query = f"?branch={quote(branch)}" if branch else ""
    response = client.get(f"/payroll/{YEAR}/{MONTH}{query}")
    assert response.status_code == 200, response.text
    return response.json()


def rows_by_name(body):
    return {row["name"]: row for row in body["items"]}


# --------------------------------------------------------------------------- #
#  Branch
# --------------------------------------------------------------------------- #
def test_the_branch_filter_offers_every_branch_in_use(api):
    body = payroll(api)
    assert body["branches"] == ["Mount Road", "Royapettah", "Singapore Desk"]


def test_an_employee_without_a_branch_falls_to_their_desk(api):
    """Nobody is "Unassigned": the desk they already work is the answer.

    The Singapore and Malaysia desk works from Royapettah and every other
    destination from Mount Road, so payroll groups on the same line.
    """
    rows = rows_by_name(payroll(api))
    assert rows["Sreya"]["branch"] == "Royapettah"
    assert rows["Meera"]["branch"] == "Mount Road"
    # And they are still paid, which is the part that must never regress.
    assert rows["Meera"]["monthly_salary"] == 30000


def test_an_explicit_branch_overrides_the_desk(api):
    """An administrator who typed a branch meant it."""
    rows = rows_by_name(payroll(api))
    # Ravi is on no special desk but was given a branch by hand.
    assert rows["Ravi"]["branch"] == "Mount Road"
    # Anand likewise, rather than being forced to his desk's default.
    assert rows["Anand"]["branch"] == "Singapore Desk"


def test_the_two_desks_can_be_filtered_apart(api):
    """The split the payroll screen is for: one desk at a time."""
    sg = rows_by_name(payroll(api, "Royapettah"))
    other = rows_by_name(payroll(api, "Mount Road"))
    assert list(sg) == ["Sreya"]
    assert "Meera" in other
    # Nobody appears in both.
    assert not set(sg) & set(other)


def test_filtering_by_branch_returns_only_that_branch(api):
    body = payroll(api, "Mount Road")
    assert body["branch"] == "Mount Road"
    assert sorted(rows_by_name(body)) == ["Karthik", "Meera", "Priya", "Ravi"]


def test_spelling_variants_are_one_branch(api):
    """"mount  road" and "Mount Road" are the same place, and must filter as one."""
    body = payroll(api, "Mount Road")
    assert "Karthik" in rows_by_name(body)
    # And the filter offers it once, not twice.
    assert payroll(api)["branches"].count("Mount Road") == 1


def test_the_branch_filter_is_case_insensitive(api):
    assert sorted(rows_by_name(payroll(api, "mount road"))) == ["Karthik", "Meera", "Priya", "Ravi"]


def test_every_row_reports_its_branch(api):
    rows = rows_by_name(payroll(api))
    assert rows["Ravi"]["branch"] == "Mount Road"
    assert rows["Anand"]["branch"] == "Singapore Desk"
    # Normalised on the way out, so the console is not asked to tidy it up.
    assert rows["Karthik"]["branch"] == "mount road"


def test_an_unknown_branch_matches_nobody_rather_than_everybody(api):
    body = payroll(api, "Nowhere")
    assert body["items"] == []


# --------------------------------------------------------------------------- #
#  Extra OT
# --------------------------------------------------------------------------- #
def add_ot(client, status, minutes=60, day=5):
    request = client.attendance.create_extra_ot({
        "employee_id": "staff-1",
        "attendance_date": date(YEAR, MONTH, day).isoformat(),
        "requested_minutes": minutes,
        "reason": "Shipment deadline",
    })
    if status != "pending":
        client.attendance.decide_extra_ot(request["id"], {
            "approved": status == "approved",
            "reason": "Reviewed",
            "decided_by": "admin-1",
            "decided_at": datetime.now(timezone.utc),
        })
    return request


def test_pending_extra_ot_does_not_move_payroll(api):
    before = rows_by_name(payroll(api))["Ravi"]
    add_ot(api, "pending")
    after = rows_by_name(payroll(api))["Ravi"]

    assert after["approved_ot_minutes"] == 0
    assert after["extra_ot_amount"] == 0
    assert after["total_payable"] == before["total_payable"]


def test_rejected_extra_ot_does_not_move_payroll(api):
    before = rows_by_name(payroll(api))["Ravi"]
    add_ot(api, "rejected")
    after = rows_by_name(payroll(api))["Ravi"]

    assert after["approved_ot_minutes"] == 0
    assert after["extra_ot_amount"] == 0
    assert after["total_payable"] == before["total_payable"]


def test_approved_extra_ot_is_paid(api):
    before = rows_by_name(payroll(api))["Ravi"]
    add_ot(api, "approved", minutes=120)
    after = rows_by_name(payroll(api))["Ravi"]

    assert after["approved_ot_minutes"] == 120
    assert after["extra_ot_amount"] > 0
    assert after["total_payable"] == pytest.approx(
        before["total_payable"] + after["extra_ot_amount"]
    )


def test_only_the_approved_share_of_a_mixed_month_is_paid(api):
    add_ot(api, "approved", minutes=60, day=5)
    add_ot(api, "pending", minutes=90, day=6)
    add_ot(api, "rejected", minutes=30, day=7)

    assert rows_by_name(payroll(api))["Ravi"]["approved_ot_minutes"] == 60


def test_approved_ot_outside_the_month_is_not_paid_into_it(api):
    """The period is the month, not the ledger."""
    request = api.attendance.create_extra_ot({
        "employee_id": "staff-1",
        "attendance_date": date(YEAR, MONTH + 1, 3).isoformat(),
        "requested_minutes": 240,
        "reason": "Next month",
    })
    api.attendance.decide_extra_ot(request["id"], {
        "approved": True, "reason": "Reviewed",
        "decided_by": "admin-1", "decided_at": datetime.now(timezone.utc),
    })
    assert rows_by_name(payroll(api))["Ravi"]["approved_ot_minutes"] == 0


def test_extra_ot_follows_the_employee_it_belongs_to(api):
    add_ot(api, "approved", minutes=90)
    rows = rows_by_name(payroll(api))
    assert rows["Ravi"]["approved_ot_minutes"] == 90
    assert rows["Priya"]["approved_ot_minutes"] == 0


def punch_late(client, day, late_minutes):
    """A day worked from 10:00 + `late_minutes` to 19:00 IST: late, not absent."""
    shift_start = datetime(YEAR, MONTH, day, 10, 0, tzinfo=IST)
    for action, occurred in (
        ("check_in", shift_start + timedelta(minutes=late_minutes)),
        ("check_out", datetime(YEAR, MONTH, day, 19, 0, tzinfo=IST)),
    ):
        occurred = occurred.astimezone(timezone.utc)
        client.attendance.append_event({
            "employee_id": "staff-1",
            "action": action,
            "occurred_at": occurred,
            "local_date": date(YEAR, MONTH, day).isoformat(),
            "source": "web",
            "idempotency_key": f"{action}-{day}",
            "evidence": {},
        })


def test_approved_ot_first_cancels_late_time(api):
    """OT above normal hours is taken off the late hours before it is paid."""
    # Two days 3h late: each is 2h short of the 8h duty (the break covers the
    # rest), so 240 short, of which the monthly 60-minute grace absorbs 60.
    punch_late(api, 3, 180)
    punch_late(api, 4, 180)
    before = rows_by_name(payroll(api))["Ravi"]
    assert before["late_unpaid_minutes"] == 180

    add_ot(api, "approved", minutes=200)
    after = rows_by_name(payroll(api))["Ravi"]

    assert after["ot_offset_minutes"] == 180
    assert after["late_unpaid_minutes"] == 0
    assert after["unpaid_minutes"] == before["unpaid_minutes"] - 180
    # Only the 20 minutes left over are paid as Extra OT.
    assert after["paid_ot_minutes"] == 20
    assert after["deduction"] < before["deduction"]


def test_ot_smaller_than_the_late_time_only_reduces_it(api):
    punch_late(api, 3, 180)
    punch_late(api, 4, 180)
    add_ot(api, "approved", minutes=45)
    row = rows_by_name(payroll(api))["Ravi"]

    assert row["ot_offset_minutes"] == 45
    assert row["late_unpaid_minutes"] == 135
    assert row["paid_ot_minutes"] == 0
    assert row["extra_ot_amount"] == 0


def test_ot_never_cancels_an_absent_day(api):
    """Absence is a missing day, not lateness; OT is paid instead."""
    add_ot(api, "approved", minutes=120)
    row = rows_by_name(payroll(api))["Ravi"]
    assert row["ot_offset_minutes"] == 0
    assert row["paid_ot_minutes"] == 120


# --------------------------------------------------------------------------- #
#  Scope
# --------------------------------------------------------------------------- #
def test_a_staff_member_sees_only_their_own_row(api):
    app.dependency_overrides[current_user] = lambda: STAFF
    try:
        body = payroll(api)
        assert [row["name"] for row in body["items"]] == ["Ravi"]
    finally:
        app.dependency_overrides[current_user] = lambda: ADMIN
