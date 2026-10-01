"""Monthly payroll derived from the attendance ledger."""
from __future__ import annotations

import calendar
import html
import re
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING

from app.api.routes import current_user, users
from app.attendance.engine import calculate_month, local_day, lop_amount
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.config import settings
from app.db.mongo import ensure_index, get_db
from app.branches import branch_of, can_see, manages
from app.db.users import ADMIN_ROLE, MANAGER_ROLE, STAFF_ROLE, normalize_branch
from app.logging_config import get_logger
from app.reimbursements import approved_amount as approved_reimbursements

log = get_logger(__name__)

router = APIRouter(prefix="/payroll", tags=["payroll"])
RUNS = "payroll_runs"
OVERRIDE_LOGS = "payroll_override_logs"
DEFAULT_SHIFT_MINUTES = 8 * 60
PRESENT_STATUSES = {"P", "LT", "EE", "PP", "OD", "WFH"}


class EmployeePayrollPolicy(BaseModel):
    monthly_salary: float = Field(ge=0)


class PaymentStatus(BaseModel):
    paid: bool


class NetPayableOverride(BaseModel):
    #: The amount to pay instead of the computed one; null removes the override.
    amount: float | None = Field(default=None, ge=0)
    remarks: str = Field(min_length=3, max_length=1000)


def _manager(user: dict = Depends(current_user)) -> dict:
    if user.get("role") not in {ADMIN_ROLE, MANAGER_ROLE}:
        raise HTTPException(status_code=403, detail="Manager role required")
    return user


def _admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def _visible_employees(user: dict):
    """Staff see their own row; managers their own branch; admins everyone.

    Noorul sees Royapettah and Rafi sees Mount Road (see `app.branches`).
    """
    if user.get("role") == STAFF_ROLE:
        employee = users.get(user["id"])
        return [employee] if employee and employee.active else []
    if user.get("role") == ADMIN_ROLE:
        return users.list_employees(include_inactive=False)
    if user.get("role") == MANAGER_ROLE:
        manager = users.get(user["id"])
        return [
            employee for employee in users.list_employees(include_inactive=False)
            if can_see(manager, employee, users)
        ]
    raise HTTPException(status_code=403, detail="Payroll access required")


def _require_payroll_authority(user: dict, employee) -> None:
    """A manager changes payroll only for the staff of their own branch."""
    if user.get("role") == MANAGER_ROLE and not manages(users.get(user["id"]), employee, users):
        raise HTTPException(status_code=404, detail="Active employee not found")


def _branch_names(employees) -> list[str]:
    """Every branch in use, one entry per name however it was capitalised.

    Records written before branch normalisation may hold "chennai" alongside
    "Chennai"; both belong to one branch, so the filter offers it once, under
    the first spelling seen.
    """
    seen: dict[str, str] = {}
    for employee in employees:
        name = branch_of(employee)
        if name:
            seen.setdefault(name.casefold(), name)
    return sorted(seen.values())


def _period_days(year: int, month: int) -> list[date]:
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="month must be between 1 and 12")
    today = datetime.now(timezone.utc).date()
    if (year, month) > (today.year, today.month):
        return []
    last = calendar.monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last + 1)]


