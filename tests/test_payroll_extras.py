"""Reimbursements, the admin net-payable override, payslips, branches and
employees without CRM access."""
from __future__ import annotations

import io
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.attendance.engine import local_day
from app.attendance.repository import AttendanceRepository
from app.branches import BRANCHES, add_branch, all_branches, remove_branch
from app.db.identity_records import passport_name
from app.db.users import UserRepository
from app.payroll import payslip_available

YEAR, MONTH = 2026, 8

YOOSUF = {"id": "admin-y", "email": "yoosuf@adira.test", "name": "Yoosuf", "role": "admin"}
OTHER_ADMIN = {"id": "admin-2", "email": "other@adira.test", "name": "Other", "role": "admin"}
STAFF = {"id": "staff-1", "email": "ravi@adira.test", "name": "Ravi", "role": "staff"}


class FakeEmployee:
    def __init__(self, user_id, name, role="staff"):
        self.id, self.name, self.role = user_id, name, role
        self.branch, self.active, self.staff_code = "Mount Road", True, "ADR-1"
        self.email, self.phone = f"{name.lower()}@adira.test", ""


RAVI = FakeEmployee("staff-1", "Ravi")


class FakeUsers:
    def list_employees(self, include_inactive=False):
        return [RAVI]

    def get(self, user_id):
        return RAVI if user_id == RAVI.id else None

    def list_admins(self, include_inactive=False):
        return [SimpleNamespace(**YOOSUF), SimpleNamespace(**OTHER_ADMIN)]


class Storage:
    def __init__(self):
        self.files = {}

    def save(self, key, data, content_type=None):
        self.files[key] = data
        return key

    def load(self, key):
        return self.files[key]


@pytest.fixture()
def api():
    database = mongomock.MongoClient()["payroll-extras"]
    attendance = AttendanceRepository(database)
    attendance.set_employee_policy(RAVI.id, {"monthly_salary": 30000})
    attendance.set_calendar_day({
        "employee_id": RAVI.id, "attendance_date": date(YEAR, MONTH, 1).isoformat(),
        "status": "H", "reason": "Tracking start",
    })
    storage = Storage()
    fake_users = FakeUsers()
    with patch("app.payroll.users", fake_users), \
         patch("app.reimbursements.users", fake_users), \
         patch("app.payroll.get_db", return_value=database), \
         patch("app.reimbursements.get_db", return_value=database), \
         patch("app.reimbursements.get_storage_backend", return_value=storage), \
         patch("app.reimbursements.NotificationRepository"), \
         patch("app.payroll.AttendanceRepository", return_value=attendance):
        client = TestClient(app)
        client.db, client.storage = database, storage
        yield client
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[current_user] = lambda: user


def row(client):
    response = client.get(f"/payroll/{YEAR}/{MONTH}")
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def test_staff_claim_with_attachment_is_paid_once_yoosuf_approves(api):
    as_user(STAFF)
    response = api.post(
        "/payroll/reimbursements",
        data={"amount": "750", "description": "Courier", "expense_date": "2026-08-03"},
        files={"attachment": ("bill.jpg", io.BytesIO(b"jpeg-bytes"), "image/jpeg")},
    )
    assert response.status_code == 201, response.text
    claim = response.json()["reimbursement"]
    assert claim["status"] == "pending" and claim["attachment"]["filename"] == "bill.jpg"
    assert api.get(f"/payroll/reimbursements/{claim['id']}/attachment").content == b"jpeg-bytes"

    # Neither the claimant nor another admin may decide it.
    assert api.post(f"/payroll/reimbursements/{claim['id']}/decision", json={"approved": True}).status_code == 403
    as_user(OTHER_ADMIN)
    assert api.post(f"/payroll/reimbursements/{claim['id']}/decision", json={"approved": True}).status_code == 403

    as_user(YOOSUF)
    with patch("app.reimbursements.local_day", return_value=date(YEAR, MONTH, 20)):
        decided = api.post(f"/payroll/reimbursements/{claim['id']}/decision", json={"approved": True, "note": "ok"})
    assert decided.status_code == 200, decided.text
    paid = row(api)
    assert paid["reimbursement_amount"] == 750
    assert paid["total_payable"] == paid["computed_payable"] == round(paid["net_salary"] + paid["extra_ot_amount"] + 750, 2)


