"""Authenticated REST API for employee attendance."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.routes import current_user, require_admin, require_service_key, users
from app.attendance.engine import calculate_month, local_day, lop_amount
from app.attendance.models import AdjustmentRequest, CalendarDayRequest, DutyPlanRequest, ExtraOTDecision, ExtraOTRequest, PermissionDecision, PermissionRequest, PunchRequest, ShiftAssignmentRequest, WeeklyOffRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceError, AttendanceService
from app.branches import branch_managers, branch_of, can_see, manages
from app.db.users import ADMIN_ROLE, MANAGER_ROLE, STAFF_ROLE
from app.whatsapp.groups import GroupIntakeError, resolve_employee
from app.db.notifications import ATTENDANCE_REQUEST, NotificationRepository
from app.logging_config import get_logger

router = APIRouter(prefix="/attendance", tags=["attendance"])
log = get_logger(__name__)


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
    if user.get("role") == MANAGER_ROLE and not can_see(users.get(user["id"]), employee, users):
        # Noorul works Royapettah and Rafi works Mount Road; neither reads nor
        # changes the other branch's attendance.
        raise HTTPException(status_code=404, detail="Active employee not found")
    return employee_id


def _conflict(call):
    try:
        return call()
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class WhatsAppAttendanceEvent(BaseModel):
    """A private-chat attendance command forwarded by the bot."""

    message_id: str = Field(min_length=1, max_length=200)
    sender_phone: str = Field(min_length=5, max_length=50)
    stated_name: str = Field(min_length=1, max_length=150)
    action: Literal["check_in", "check_out"]
    occurred_at: datetime
    chat_type: Literal["private"] = "private"


def _whatsapp_employee(sender_phone: str, stated_name: str):
    """Authenticate attendance by CRM phone and verify the written name.

    The rule itself lives in `app.whatsapp.groups.resolve_employee`, shared with
    group intake so a number and a name mean the same thing in both chats. This
    only turns the refusal into the HTTP answer the bot expects.
    """
    try:
        return resolve_employee(users, sender_phone, stated_name)
    except GroupIntakeError as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc


@router.post("/punch", status_code=201)
def record_punch(payload: PunchRequest, user: dict = Depends(current_user)) -> dict:
    employee_id = _employee_id(user, payload.employee_id)
    attendance = service()
    event, created = _conflict(lambda: attendance.punch(employee_id, payload, allow_recorded_time=user.get("role") in {ADMIN_ROLE, MANAGER_ROLE}))
    event_day = local_day(event["occurred_at"], attendance.policy.timezone_name)
    return {
        "status": "recorded" if created else "duplicate",
        "event": event,
        "attendance": attendance.day(employee_id, event_day),
    }


@router.post("/webhooks/whatsapp", status_code=201)
def whatsapp_punch(payload: PunchRequest, _service: None = Depends(require_service_key)) -> dict:
    if not payload.employee_id:
        raise HTTPException(status_code=422, detail="employee_id is required")
    employee = users.get(payload.employee_id)
    if not employee or not employee.active:
        raise HTTPException(status_code=404, detail="Active employee not found")
    payload.source = "whatsapp"
    attendance = service()
    event, created = _conflict(
        lambda: attendance.punch(payload.employee_id, payload, allow_recorded_time=True)
    )
    event_day = local_day(event["occurred_at"], attendance.policy.timezone_name)
    return {
        "status": "recorded" if created else "duplicate",
        "event": event,
        "attendance": attendance.day(payload.employee_id, event_day),
    }


@router.post("/events", status_code=201)
def whatsapp_private_attendance(
    payload: WhatsAppAttendanceEvent,
    _service: None = Depends(require_service_key),
) -> dict:
    """Record a private staff WhatsApp command; the bot confirms successful records."""
    employee = _whatsapp_employee(payload.sender_phone, payload.stated_name)
    request = PunchRequest(
        action=payload.action,
        idempotency_key=payload.message_id,
        employee_id=employee.id,
        occurred_at=payload.occurred_at,
        source="whatsapp",
        evidence={
            "metadata": {
                "chat_type": payload.chat_type,
                "stated_name": payload.stated_name,
            }
        },
    )
    attendance = service()
    event, created = _conflict(
        lambda: attendance.punch(employee.id, request, allow_recorded_time=True)
    )
    event_day = local_day(event["occurred_at"], attendance.policy.timezone_name)
    return {
        "status": "recorded" if created else "duplicate",
        "event": event,
        "attendance": attendance.day(employee.id, event_day),
    }


@router.get("/directory")
def whatsapp_attendance_directory(
    _service: None = Depends(require_service_key),
) -> dict:
    """Active employees whose private phone messages are staff traffic."""
    contacts = [
        {
            "id": employee.id,
            "staff_code": employee.staff_code,
            "name": employee.name,
            "phone": employee.phone,
            "role": employee.role,
            "active": employee.active,
        }
        for employee in users.list_employees(include_inactive=False)
    ]
    return {"contacts": contacts, "count": len(contacts)}


@router.get("/employees")
def attendance_employees(user: dict = Depends(require_attendance_manager)) -> dict:
    """Active people visible in the attendance team roster.

    Administrators review attendance for every employee, including managers.
    A manager's team view remains staff-only so one manager cannot inspect
    another manager's attendance.
    """
    if user.get("role") == ADMIN_ROLE:
        employees = users.list_employees(include_inactive=False)
    else:
        employees = _branch_staff(user)
    return {"items": [employee.to_public() for employee in employees], "count": len(employees)}


@router.get("/day/{attendance_date}")
def attendance_day(attendance_date: date, employee_id: str | None = Query(default=None), user: dict = Depends(current_user)) -> dict:
    return service().day(_employee_id(user, employee_id), attendance_date)


@router.get("/weekly-off")
def get_weekly_off(user: dict = Depends(current_user)) -> dict:
    employee_id = _employee_id(user)
    policy = AttendanceRepository().employee_policy(employee_id)
    pattern = policy.get("weekly_off_pattern", "sunday")
    if pattern == "sunday_alternate_friday":
        pattern = "alternate_friday"
    return {"employee_id": employee_id, "weekly_off_pattern": pattern}


@router.put("/weekly-off")
def update_weekly_off(payload: WeeklyOffRequest, user: dict = Depends(current_user)) -> dict:
    employee_id = _employee_id(user)
    policy = AttendanceRepository().set_employee_policy(
        employee_id,
        {"weekly_off_pattern": payload.weekly_off_pattern},
    )
    return {
        "status": "saved",
        "employee_id": employee_id,
        "weekly_off_pattern": policy["weekly_off_pattern"],
    }


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
    attendance = service()
    tracking_start = attendance.repository.attendance_start_date(employee)
    period_end = date(year, month, end)
    if tracking_start is None or tracking_start > period_end:
        daily = []
    else:
        first = tracking_start.day if (tracking_start.year, tracking_start.month) == (year, month) else 1
        daily = [attendance.day(employee, date(year, month, number)) for number in range(first, end + 1)]
    days = calculate_month(daily)
    unpaid_total = sum(row["unpaid_minutes"] for row in days)
    result = {
        "employee_id": employee,
        "year": year,
        "month": month,
        "days": days,
        "totals": {
            "paid_permission_minutes": sum(row["paid_permission_minutes"] for row in days),
            "approved_permission_minutes": sum(row.get("approved_permission_minutes", 0) for row in days),
            "grace_minutes": sum(row.get("grace_minutes_applied", 0) for row in days),
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


def _approvers(employee_record) -> tuple[list, str]:
    """Who decides this employee's requests.

    A manager's own request goes to the administrators. Staff requests go to
    the manager of their branch: Noorul at Royapettah for the Singapore and
    Malaysia desk, Rafi at Mount Road for every other destination (see
    `app.branches`).
    """
    if employee_record and employee_record.role == MANAGER_ROLE:
        return getattr(users, "list_admins", lambda: [])(), "administrators"
    if employee_record is None:
        return getattr(users, "list_managers", lambda: [])(), "managers"
    return branch_managers(employee_record, users), f"{branch_of(employee_record)} managers"


def _notify_approvers(employee_id: str, request_id, title: str, what: str) -> None:
    employee_record = users.get(employee_id)
    approvers, recipient_label = _approvers(employee_record)
    try:
        notification_repo = NotificationRepository() if approvers else None
        for approver in approvers:
            notification_repo.record(
                approver.id,
                type=ATTENDANCE_REQUEST,
                title=title,
                message=f"{employee_record.name if employee_record else employee_id} requested {what}.",
            )
    except Exception as exc:  # The request is durable even if its alert cannot be written.
        log.warning("Attendance request %s could not notify %s: %s", request_id, recipient_label, exc)


def _require_approver(approver: dict, employee_id: str | None) -> None:
    """Refuse a decision from anyone but the requester's own approvers."""
    if approver.get("role") == ADMIN_ROLE:
        return
    requester = users.get(employee_id) if employee_id else None
    if requester is None:
        return
    if requester.role == MANAGER_ROLE:
        raise HTTPException(status_code=403, detail="Administrator approval is required for a manager request")
    if not manages(users.get(approver["id"]), requester, users):
        raise HTTPException(
            status_code=403,
            detail=f"This request belongs to the {branch_of(requester)} branch manager",
        )