def _employee_row(employee, year: int, month: int, attendance_repo, attendance, runs) -> dict:
    """One employee's payroll for the month, reimbursements and override included."""
    policy = attendance_repo.employee_policy(employee.id)
    tracking_start = attendance_repo.attendance_start_date(employee.id)
    period_days = [
        day for day in _period_days(year, month)
        if tracking_start is not None and day >= tracking_start
    ]
    days = calculate_month(attendance.day(employee.id, day) for day in period_days)
    required_working_days = sum(day.get("status") not in {"WO", "H"} for day in days)
    unpaid_before_ot = sum(int(day.get("unpaid_minutes", 0)) for day in days)
    late_unpaid_minutes = sum(int(day.get("late_unpaid_minutes", 0)) for day in days)
    grace_minutes = sum(int(day.get("grace_minutes_applied", 0)) for day in days)
    paid_leave_days = sum(day.get("status") == "PL" for day in days)
    monthly_salary = float(policy.get("monthly_salary", 0) or 0)
    scheduled_minutes = max(1, required_working_days * DEFAULT_SHIFT_MINUTES)
    approved_ot_minutes = attendance_repo.approved_extra_ot_minutes(
        employee.id, date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    )
    # Approved OT is time worked beyond the normal hours, and it first
    # cancels the month's late / short time. Only late time: an absent day
    # or unpaid leave is a whole missing day, not lateness, and stays
    # deducted. Whatever OT is left after that is paid as Extra OT.
    ot_offset_minutes = min(approved_ot_minutes, late_unpaid_minutes)
    unpaid_minutes = unpaid_before_ot - ot_offset_minutes
    paid_ot_minutes = approved_ot_minutes - ot_offset_minutes
    deduction = lop_amount(unpaid_minutes, monthly_salary, scheduled_minutes)
    extra_ot_amount = round(paid_ot_minutes * monthly_salary / scheduled_minutes, 2)
    # Claims Yoosuf approved this month are paid with this month's salary.
    reimbursement_amount = approved_reimbursements(employee.id, year, month, db=runs.database)
    computed_payable = round(
        max(0, monthly_salary - deduction) + extra_ot_amount + reimbursement_amount, 2
    )
    run = runs.find_one({"employee_id": employee.id, "year": year, "month": month}) or {}
    override = run.get("net_payable_override")
    overridden = override is not None
    return {
        "employee_id": employee.id,
        "name": employee.name,
        "branch": branch_of(employee),
        "staff_code": employee.staff_code,
        "monthly_salary": monthly_salary,
        "deduction": deduction,
        "net_salary": round(max(0, monthly_salary - deduction), 2),
        "approved_ot_minutes": approved_ot_minutes,
        "ot_offset_minutes": ot_offset_minutes,
        "paid_ot_minutes": paid_ot_minutes,
        "extra_ot_amount": extra_ot_amount,
        "reimbursement_amount": reimbursement_amount,
        # What the rules produce. `total_payable` is what is actually paid,
        # which an administrator may have overridden (`override_net_payable`).
        "computed_payable": computed_payable,
        "total_payable": round(float(override), 2) if overridden else computed_payable,
        "net_payable_overridden": overridden,
        "override_remarks": run.get("override_remarks") if overridden else None,
        "overridden_by_name": run.get("overridden_by_name") if overridden else None,
        "unpaid_minutes": unpaid_minutes,
        "unpaid_minutes_before_ot": unpaid_before_ot,
        "late_unpaid_minutes": late_unpaid_minutes - ot_offset_minutes,
        "grace_minutes": grace_minutes,
        "paid_leave_days": paid_leave_days,
        "present_days": sum(day.get("status") in PRESENT_STATUSES for day in days),
        "absent_days": sum(day.get("status") in {"A", "UL"} for day in days),
        "calendar_days": len(days),
        "required_working_days": required_working_days,
        "daily_lop_rate": round(monthly_salary / required_working_days, 2) if required_working_days else 0,
        "weekly_off_pattern": (
            "alternate_friday"
            if policy.get("weekly_off_pattern") == "sunday_alternate_friday"
            else policy.get("weekly_off_pattern", "sunday")
        ),
        "alternate_friday_parity": int(policy.get("alternate_friday_parity", 0)),
        "status": "paid" if run.get("paid") else "draft",
    }


def _row_for(employee, year: int, month: int) -> dict:
    attendance_repo = AttendanceRepository()
    return _employee_row(
        employee, year, month, attendance_repo, AttendanceService(attendance_repo), get_db()[RUNS],
    )


@router.get("/{year}/{month}")
def payroll_month(year: int, month: int, branch: str | None = Query(default=None), user: dict = Depends(current_user)) -> dict:
    attendance_repo = AttendanceRepository()
    attendance = AttendanceService(attendance_repo)
    runs = get_db()[RUNS]
    rows = []
    wanted = normalize_branch(branch).casefold() if branch else None
    for employee in _visible_employees(user):
        if wanted is not None and branch_of(employee).casefold() != wanted:
            continue
        rows.append(_employee_row(employee, year, month, attendance_repo, attendance, runs))
    return {
        "year": year,
        "month": month,
        "basis": "required_working_days",
        "grace_allowance_minutes": 60,
        "paid_leave_allowance_days": 1,
        "items": rows,
        "branch": branch,
        "branches": _branch_names(_visible_employees(user)),
        "can_override": user.get("role") == ADMIN_ROLE,
        "payslips_available": payslip_available(year, month),
    }


