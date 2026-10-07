"""Monthly payroll derived from the attendance ledger."""
from __future__ import annotations

import calendar
import html
import re
import uuid
from pathlib import Path
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING

from app.api.routes import current_user, users
from app.attendance.engine import IST, calculate_month, local_day, lop_amount
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.config import settings
from app.db.mongo import ensure_index, get_db
from app.branches import branch_of, can_see, manages
from app.db.users import ADMIN_ROLE, EMPLOYEE_ROLES, MANAGER_ROLES, STAFF_ROLE, normalize_branch, on_attendance
from app.logging_config import get_logger
from app.reimbursements import approved_amount as approved_reimbursements, approved_claims

log = get_logger(__name__)

router = APIRouter(prefix="/payroll", tags=["payroll"])
RUNS = "payroll_runs"
OVERRIDE_LOGS = "payroll_override_logs"
INCENTIVES = "payroll_incentives"
DEFAULT_SHIFT_MINUTES = 8 * 60
PRESENT_STATUSES = {"P", "LT", "EE", "PP", "OD", "WFH"}


class EmployeePayrollPolicy(BaseModel):
    monthly_salary: float = Field(ge=0)


class PaymentStatus(BaseModel):
    paid: bool


class IncentiveIn(BaseModel):
    """One incentive for one employee's month; any number may be added."""
    employee_id: str = Field(min_length=1)
    amount: float = Field(gt=0, le=10_000_000)
    remarks: str = Field(default="", max_length=500)
    incentive_date: date | None = None


class PayslipDetails(BaseModel):
    """Asked for before a payslip is issued: when the salary was paid."""
    payment_date: date


class NetPayableOverride(BaseModel):
    #: The amount to pay instead of the computed one; null removes the override.
    amount: float | None = Field(default=None, ge=0)
    remarks: str = Field(min_length=3, max_length=1000)


def _manager(user: dict = Depends(current_user)) -> dict:
    if user.get("role") not in {ADMIN_ROLE, *MANAGER_ROLES}:
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
    if user.get("role") in MANAGER_ROLES:
        manager = users.get(user["id"])
        return [
            employee for employee in users.list_employees(include_inactive=False)
            if can_see(manager, employee, users)
        ]
    raise HTTPException(status_code=403, detail="Payroll access required")