def _branch_staff(user: dict) -> list:
    """The staff a manager answers for: their own branch only."""
    staff = (
        users.list_staff(include_inactive=False)
        if hasattr(users, "list_staff")
        else users.list_assignable_staff()
    )
    manager = users.get(user["id"])
    return [member for member in staff if manages(manager, member, users)]


@router.post("/permissions", status_code=201)
def request_permission(payload: PermissionRequest, user: dict = Depends(current_user)) -> dict:
    employee = _employee_id(user, payload.employee_id)
    permission = _conflict(lambda: service().request_permission(employee, payload))
    _notify_approvers(
        employee, permission.get("id"), "Attendance request",
        f"{payload.kind.replace('_', ' ')} for {payload.attendance_date}",
    )
    return {"status": "pending", "permission": permission}


@router.post("/extra-ot", status_code=201)
def request_extra_ot(payload: ExtraOTRequest, user: dict = Depends(current_user)) -> dict:
    employee = _employee_id(user, payload.employee_id)
    request = _conflict(lambda: service().request_extra_ot(employee, payload))
    _notify_approvers(
        employee, request.get("id"), "Extra OT request",
        f"{payload.requested_minutes} min Extra OT for {payload.attendance_date}",
    )
    return {"status": "pending", "request": request}


@router.post("/extra-ot/{request_id}/decision")
def decide_extra_ot(request_id: str, payload: ExtraOTDecision, approver: dict = Depends(require_attendance_manager)) -> dict:
    pending = AttendanceRepository().extra_ot(request_id)
    _require_approver(approver, pending.get("employee_id") if pending else None)
    return {"status": "decided", "request": _conflict(lambda: service().decide_extra_ot(request_id, payload, approver["id"]))}