@router.put("/employees/{employee_id}")
def update_employee_payroll(employee_id: str, payload: EmployeePayrollPolicy, _user: dict = Depends(_manager)) -> dict:
    employee = users.get(employee_id)
    if not employee or not employee.active or employee.role not in {"staff", "manager"}:
        raise HTTPException(status_code=404, detail="Active employee not found")
    _require_payroll_authority(_user, employee)
    # Payroll owns salary only. Weekly-off choice belongs to the employee's
    # Attendance settings and must never be overwritten by a salary update.
    policy = AttendanceRepository().set_employee_policy(
        employee_id,
        {"monthly_salary": payload.monthly_salary},
    )
    return {"status": "saved", "policy": policy}


@router.put("/{year}/{month}/employees/{employee_id}/status")
def update_payment_status(year: int, month: int, employee_id: str, payload: PaymentStatus, actor: dict = Depends(_manager)) -> dict:
    employee = users.get(employee_id)
    if month < 1 or month > 12 or not employee:
        raise HTTPException(status_code=404, detail="Employee or payroll period not found")
    _require_payroll_authority(actor, employee)
    get_db()[RUNS].update_one(
        {"employee_id": employee_id, "year": year, "month": month},
        {"$set": {"paid": payload.paid, "updated_at": datetime.now(timezone.utc), "updated_by": actor["id"]}},
        upsert=True,
    )
    return {"status": "paid" if payload.paid else "draft"}


# --------------------------------------------------------------------------- #
#  Net payable override (admins only, always with remarks, always logged)
# --------------------------------------------------------------------------- #
@router.put("/{year}/{month}/employees/{employee_id}/net-payable")
def override_net_payable(year: int, month: int, employee_id: str, payload: NetPayableOverride, admin: dict = Depends(_admin)) -> dict:
    """Set the net payable an admin decided, or clear it with `amount: null`.

    Every change is logged: the admin, their remarks, the amount the rules
    computed, and the amount payable before and after.
    """
    employee = users.get(employee_id)
    if month < 1 or month > 12 or not employee:
        raise HTTPException(status_code=404, detail="Employee or payroll period not found")
    before = _row_for(employee, year, month)
    now = datetime.now(timezone.utc)
    amount = round(payload.amount, 2) if payload.amount is not None else None
    remarks = payload.remarks.strip()
    get_db()[RUNS].update_one(
        {"employee_id": employee_id, "year": year, "month": month},
        {"$set": {
            "net_payable_override": amount,
            "override_remarks": remarks,
            "overridden_by": admin["id"],
            "overridden_by_name": admin.get("name", ""),
            "overridden_at": now,
            "updated_at": now,
        }},
        upsert=True,
    )
    get_db()[OVERRIDE_LOGS].insert_one({
        "_id": uuid.uuid4().hex,
        "employee_id": employee_id,
        "employee_name": employee.name,
        "year": year,
        "month": month,
        "computed_payable": before["computed_payable"],
        "previous_payable": before["total_payable"],
        "new_payable": amount if amount is not None else before["computed_payable"],
        "cleared": amount is None,
        "remarks": remarks,
        "actor_id": admin["id"],
        "actor_name": admin.get("name", ""),
        "actor_email": admin.get("email", ""),
        "created_at": now,
    })
    log.info("Net payable of %s for %04d-%02d set to %s by %s: %s",
             employee_id, year, month, amount, admin.get("email"), remarks)
    return {"status": "cleared" if amount is None else "overridden", "net_payable": amount}


@router.get("/{year}/{month}/net-payable-logs")
def net_payable_logs(year: int, month: int, user: dict = Depends(_manager)) -> dict:
    """Who overrode which net payable, from what to what, and why."""
    visible = {employee.id for employee in _visible_employees(user)}
    rows = []
    for row in get_db()[OVERRIDE_LOGS].find({"year": year, "month": month}).sort("created_at", DESCENDING):
        if row["employee_id"] not in visible:
            continue
        row["id"] = row.pop("_id")
        created = row.get("created_at")
        if isinstance(created, datetime):
            row["created_at"] = created.replace(tzinfo=created.tzinfo or timezone.utc).isoformat()
        rows.append(row)
    return {"items": rows}


# --------------------------------------------------------------------------- #
#  Payslips: generated from the last day of the month onwards
# --------------------------------------------------------------------------- #
def payslip_available(year: int, month: int, today: date | None = None) -> bool:
    """A month's payslip exists from its last day (office time) onwards."""
    if month < 1 or month > 12:
        return False
    today = today or local_day(datetime.now(timezone.utc))
    return today >= date(year, month, calendar.monthrange(year, month)[1])


def _money(value: float) -> str:
    return f"Rs. {value:,.2f}"