def _require_payroll_authority(user: dict, employee) -> None:
    """A manager changes payroll only for the staff of their own branch."""
    if user.get("role") in MANAGER_ROLES and not manages(users.get(user["id"]), employee, users):
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
    tracked = on_attendance(employee)
    tracking_start = attendance_repo.attendance_start_date(employee.id) if tracked else None
    # Payroll-only staff (no CRM access) have no attendance: no deductions,
    # the full monthly salary, plus reimbursements and any override.
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
    # Approved OT is time worked beyond the normal hours, and it only
    # cancels the month's late / short time. Only late time: an absent day
    # or unpaid leave is a whole missing day, not lateness, and stays
    # deducted. OT is never paid as extra money; whatever is left after the
    # offset is reported (`paid_ot_minutes`, kept for compatibility) but earns
    # nothing.
    ot_offset_minutes = min(approved_ot_minutes, late_unpaid_minutes)
    unpaid_minutes = unpaid_before_ot - ot_offset_minutes
    paid_ot_minutes = approved_ot_minutes - ot_offset_minutes
    deduction = lop_amount(unpaid_minutes, monthly_salary, scheduled_minutes)
    extra_ot_amount = 0.0
    # Claims Yoosuf approved this month are paid with this month's salary.
    reimbursement_amount = approved_reimbursements(employee.id, year, month, db=runs.database)
    run = runs.find_one({"employee_id": employee.id, "year": year, "month": month}) or {}
    # Incentives are recorded one by one in the Incentives section.
    incentives = _incentives(runs.database, year, month, employee.id)
    incentive_amount = round(sum(float(item.get("amount", 0) or 0) for item in incentives), 2)
    computed_payable = round(
        max(0, monthly_salary - deduction) + extra_ot_amount + reimbursement_amount + incentive_amount, 2
    )
    override = run.get("net_payable_override")
    overridden = override is not None
    return {
        "employee_id": employee.id,
        "name": employee.name,
        "branch": branch_of(employee),
        "staff_code": employee.staff_code,
        "attendance_tracked": tracked,
        "monthly_salary": monthly_salary,
        "deduction": deduction,
        "net_salary": round(max(0, monthly_salary - deduction), 2),
        "approved_ot_minutes": approved_ot_minutes,
        "ot_offset_minutes": ot_offset_minutes,
        "paid_ot_minutes": paid_ot_minutes,
        "extra_ot_amount": extra_ot_amount,
        "reimbursement_amount": reimbursement_amount,
        "incentive_amount": incentive_amount,
        "incentives": incentives,
        "payment_date": run.get("payment_date"),
        "payslip_issued": bool(run.get("payment_date")),
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
        "unpaid_leave_days": sum(day.get("status") == "UL" for day in days),
        "weekly_off_days": sum(day.get("status") == "WO" for day in days),
        "holiday_days": sum(day.get("status") == "H" for day in days),
        "worked_minutes": sum(int(day.get("actual_covered_minutes", 0) or 0) for day in days),
        "phone": getattr(employee, "phone", "") or "",
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


def _incentives(db, year: int, month: int, employee_id: str | None = None) -> list[dict]:
    query: dict = {"year": year, "month": month}
    if employee_id:
        query["employee_id"] = employee_id
    rows = []
    for row in db[INCENTIVES].find(query).sort("created_at", ASCENDING):
        row["id"] = row.pop("_id")
        created = row.get("created_at")
        if isinstance(created, datetime):
            row["created_at"] = created.replace(tzinfo=created.tzinfo or timezone.utc).isoformat()
        rows.append(row)
    return rows


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
    if not employee or not employee.active or employee.role not in EMPLOYEE_ROLES:
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
    return f"₹ {value:,.2f}"


def _hours(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _day_label(value) -> str:
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    return value.strftime("%d %b %Y") if value else "-"


LOGO_PATH = Path(__file__).resolve().parent / "assets" / "adira-logo.png"


def _payslip_html(row: dict, employee, year: int, month: int, reimbursements: list[dict]) -> str:
    """The payslip, laid out like the office's earlier SalaryBox payslips."""
    esc = html.escape
    month_name = date(year, month, 1).strftime("%B")
    period = f"{month_name} {year}"

    earnings = [("Basic Salary", row["monthly_salary"])]
    if row["extra_ot_amount"]:
        earnings.append(("Extra OT", row["extra_ot_amount"]))
    if row["incentive_amount"]:
        earnings.append(("Incentives", row["incentive_amount"]))
    if row["reimbursement_amount"]:
        earnings.append(("Reimbursements", row["reimbursement_amount"]))
    deductions = []
    if row["deduction"]:
        deductions.append(("Loss of Pay (attendance)", row["deduction"]))
    # An admin override is shown as the adjustment it is, so the columns
    # still add up to the net salary actually paid.
    adjustment = round(row["total_payable"] - row["computed_payable"], 2)
    if row["net_payable_overridden"] and adjustment > 0:
        earnings.append(("Adjustment", adjustment))
    elif row["net_payable_overridden"] and adjustment < 0:
        deductions.append(("Adjustment", -adjustment))
    total_earnings = round(sum(amount for _, amount in earnings), 2)
    total_deductions = round(sum(amount for _, amount in deductions), 2)
    net = round(total_earnings - total_deductions, 2)
    paid = net if row.get("payment_date") else 0.0

    rows = max(len(earnings), len(deductions))
    calc = "".join(
        "<tr>"
        + (f"<td>{esc(earnings[i][0])}</td><td class='r'>{_money(earnings[i][1])}</td>" if i < len(earnings) else "<td></td><td></td>")
        + (f"<td class='sep'>{esc(deductions[i][0])}</td><td class='r'>{_money(deductions[i][1])}</td>" if i < len(deductions) else "<td class='sep'></td><td></td>")
        + "</tr>"
        for i in range(rows)
    )

    other = [
        ("Incentives", _day_label(item.get("incentive_date") or row.get("payment_date")), item.get("amount", 0), item.get("remarks", ""))
        for item in row.get("incentives") or []
    ] + [
        ("Reimbursements", _day_label(claim.get("decided_at")), claim.get("amount", 0), claim.get("description", ""))
        for claim in reimbursements
    ]
    if row["net_payable_overridden"]:
        other.append(("Adjustment", "-", adjustment, row.get("override_remarks") or ""))
    other_html = ""
    if other:
        other_html = (
            "<h3>Other Earnings Breakdown</h3><table class='grid'>"
            "<tr><th style='width:25%'>Earning Type</th><th style='width:25%'>Date</th><th style='width:25%'>Amount</th><th style='width:25%'>Notes</th></tr>"
            + "".join(
                f"<tr><td>{esc(kind)}</td><td>{esc(when)}</td><td>{_money(float(amount))}</td><td>{esc(notes)}</td></tr>"
                for kind, when, amount, notes in other
            )
            + "</table>"
        )

    if row.get("attendance_tracked", True):
        attendance = (
            "<h3>Attendance Summary</h3><table class='grid'>"
            "<tr><th style='width:20%'>Present</th><th style='width:20%'>Absent</th><th style='width:20%'>Paid Leaves</th><th style='width:20%'>Unpaid Leaves</th><th style='width:20%'>Weekly Off</th></tr>"
            f"<tr><td>{row['present_days']}</td><td>{row['absent_days'] - row['unpaid_leave_days']}</td>"
            f"<td>{row['paid_leave_days']}</td><td>{row['unpaid_leave_days']}</td><td>{row['weekly_off_days']}</td></tr>"
            "<tr><th>Holidays</th><th>Working Days</th><th>Hours Worked</th><th>Overtime</th><th></th></tr>"
            f"<tr><td>{row['holiday_days']}</td><td>{row['required_working_days']}</td>"
            f"<td>{_hours(row['worked_minutes'])}</td><td>{_hours(row['approved_ot_minutes'])}</td><td></td></tr>"
            "</table>"
        )
    else:
        attendance = "<h3>Attendance Summary</h3><p class='muted'>Attendance is not tracked for this employee (payroll only).</p>"

    paid_breakdown = ""
    if row.get("payment_date"):
        paid_breakdown = (
            "<h3>Paid Amount Breakdown</h3><table class='grid'>"
            "<tr><th style='width:25%'>Payment Type</th><th style='width:25%'>Date</th><th style='width:25%'>Amount</th><th style='width:25%'>Notes</th></tr>"
            f"<tr><td>Salary</td><td>{esc(_day_label(row['payment_date']))}</td><td>{_money(paid)}</td><td></td></tr>"
            "</table>"
        )

    logo = f"<img src='adira-logo.png' width='150'>" if LOGO_PATH.exists() else ""
    return f"""
<style>
  body {{ font-family: sans-serif; font-size: 9pt; color: #111827; }}
  .head td {{ border: 0; vertical-align: middle; padding: 0; }}
  .company {{ font-size: 13pt; }}
  .address {{ font-size: 8.5pt; color: #374151; }}
  h2 {{ font-size: 12pt; text-align: center; margin: 8pt 0; padding: 6pt 0; border-top: 1px solid #d1d5db; border-bottom: 1px solid #d1d5db; }}
  h3 {{ font-size: 10.5pt; font-weight: normal; margin: 12pt 0 6pt; }}
  table {{ width: 100%; border-collapse: collapse; }}
  .details td {{ padding: 3pt 4pt; border: 0; }}
  .details .v {{ font-weight: bold; }}
  .calc, .grid {{ border: 1px solid #d1d5db; }}
  .calc th.r {{ text-align: right; }}
  .calc th, .grid th {{ background: #f3f4f6; text-align: left; padding: 5pt 6pt; font-weight: normal; border: 1px solid #d1d5db; }}
  .calc td {{ padding: 4pt 6pt; }}
  .grid td {{ padding: 4pt 6pt; border: 1px solid #d1d5db; }}
  .calc .sep {{ border-left: 1px solid #d1d5db; }}
  .calc .total td {{ font-weight: bold; border-top: 1px solid #d1d5db; }}
  .r {{ text-align: right; }}
  .net td {{ padding: 5pt 8pt; border: 1px solid #d1d5db; }}
  .net .b {{ font-weight: bold; }}
  .muted {{ color: #6b7280; }}
</style>
<table class='head'><tr>
  <td><div class='company'>{esc(settings.payslip_company_name)}</div><div class='address'>{esc(settings.payslip_company_address)}</div></td>
  <td class='r'>{logo}</td>
</tr></table>
<h2>Pay Slip for {period}</h2>
<h3>Employee Details</h3>
<table class='details'>
  <tr><td style='width:17%'>Name</td><td style='width:33%' class='v'>{esc(employee.name)}</td><td style='width:17%'>Phone Number</td><td style='width:33%' class='v'>{esc(row.get('phone') or '-')}</td></tr>
  <tr><td>Employee Id</td><td class='v'>{esc(row.get('staff_code') or '-')}</td><td>Salary Amount</td><td class='v'>{_money(row['monthly_salary'])}/Month</td></tr>
  <tr><td>Branch</td><td class='v'>{esc(row.get('branch') or '-')}</td><td>Date of Payment</td><td class='v'>{esc(_day_label(row.get('payment_date')))}</td></tr>
</table>
<h3>Salary Calculations</h3>
<table class='calc'>
  <tr><th style='width:30%'>EARNINGS</th><th style='width:20%' class='r'>AMOUNT</th><th style='width:30%' class='sep'>DEDUCTIONS</th><th style='width:20%' class='r'>AMOUNT</th></tr>
  {calc}
  <tr class='total'><td>Total Earnings</td><td class='r'>{_money(total_earnings)}</td><td class='sep'>Total Deductions</td><td class='r'>{_money(total_deductions)}</td></tr>
</table>
<br>
<table class='net'>
  <tr><td style='width:50%'>{month_name} Net Salary</td><td style='width:50%'>{_money(net)}</td></tr>
  <tr><td class='b'>Paid Amount</td><td class='b'>{_money(paid)}</td></tr>
  <tr><td class='b'>Pending {month_name} Salary</td><td class='b'>{_money(round(net - paid, 2))}</td></tr>
</table>
{attendance}
{other_html}
{paid_breakdown}
"""


def render_payslip_pdf(row: dict, employee, year: int, month: int, reimbursements: list[dict] | None = None) -> bytes:
    import fitz  # PyMuPDF

    archive = fitz.Archive(str(LOGO_PATH.parent)) if LOGO_PATH.exists() else None
    document = fitz.open()
    width, height = 595, 842  # A4 in points
    page = document.new_page(width=width, height=height)
    body = _payslip_html(row, employee, year, month, reimbursements or [])
    story_box = fitz.Rect(40, 36, width - 40, height - 50)
    # `insert_htmlbox` scales down rather than overflowing; a payslip with a
    # long breakdown still fits one page, as the office's payslips do.
    page.insert_htmlbox(story_box, body, archive=archive)
    page.insert_text(
        (width - 40 - 170, height - 28),
        f"Report date: {datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')}",
        fontsize=8, color=(0.3, 0.3, 0.3),
    )
    data = document.tobytes()
    document.close()
    return data


def _payslip_employee(user: dict, employee_id: str):
    employee = next((e for e in _visible_employees(user) if e.id == employee_id), None)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


# --------------------------------------------------------------------------- #
#  Incentives: as many as needed per employee per month, each with remarks
# --------------------------------------------------------------------------- #
@router.get("/{year}/{month}/incentives")
def list_incentives(year: int, month: int, user: dict = Depends(current_user)) -> dict:
    """The month's incentives: own for staff, their branch for managers, all for admins."""
    visible = {employee.id for employee in _visible_employees(user)}
    items = [row for row in _incentives(get_db(), year, month) if row["employee_id"] in visible]
    return {"items": items, "can_manage": user.get("role") in {ADMIN_ROLE, *MANAGER_ROLES}}


@router.post("/{year}/{month}/incentives", status_code=201)
def add_incentive(year: int, month: int, payload: IncentiveIn, user: dict = Depends(_manager)) -> dict:
    """Record one incentive; it is added to that month's net payable."""
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="month must be between 1 and 12")
    employee = _payslip_employee(user, payload.employee_id)
    _require_payroll_authority(user, employee)
    doc = {
        "_id": uuid.uuid4().hex,
        "employee_id": employee.id,
        "employee_name": employee.name,
        "year": year,
        "month": month,
        "amount": round(payload.amount, 2),
        "remarks": payload.remarks.strip(),
        "incentive_date": (payload.incentive_date or local_day(datetime.now(timezone.utc))).isoformat(),
        "created_by": user["id"],
        "created_by_name": user.get("name", ""),
        "created_at": datetime.now(timezone.utc),
    }
    get_db()[INCENTIVES].insert_one(doc)
    log.info("Incentive of %s for %s %04d-%02d added by %s: %s",
             doc["amount"], employee.id, year, month, user.get("email"), doc["remarks"])
    doc["id"] = doc.pop("_id")
    doc["created_at"] = doc["created_at"].isoformat()
    return {"status": "created", "incentive": doc}


@router.delete("/{year}/{month}/incentives/{incentive_id}")
def delete_incentive(year: int, month: int, incentive_id: str, user: dict = Depends(_manager)) -> dict:
    doc = get_db()[INCENTIVES].find_one({"_id": incentive_id, "year": year, "month": month})
    if not doc:
        raise HTTPException(status_code=404, detail="Incentive not found")
    employee = _payslip_employee(user, doc["employee_id"])
    _require_payroll_authority(user, employee)
    get_db()[INCENTIVES].delete_one({"_id": incentive_id})
    log.info("Incentive %s for %s %04d-%02d removed by %s", incentive_id, employee.id, year, month, user.get("email"))
    return {"status": "deleted", "id": incentive_id}


@router.put("/{year}/{month}/employees/{employee_id}/payslip-details")
def save_payslip_details(year: int, month: int, employee_id: str, payload: PayslipDetails, user: dict = Depends(_manager)) -> dict:
    """Record the salary payment date, asked for before a payslip is issued.

    The staff member can download their payslip once a payment date is recorded.
    """
    employee = _payslip_employee(user, employee_id)
    _require_payroll_authority(user, employee)
    if not payslip_available(year, month):
        raise HTTPException(status_code=409, detail="The payslip is generated on the last day of the month")
    get_db()[RUNS].update_one(
        {"employee_id": employee_id, "year": year, "month": month},
        {"$set": {
            "payment_date": payload.payment_date.isoformat(),
            "payslip_details_by": user["id"],
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    log.info("Payslip payment date for %s %04d-%02d set to %s by %s",
             employee_id, year, month, payload.payment_date, user.get("email"))
    return {"status": "saved", "payment_date": payload.payment_date.isoformat()}


@router.get("/{year}/{month}/employees/{employee_id}/payslip")
def download_payslip(year: int, month: int, employee_id: str, user: dict = Depends(current_user)) -> Response:
    """The month's payslip as a PDF, once its payment date has been recorded."""
    employee = _payslip_employee(user, employee_id)
    if not payslip_available(year, month):
        raise HTTPException(status_code=409, detail="The payslip is generated on the last day of the month")
    row = _row_for(employee, year, month)
    if not row.get("payment_date"):
        raise HTTPException(status_code=409, detail="Enter the payment date before downloading the payslip")
    reimbursements = approved_claims(employee.id, year, month, db=get_db())
    slug = re.sub(r"[^A-Za-z0-9]+", "-", employee.name).strip("-") or employee_id
    filename = f"Pay-Slip-{date(year, month, 1).strftime('%B')}-{year}-{slug}.pdf"
    return Response(
        content=render_payslip_pdf(row, employee, year, month, reimbursements),
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
        get_db()[INCENTIVES],
        [("year", ASCENDING), ("month", ASCENDING), ("employee_id", ASCENDING)],
        "payroll_incentive_period",
    )
    ensure_index(
        get_db()[OVERRIDE_LOGS],
        [("year", ASCENDING), ("month", ASCENDING), ("created_at", DESCENDING)],
        "payroll_override_period",
    )