def test_a_claim_without_attachment_and_a_rejected_claim_pays_nothing(api):
    as_user(STAFF)
    claim = api.post(
        "/payroll/reimbursements",
        data={"amount": "100", "description": "Tea", "expense_date": "2026-08-03"},
    ).json()["reimbursement"]
    assert claim["attachment"] is None
    as_user(YOOSUF)
    api.post(f"/payroll/reimbursements/{claim['id']}/decision", json={"approved": False})
    assert row(api)["reimbursement_amount"] == 0


def test_admin_override_needs_remarks_and_is_logged(api):
    as_user(YOOSUF)
    computed = row(api)["computed_payable"]
    url = f"/payroll/{YEAR}/{MONTH}/employees/{RAVI.id}/net-payable"
    assert api.put(url, json={"amount": 1000}).status_code == 422

    assert api.put(url, json={"amount": 12345.5, "remarks": "Advance recovered"}).status_code == 200
    overridden = row(api)
    assert overridden["total_payable"] == 12345.5 and overridden["net_payable_overridden"]
    assert overridden["computed_payable"] == computed
    assert overridden["override_remarks"] == "Advance recovered"

    assert api.put(url, json={"amount": None, "remarks": "Back to calculated"}).status_code == 200
    assert row(api)["total_payable"] == computed

    logs = api.get(f"/payroll/{YEAR}/{MONTH}/net-payable-logs").json()["items"]
    assert [entry["remarks"] for entry in logs] == ["Back to calculated", "Advance recovered"]
    assert logs[1]["previous_payable"] == computed and logs[1]["new_payable"] == 12345.5
    assert logs[0]["cleared"] and logs[0]["actor_name"] == "Yoosuf"


def test_staff_cannot_override_net_payable(api):
    as_user(STAFF)
    response = api.put(
        f"/payroll/{YEAR}/{MONTH}/employees/{RAVI.id}/net-payable",
        json={"amount": 99999, "remarks": "please"},
    )
    assert response.status_code == 403


