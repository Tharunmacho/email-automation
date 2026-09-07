"""Authenticated REST API for employee attendance."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.routes import current_user, require_admin, require_service_key, users
from app.attendance.engine import calculate_month, lop_amount
from app.attendance.models import AdjustmentRequest, CalendarDayRequest, PermissionDecision, PermissionRequest, PunchRequest, ShiftAssignmentRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceError, AttendanceService
from app.db.users import ADMIN_ROLE, MANAGER_ROLE, STAFF_ROLE

router = APIRouter(prefix="/attendance", tags=["attendance"])


def service() -> AttendanceService:
    return AttendanceService(AttendanceRepository())


def require_attendance_manager(user: dict = Depends(current_user)) -> dict:
    if user.get("role") not in {ADMIN_ROLE, MANAGER_ROLE}:
        raise HTTPException(status_code=403, detail="Manager role required")
    return user


def _employee_id(user: dict, requested: str | None = None) -> str:
    employee_id = requested if user.get("role") in {ADMIN_ROLE, MANAGER_ROLE} and requested else user["id"]
    employee = users.get(employee_id)
    if not employee or not employee.active or employee.role not in {STAFF_ROLE, MANAGER_ROLE}:
        raise HTTPException(status_code=404, detail="Active employee not found")
    return employee_id


def _conflict(call):
    try:
        return call()
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/punch", status_code=201)
def record_punch(payload: PunchRequest, user: dict = Depends(current_user)) -> dict:
    employee_id = _employee_id(user, payload.employee_id)
    event, created = _conflict(lambda: service().punch(employee_id, payload, allow_recorded_time=user.get("role") in {ADMIN_ROLE, MANAGER_ROLE}))
    return {"status": "recorded" if created else "duplicate", "event": event}


@router.post("/webhooks/whatsapp", status_code=201)
def whatsapp_punch(payload: PunchRequest, _service: None = Depends(require_service_key)) -> dict:
    if not payload.employee_id:
        raise HTTPException(status_code=422, detail="employee_id is required")
    employee = users.get(payload.employee_id)
    if not employee or not employee.active:
        raise HTTPException(status_code=404, detail="Active employee not found")
    payload.source = "whatsapp"
    event, created = _conflict(lambda: service().punch(payload.employee_id, payload))
    return {"status": "recorded" if created else "duplicate", "event": event}


@router.get("/day/{attendance_date}")
def attendance_day(attendance_date: date, employee_id: str | None = Query(default=None), user: dict = Depends(current_user)) -> dict:
    return service().day(_employee_id(user, employee_id), attendance_date)


@router.get("/month/{year}/{month}")
def attendance_month(
    year: int,
    month: int,
    employee_id: str | None = Query(default=None),
    wage_basis: float | None = Query(default=None, ge=0),
    scheduled_payable_minutes: int | None = Query(default=None, gt=0),
    user: dict = Depends(current_user),
) -> dict:
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="month must be between 1 and 12")
    employee = _employee_id(user, employee_id)
    today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
    last = calendar.monthrange(year, month)[1]
    end = min(last, today.day) if (year, month) == (today.year, today.month) else last
    if (year, month) > (today.year, today.month):
        return {"employee_id": employee, "year": year, "month": month, "days": []}
    daily = [service().day(employee, date(year, month, number)) for number in range(1, end + 1)]
    days = calculate_month(daily)
    unpaid_total = sum(row["unpaid_minutes"] for row in days)
    result = {
        "employee_id": employee,
        "year": year,
        "month": month,
        "days": days,
        "totals": {
            "paid_permission_minutes": sum(row["paid_permission_minutes"] for row in days),
            "unpaid_minutes": unpaid_total,
            "permission_occasions": days[-1]["permission_occasions_used"] if days else 0,
        },
    }
    if (wage_basis is None) != (scheduled_payable_minutes is None):
        raise HTTPException(status_code=422, detail="wage_basis and scheduled_payable_minutes must be supplied together")
    if wage_basis is not None and scheduled_payable_minutes is not None:
        result["salary_preview"] = {
            "label": "PROVISIONAL",
            "attendance_lop": lop_amount(unpaid_total, wage_basis, scheduled_payable_minutes),
            "wage_basis": wage_basis,
            "scheduled_payable_minutes": scheduled_payable_minutes,
        }
    return result


@router.post("/permissions", status_code=201)
def request_permission(payload: PermissionRequest, user: dict = Depends(current_user)) -> dict:
    employee = _employee_id(user, payload.employee_id)
    permission = _conflict(lambda: service().request_permission(employee, payload))
    return {"status": "pending", "permission": permission}


@router.get("/permissions")
def list_permissions(
    year: int,
    month: int,
    employee_id: str | None = Query(default=None),
    user: dict = Depends(current_user),
) -> dict:
    """Own permission history for staff; roster permission history for admin."""
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="month must be between 1 and 12")
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    repository = AttendanceRepository()

    if user.get("role") in {ADMIN_ROLE, MANAGER_ROLE} and not employee_id:
        members = (
            users.list_employees(include_inactive=False)
            if hasattr(users, "list_employees")
            else users.list_assignable_staff()
        )
        employee_ids = [member.id for member in members]
        items = repository.permissions_for_period(employee_ids, start, end)
    else:
        employee = _employee_id(user, employee_id)
        items = repository.permissions_for_period(employee, start, end)
    return {"items": items, "count": len(items), "year": year, "month": month}


@router.post("/permissions/{permission_id}/decision")
def decide_permission(permission_id: str, payload: PermissionDecision, admin: dict = Depends(require_attendance_manager)) -> dict:
    permission = _conflict(lambda: service().decide_permission(permission_id, payload, admin["id"]))
    return {"status": permission["status"], "permission": permission}


@router.post("/adjustments", status_code=201)
def create_adjustment(payload: AdjustmentRequest, admin: dict = Depends(require_attendance_manager)) -> dict:
    _employee_id(admin, payload.employee_id)
    adjustment = _conflict(lambda: service().adjust(payload, admin["id"]))
    return {"status": "recorded", "adjustment": adjustment}


@router.post("/shifts", status_code=201)
def assign_shift(payload: ShiftAssignmentRequest, admin: dict = Depends(require_attendance_manager)) -> dict:
    _employee_id(admin, payload.employee_id)
    return {"status": "recorded", "assignment": service().assign_shift(payload, admin["id"])}


@router.post("/calendar", status_code=201)
def set_calendar_day(payload: CalendarDayRequest, admin: dict = Depends(require_attendance_manager)) -> dict:
    _employee_id(admin, payload.employee_id)
    return {"status": "recorded", "calendar_day": service().set_calendar_day(payload, admin["id"])}