def _payslip_html(row: dict, employee, year: int, month: int) -> str:
    period = date(year, month, 1).strftime("%B %Y")
    esc = html.escape
    earnings = [
        ("Monthly salary", row["monthly_salary"]),
        ("Extra OT", row["extra_ot_amount"]),
        ("Reimbursements", row["reimbursement_amount"]),
    ]
    deductions = [("Loss of pay", row["deduction"])]
    lines = "".join(
        f"<tr><td>{esc(label)}</td><td class='r'>{_money(value)}</td></tr>" for label, value in earnings
    )
    cuts = "".join(
        f"<tr><td>{esc(label)}</td><td class='r'>{_money(value)}</td></tr>" for label, value in deductions
    )
    adjustment = ""
    if row["net_payable_overridden"]:
        adjustment = (
            f"<p class='note'>Net payable adjusted from {_money(row['computed_payable'])} by "
            f"{esc(row.get('overridden_by_name') or 'an administrator')}: "
            f"{esc(row.get('override_remarks') or '')}</p>"
        )
    return f"""
<style>
  body {{ font-family: sans-serif; font-size: 10pt; color: #1f2937; }}
  h1 {{ font-size: 16pt; margin: 0; }}
  h2 {{ font-size: 11pt; margin: 2pt 0 12pt; color: #4b5563; font-weight: normal; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 10pt; }}
  td, th {{ border: 1px solid #d1d5db; padding: 4pt 6pt; text-align: left; }}
  th {{ background: #f3f4f6; }}
  .r {{ text-align: right; }}
  .total td {{ font-weight: bold; background: #eef2ff; }}
  .note {{ font-size: 9pt; color: #4b5563; }}
</style>
<h1>{esc(settings.payslip_company_name)}</h1>
<h2>Payslip for {period}</h2>
<table>
  <tr><th>Employee</th><td>{esc(employee.name)}</td><th>Staff code</th><td>{esc(row.get('staff_code') or '-')}</td></tr>
  <tr><th>Branch</th><td>{esc(row.get('branch') or '-')}</td><th>Status</th><td>{'Paid' if row['status'] == 'paid' else 'Draft'}</td></tr>
  <tr><th>Working days</th><td>{row['required_working_days']}</td><th>Days present</th><td>{row['present_days']}</td></tr>
  <tr><th>Paid leave</th><td>{row['paid_leave_days']}</td><th>Absent / unpaid leave</th><td>{row['absent_days']}</td></tr>
</table>
<table>
  <tr><th>Earnings</th><th class='r'>Amount</th></tr>
  {lines}
</table>
<table>
  <tr><th>Deductions</th><th class='r'>Amount</th></tr>
  {cuts}
</table>
<table>
  <tr class='total'><td>Net payable</td><td class='r'>{_money(row['total_payable'])}</td></tr>
</table>
{adjustment}
<p class='note'>This is a system-generated payslip.</p>
"""


def render_payslip_pdf(row: dict, employee, year: int, month: int) -> bytes:
    import fitz  # PyMuPDF

    document = fitz.open()
    page = document.new_page(width=595, height=842)  # A4 in points
    page.insert_htmlbox(fitz.Rect(40, 40, 555, 802), _payslip_html(row, employee, year, month))
    data = document.tobytes()
    document.close()
    return data


@router.get("/{year}/{month}/employees/{employee_id}/payslip")
def download_payslip(year: int, month: int, employee_id: str, user: dict = Depends(current_user)) -> Response:
    """The month's payslip as a PDF, for the employee, their manager or an admin."""
    employee = next((e for e in _visible_employees(user) if e.id == employee_id), None)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not payslip_available(year, month):
        raise HTTPException(status_code=409, detail="The payslip is generated on the last day of the month")
    row = _row_for(employee, year, month)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", employee.name).strip("-") or employee_id
    filename = f"payslip-{slug}-{year}-{month:02d}.pdf"
    return Response(
        content=render_payslip_pdf(row, employee, year, month),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def ensure_payroll_indexes() -> None:
    ensure_index(
        get_db()[RUNS],
        [("employee_id", ASCENDING), ("year", ASCENDING), ("month", ASCENDING)],
        "payroll_employee_period",
        unique=True,
    )
    ensure_index(
        get_db()[OVERRIDE_LOGS],
        [("year", ASCENDING), ("month", ASCENDING), ("created_at", DESCENDING)],
        "payroll_override_period",
    )
