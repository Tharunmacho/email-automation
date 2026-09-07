"""Monthly payroll derived from the attendance ledger."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import ASCENDING

from app.api.routes import current_user, users
from app.attendance.engine import calculate_month, lop_amount
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.db.mongo import ensure_index, get_db
from app.db.users import ADMIN_ROLE, MANAGER_ROLE, STAFF_ROLE

router = APIRouter(prefix="/payroll", tags=["payroll"])
RUNS = "payroll_runs"
DEFAULT_SHIFT_MINUTES = 8 * 60


class EmployeePayrollPolicy(BaseModel):
    monthly_salary: float = Field(ge=0)
    weekly_off_pattern: str = Field(default="sunday", pattern="^(sunday|alternate_friday)$")
    alternate_friday_parity: int = Field(default=0, ge=0, le=1)


class PaymentStatus(BaseModel):
    paid: bool


def _manager(user: dict = Depends(current_user)) -> dict:
    if user.get("role") not in {ADMIN_ROLE, MANAGER_ROLE}:
        raise HTTPException(status_code=403, detail="Manager role required")
    return user


def _visible_employees(user: dict):
    """Staff see exactly one payroll row—their own; managers see the roster."""
    if user.get("role") == STAFF_ROLE:
        employee = users.get(user["id"])
        return [employee] if employee and employee.active else []
    if user.get("role") in {ADMIN_ROLE, MANAGER_ROLE}:
        return users.list_employees(include_inactive=False)
    raise HTTPException(status_code=403, detail="Payroll access required")


def _period_days(year: int, month: int) -> list[date]:
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="month must be between 1 and 12")
    today = datetime.now(timezone.utc).date()
    if (year, month) > (today.year, today.month):
        return []
    last = calendar.monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last + 1)]


@router.get("/{year}/{month}")
def payroll_month(year: int, month: int, user: dict = Depends(current_user)) -> dict:
    attendance_repo = AttendanceRepository()
    attendance = AttendanceService(attendance_repo)
    runs = get_db()[RUNS]
    rows = []
    for employee in _visible_employees(user):
        policy = attendance_repo.employee_policy(employee.id)
        tracking_start = attendance_repo.attendance_start_date(employee.id)
        period_days = [
            day for day in _period_days(year, month)
            if tracking_start is not None and day >= tracking_start
        ]
        days = calculate_month(attendance.day(employee.id, day) for day in period_days)
        required_working_days = sum(day.get("status") not in {"WO", "H"} for day in days)
        unpaid_minutes = sum(int(day.get("unpaid_minutes", 0)) for day in days)
        grace_minutes = sum(int(day.get("grace_minutes_applied", 0)) for day in days)
        paid_leave_days = sum(day.get("status") == "PL" for day in days)
        monthly_salary = float(policy.get("monthly_salary", 0) or 0)
        scheduled_minutes = max(1, required_working_days * DEFAULT_SHIFT_MINUTES)
        deduction = lop_amount(unpaid_minutes, monthly_salary, scheduled_minutes)
        run = runs.find_one({"employee_id": employee.id, "year": year, "month": month}) or {}
        rows.append({
            "employee_id": employee.id,
            "name": employee.name,
            "staff_code": employee.staff_code,
            "monthly_salary": monthly_salary,
            "deduction": deduction,
            "net_salary": round(max(0, monthly_salary - deduction), 2),
            "unpaid_minutes": unpaid_minutes,
            "grace_minutes": grace_minutes,
            "paid_leave_days": paid_leave_days,
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
        })
    return {
        "year": year,
        "month": month,
        "basis": "required_working_days",
        "grace_allowance_minutes": 60,
        "paid_leave_allowance_days": 1,
        "items": rows,
    }


@router.put("/employees/{employee_id}")
def update_employee_payroll(employee_id: str, payload: EmployeePayrollPolicy, _user: dict = Depends(_manager)) -> dict:
    employee = users.get(employee_id)
    if not employee or not employee.active or employee.role not in {"staff", "manager"}:
        raise HTTPException(status_code=404, detail="Active employee not found")
    policy = AttendanceRepository().set_employee_policy(employee_id, payload.model_dump())
    return {"status": "saved", "policy": policy}


@router.put("/{year}/{month}/employees/{employee_id}/status")
def update_payment_status(year: int, month: int, employee_id: str, payload: PaymentStatus, actor: dict = Depends(_manager)) -> dict:
    if month < 1 or month > 12 or not users.get(employee_id):
        raise HTTPException(status_code=404, detail="Employee or payroll period not found")
    get_db()[RUNS].update_one(
        {"employee_id": employee_id, "year": year, "month": month},
        {"$set": {"paid": payload.paid, "updated_at": datetime.now(timezone.utc), "updated_by": actor["id"]}},
        upsert=True,
    )
    return {"status": "paid" if payload.paid else "draft"}


def ensure_payroll_indexes() -> None:
    ensure_index(
        get_db()[RUNS],
        [("employee_id", ASCENDING), ("year", ASCENDING), ("month", ASCENDING)],
        "payroll_employee_period",
        unique=True,
    )