def _period(year: int, month: int) -> tuple[date, date]:
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="month must be between 1 and 12")
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _visible_employee_ids(user: dict, employee_id: str | None) -> str | list[str]:
    """Whose records this caller may list.

    Own history for employees; their branch's staff for managers; everyone for
    administrators. Naming an employee narrows a manager or administrator to
    that person, and is checked by `_employee_id` so staff cannot name somebody
    else.
    """
    if user.get("role") in {ADMIN_ROLE, MANAGER_ROLE} and not employee_id:
        if user.get("role") == ADMIN_ROLE:
            members = (
                users.list_employees(include_inactive=False)
                if hasattr(users, "list_employees")
                else users.list_assignable_staff()
            )
        else:
            members = _branch_staff(user)
        return [member.id for member in members]
    return _employee_id(user, employee_id)


@router.get("/permissions")
def list_permissions(
    year: int,
    month: int,
    employee_id: str | None = Query(default=None),
    user: dict = Depends(current_user),
) -> dict:
    """Own history for employees; staff approvals for managers; all for admins."""
    start, end = _period(year, month)
    items = AttendanceRepository().permissions_for_period(
        _visible_employee_ids(user, employee_id), start, end
    )
    return {"items": items, "count": len(items), "year": year, "month": month}


@router.get("/extra-ot")
def list_extra_ot(
    year: int,
    month: int,
    employee_id: str | None = Query(default=None),
    user: dict = Depends(current_user),
) -> dict:
    """Extra OT requests for the month, scoped exactly like permissions."""
    start, end = _period(year, month)
    items = AttendanceRepository().extra_ot_for_period(
        _visible_employee_ids(user, employee_id), start, end
    )
    return {"items": items, "count": len(items), "year": year, "month": month}


# --------------------------------------------------------------------------- #
#  Duty planning
#
#  A duty plan rosters one date as a working day. It is what makes a Sunday
#  count, and it is deliberately separate from `POST /shifts`, which changes an
#  employee's hours from a date onwards and says nothing about which days are
#  worked.
# --------------------------------------------------------------------------- #
@router.post("/duty-plans", status_code=201)
def create_duty_plan(payload: DutyPlanRequest, admin: dict = Depends(require_attendance_manager)) -> dict:
    _employee_id(admin, payload.employee_id)
    return {"status": "recorded", "duty_plan": service().plan_duty(payload, admin["id"])}


@router.get("/duty-plans")
def list_duty_plans(
    year: int,
    month: int,
    employee_id: str | None = Query(default=None),
    user: dict = Depends(current_user),
) -> dict:
    start, end = _period(year, month)
    items = service().duty_plans(_visible_employee_ids(user, employee_id), start, end)
    return {"items": items, "count": len(items), "year": year, "month": month}


@router.delete("/duty-plans/{plan_id}")
def delete_duty_plan(plan_id: str, admin: dict = Depends(require_attendance_manager)) -> dict:
    if not service().remove_duty_plan(plan_id):
        raise HTTPException(status_code=404, detail="Duty plan not found")
    return {"status": "deleted", "id": plan_id}


@router.post("/permissions/{permission_id}/decision")
def decide_permission(permission_id: str, payload: PermissionDecision, admin: dict = Depends(require_attendance_manager)) -> dict:
    attendance = service()
    pending = attendance.repository.permission(permission_id)
    if not pending or pending.get("status") != "pending":
        raise HTTPException(status_code=409, detail="Pending permission not found")
    _require_approver(admin, pending["employee_id"])
    permission = _conflict(lambda: attendance.decide_permission(permission_id, payload, admin["id"]))
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