def test_payslip_is_generated_from_the_last_day_of_the_month(api):
    assert not payslip_available(YEAR, MONTH, today=date(YEAR, MONTH, 30))
    assert payslip_available(YEAR, MONTH, today=date(YEAR, MONTH, 31))
    url = f"/payroll/{YEAR}/{MONTH}/employees/{RAVI.id}"
    as_user(STAFF)
    # Not issued until a manager or admin records the payment date.
    assert api.get(f"{url}/payslip").status_code == 409
    assert api.put(f"{url}/payslip-details", json={"payment_date": "2026-09-04"}).status_code == 403
    assert api.post(f"/payroll/{YEAR}/{MONTH}/incentives", json={"employee_id": RAVI.id, "amount": 9}).status_code == 403

    as_user(YOOSUF)
    before = row(api)["computed_payable"]
    incentives = f"/payroll/{YEAR}/{MONTH}/incentives"
    for amount, remarks in ((500, "Interview incentive"), (250, "Target"), (100, "Removed later")):
        added = api.post(incentives, json={"employee_id": RAVI.id, "amount": amount, "remarks": remarks})
        assert added.status_code == 201, added.text
    assert api.delete(f"{incentives}/{added.json()['incentive']['id']}").status_code == 200
    listed = api.get(incentives).json()["items"]
    assert [(item["amount"], item["remarks"]) for item in listed] == [(500, "Interview incentive"), (250, "Target")]
    saved = api.put(f"{url}/payslip-details", json={"payment_date": "2026-09-04"})
    assert saved.status_code == 200, saved.text
    issued = row(api)
    assert issued["incentive_amount"] == 750 and issued["payment_date"] == "2026-09-04"
    assert issued["computed_payable"] == issued["total_payable"] == before + 750

    as_user(STAFF)
    response = api.get(f"{url}/payslip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    import fitz
    text = fitz.open(stream=response.content, filetype="pdf")[0].get_text()
    assert "Pay Slip for August 2026" in text and "Interview incentive" in text
    assert "04 Sep 2026" in text and "SalaryBox" not in text


def test_branches_can_be_added_and_removed_but_not_the_originals():
    db = mongomock.MongoClient()["branches"]
    assert all_branches(db) == list(BRANCHES)
    assert add_branch("  Anna   Nagar ", "admin", db) == "Anna Nagar"
    assert all_branches(db) == [*BRANCHES, "Anna Nagar"]
    with pytest.raises(ValueError):
        add_branch("anna nagar", "admin", db)
    with pytest.raises(ValueError):
        remove_branch("Royapettah", db)
    assert remove_branch("ANNA NAGAR", db)
    assert all_branches(db) == list(BRANCHES)


def test_an_employee_without_crm_access_cannot_sign_in_or_receive_candidates():
    users = UserRepository(collection=mongomock.MongoClient()["users"]["users"])
    offline = users.create("", "", name="Office Helper", role="staff", crm_access=False)
    online = users.create("ravi@adira.test", "secret1", name="Ravi", role="staff")
    assert offline.email.endswith("@staff.local") and not offline.crm_access
    assert [user.id for user in users.list_assignable_staff()] == [online.id]
    assert {user.id for user in users.list_employees()} == {offline.id, online.id}
    users.update_user(online.id, crm_access=False)
    assert users.authenticate("ravi@adira.test", "secret1") is None


def test_passport_names_are_given_name_then_surname():
    assert passport_name({"given_names": "MOHAMED NASIR", "surname": "SHAIK"}) == "Mohamed Nasir Shaik"
    assert passport_name({"given_names": "PRIYA", "surname": ""}) == "Priya"
    assert passport_name({"given_names": "X", "surname": "Y", "check_digits_valid": False}) == ""


def test_local_day_is_used_for_payroll_month():
    # Sanity: the approval month comes from office time, not UTC.
    from datetime import datetime, timezone
    assert local_day(datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)) == date(2026, 9, 1)


def test_payroll_only_staff_get_full_salary_with_no_attendance(api):
    offline = FakeEmployee("staff-9", "Helper")
    offline.crm_access = False
    api.db["attendance_calendar"].insert_one({
        "_id": "absent", "employee_id": "staff-9", "attendance_date": date(YEAR, MONTH, 4).isoformat(),
        "status": "UL", "reason": "would be deducted if tracked",
    })
    from app.attendance.repository import AttendanceRepository as Repo
    Repo(api.db).set_employee_policy("staff-9", {"monthly_salary": 20000})
    with patch.object(FakeUsers, "list_employees", lambda self, include_inactive=False: [offline]):
        as_user(YOOSUF)
        paid = row(api)
    assert paid["attendance_tracked"] is False
    assert paid["deduction"] == 0 and paid["total_payable"] == 20000
    assert paid["calendar_days"] == 0


def test_payroll_only_staff_are_not_on_attendance():
    from app.attendance import api as attendance_api
    from app.db.users import on_attendance

    offline = SimpleNamespace(id="x", active=True, role="staff", crm_access=False)
    assert not on_attendance(offline) and on_attendance(SimpleNamespace())
    with patch.object(attendance_api.users, "get", return_value=offline):
        with pytest.raises(Exception) as refused:
            attendance_api._employee_id({"id": "admin", "role": "admin"}, "x")
    assert refused.value.status_code == 404
