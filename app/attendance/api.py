"""Authenticated REST API for employee attendance."""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.routes import current_user, require_admin, require_service_key, users
from app.attendance.engine import calculate_month, local_day, lop_amount
from app.attendance.models import AdjustmentRequest, CalendarDayRequest, CoverNomination, CoverResponse, DutyPlanRequest, ExtraOTDecision, ExtraOTRequest, PermissionDecision, PermissionRequest, PunchRequest, ShiftAssignmentRequest, WeeklyOffRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import LEAVE_KINDS, WEEKDAYS, AttendanceError, AttendanceService
from app.attendance.sites import OffSiteError, check_on_site
from app.attendance import approvals
from app.branches import branch_of, can_see, manages, same_branch
from app.db.users import ADMIN_ROLE, EMPLOYEE_ROLES, FINANCE_MANAGER_ROLE, MANAGER_ROLES, STAFF_ROLE, on_attendance
from app.whatsapp.groups import GroupIntakeError, resolve_employee
from app.db.notifications import ATTENDANCE_REQUEST, NotificationRepository
from app.logging_config import get_logger

router = APIRouter(prefix="/attendance", tags=["attendance"])
log = get_logger(__name__)


def service() -> AttendanceService:
    return AttendanceService(AttendanceRepository())


#: Roles that see a team's attendance and decide its requests.
_APPROVER_ROLES = frozenset({ADMIN_ROLE, *MANAGER_ROLES})


def require_attendance_manager(user: dict = Depends(current_user)) -> dict:
    if user.get("role") not in _APPROVER_ROLES:
        raise HTTPException(status_code=403, detail="Manager role required")
    return user


def _employee_id(user: dict, requested: str | None = None) -> str:
    employee_id = requested if user.get("role") in _APPROVER_ROLES and requested else user["id"]
    employee = users.get(employee_id)
    if not employee or not employee.active or employee.role not in EMPLOYEE_ROLES:
        raise HTTPException(status_code=404, detail="Active employee not found")
    if not on_attendance(employee):
        # Staff without CRM access are on payroll only.
        raise HTTPException(status_code=404, detail="This employee is on payroll only, not attendance")
    if user.get("role") in MANAGER_ROLES and not can_see(users.get(user["id"]), employee, users):
        # Noorul works Royapettah and Rafi works Mount Road; neither reads nor
        # changes the other branch's attendance.
        raise HTTPException(status_code=404, detail="Active employee not found")
    return employee_id


def _conflict(call):
    try:
        return call()
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _on_site(employee, latitude: float | None, longitude: float | None) -> dict | None:
    """The office-radius check as the HTTP answer the bot relays to the employee."""
    try:
        return check_on_site(employee, latitude, longitude)
    except OffSiteError as exc:
        log.info("Attendance refused off-site for %s: %s m away", employee.id, exc.distance_m)
        raise HTTPException(status_code=403, detail=str(exc)) from exc


class WhatsAppAttendanceEvent(BaseModel):
    """A private-chat attendance command forwarded by the bot."""

    message_id: str = Field(min_length=1, max_length=200)
    sender_phone: str = Field(min_length=5, max_length=50)
    stated_name: str = Field(min_length=1, max_length=150)
    action: Literal["check_in", "check_out"]
    occurred_at: datetime
    chat_type: Literal["private"] = "private"
    #: The location the employee shared for this command; see `app.attendance.sites`.
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    #: The text command the shared location completes, when it differs from `message_id`.
    command_message_id: str | None = Field(default=None, max_length=200)


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
    if employee_id == user["id"]:
        # A punch for oneself proves presence; a manager recording somebody
        # else's punch is an administrative entry and is not held to a radius.
        site = _on_site(users.get(employee_id), payload.evidence.latitude, payload.evidence.longitude)
        if site:
            payload.evidence.metadata["site"] = site
    attendance = service()
    event, created = _conflict(lambda: attendance.punch(employee_id, payload, allow_recorded_time=user.get("role") in _APPROVER_ROLES))
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
    if not employee or not employee.active or not on_attendance(employee):
        raise HTTPException(status_code=404, detail="Active employee not found")
    payload.source = "whatsapp"
    site = _on_site(employee, payload.evidence.latitude, payload.evidence.longitude)
    if site:
        payload.evidence.metadata["site"] = site
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
    site = _on_site(employee, payload.latitude, payload.longitude)
    request = PunchRequest(
        action=payload.action,
        idempotency_key=payload.message_id,
        employee_id=employee.id,
        occurred_at=payload.occurred_at,
        source="whatsapp",
        evidence={
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "metadata": {
                "chat_type": payload.chat_type,
                "stated_name": payload.stated_name,
                "command_message_id": payload.command_message_id,
                "site": site,
            },
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
        if on_attendance(employee)
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
    employees = [employee for employee in employees if on_attendance(employee)]
    return {"items": [employee.to_public() for employee in employees], "count": len(employees)}


@router.get("/day/{attendance_date}")
def attendance_day(attendance_date: date, employee_id: str | None = Query(default=None), user: dict = Depends(current_user)) -> dict:
    return service().day(_employee_id(user, employee_id), attendance_date)


#: How many weeks ahead the weekly-off picker offers, this week included.
WEEKLY_OFF_WEEKS_AHEAD = 4


def _weekly_off_deadline(week_start: date, timezone_name: str) -> datetime:
    """Thursday 11:59:59 PM (office time) of the week: the last moment to choose."""
    thursday = week_start + timedelta(days=3)
    return datetime.combine(thursday, time(23, 59, 59), tzinfo=ZoneInfo(timezone_name))


def _weekly_off_week(
    week_start: date, day: str, now: datetime, timezone_name: str, *, approved: bool = False,
) -> dict:
    deadline = _weekly_off_deadline(week_start, timezone_name)
    index = WEEKDAYS.index(day) if day in WEEKDAYS else 6
    return {
        "week_start": week_start.isoformat(),
        "friday": (week_start + timedelta(days=4)).isoformat(),
        "sunday": (week_start + timedelta(days=6)).isoformat(),
        "day": day,
        "off_date": (week_start + timedelta(days=index)).isoformat(),
        "deadline": deadline.isoformat(),
        # A rotational weekly off the manager approved is settled: the picker
        # may not change it.
        "approved": approved,
        "locked": approved or now > deadline,
    }


@router.get("/weekly-off")
def get_weekly_off(user: dict = Depends(current_user)) -> dict:
    """This week and the next few: which day is off, and whether it can still change."""
    employee_id = _employee_id(user)
    timezone_name = service().policy.timezone_name
    now = datetime.now(timezone.utc)
    today = local_day(now, timezone_name)
    first = today - timedelta(days=today.weekday())
    weeks = [first + timedelta(weeks=offset) for offset in range(WEEKLY_OFF_WEEKS_AHEAD)]
    chosen = AttendanceRepository().weekly_off_rows(employee_id, weeks)
    return {
        "employee_id": employee_id,
        "default": "sunday",
        "weeks": [
            _weekly_off_week(
                week,
                (chosen.get(week.isoformat()) or {}).get("day", "sunday"),
                now,
                timezone_name,
                approved=(chosen.get(week.isoformat()) or {}).get("source") == "request",
            )
            for week in weeks
        ],
    }


@router.put("/weekly-off")
def update_weekly_off(payload: WeeklyOffRequest, user: dict = Depends(current_user)) -> dict:
    """Choose Friday (or go back to Sunday) for one week, before Thursday 11:59 PM."""
    if user.get("role") == ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Weekly off is chosen by staff and managers")
    employee_id = _employee_id(user)
    week_start = payload.week_start - timedelta(days=payload.week_start.weekday())
    timezone_name = service().policy.timezone_name
    now = datetime.now(timezone.utc)
    if now > _weekly_off_deadline(week_start, timezone_name):
        raise HTTPException(
            status_code=409,
            detail="The weekly off for this week was due by Thursday 11:59 PM and can no longer be changed",
        )
    repository = AttendanceRepository()
    current = repository.weekly_off_rows(employee_id, [week_start]).get(week_start.isoformat())
    if current and current.get("source") == "request":
        raise HTTPException(
            status_code=409,
            detail="Your manager approved a weekly off for this week; it can no longer be changed here",
        )
    saved = repository.set_weekly_off_choice(employee_id, week_start, payload.day)
    return {
        "status": "saved",
        "employee_id": employee_id,
        "week": _weekly_off_week(week_start, saved["day"], now, timezone_name),
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


def _notify_approvers(employee_id: str, request_type: str, row: dict, title: str, what: str) -> None:
    """Tell everyone with a stage to decide about a new request.

    Staff requests go to the manager of their branch (Noorul at Royapettah,
    Rafi at Mount Road — see `app.branches`); a manager's own request goes to
    the administrators. Paid leave, LOP and Extra OT also go to the finance
    manager (see `app.attendance.approvals`).
    """
    employee_record = users.get(employee_id)
    recipients = approvals.approvers_for(request_type, row, employee_record, users)
    try:
        notification_repo = NotificationRepository() if recipients else None
        for approver in recipients:
            notification_repo.record(
                approver.id,
                type=ATTENDANCE_REQUEST,
                title=title,
                message=f"{employee_record.name if employee_record else employee_id} requested {what}.",
            )
    except Exception as exc:  # The request is durable even if its alert cannot be written.
        log.warning("Attendance request %s could not notify its approvers: %s", row.get("id"), exc)


_STAGE_LABELS = {approvals.MANAGER_STAGE: "manager", approvals.FINANCE_STAGE: "finance manager"}


def _record_decision(approver: dict, request_type: str, pending: dict, approved: bool, reason: str) -> tuple[dict, dict | None]:
    """Record this approver's stages of a pending request.

    When the decision settles the request — a rejection (one is final), or the
    last approval it was waiting for — nothing is written here; the stage
    records are returned for the caller to store with the final status, so a
    request can never be left marked approved at every stage yet still pending.
    Otherwise the approval is saved and ``None`` returned. Refuses anyone who
    has no stage of this request left to decide.
    """
    viewer = users.get(approver["id"])
    requester = users.get(pending["employee_id"])
    mine = approvals.stages_for(viewer, request_type, pending, requester, users)
    if not mine:
        if requester is not None and requester.role in MANAGER_ROLES:
            detail = "Administrator approval is required for a manager request"
        elif requester is not None:
            detail = f"This request belongs to the {branch_of(requester)} branch manager"
        else:
            detail = "You cannot decide this request"
        raise HTTPException(status_code=403, detail=detail)
    waiting = [stage for stage in approvals.outstanding(request_type, pending) if stage in mine]
    if not waiting:
        others = ", ".join(_STAGE_LABELS[stage] for stage in approvals.outstanding(request_type, pending))
        raise HTTPException(status_code=409, detail=f"You have already approved this; it is waiting for the {others}")
    record = approvals.stage_record(viewer, approved, reason)
    stages = {stage: record for stage in waiting}
    if not approved or set(approvals.outstanding(request_type, pending)) <= set(waiting):
        return pending, stages
    repository = AttendanceRepository()
    save = (
        repository.record_permission_approvals
        if request_type == approvals.PERMISSION
        else repository.record_extra_ot_approvals
    )
    saved = save(pending["id"], stages)
    if not saved:
        raise HTTPException(status_code=409, detail="Pending request not found")
    if approvals.outstanding(request_type, saved):
        _notify_partial(saved, request_type, viewer)
        return saved, None
    # The other approver signed at the same moment: this write completed it.
    return saved, {}


def _notify_partial(row: dict, request_type: str, viewer) -> None:
    """Tell the requester one approval is in and who still has to sign."""
    remaining = ", ".join(_STAGE_LABELS[stage] for stage in approvals.outstanding(request_type, row))
    what = "Extra OT" if request_type == approvals.EXTRA_OT else row.get("kind", "request").replace("_", " ")
    try:
        NotificationRepository().record(
            row["employee_id"], type=ATTENDANCE_REQUEST,
            title="Request partly approved",
            message=(
                f"Your {what} for {row['attendance_date']} was approved by "
                f"{viewer.name or viewer.email}; it is now waiting for the {remaining}."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Partial approval of %s could not notify the requester: %s", row.get("id"), exc)


def _for_viewer(user: dict, request_type: str, rows: list[dict], team_ids) -> list[dict]:
    """Rows this caller may see, each with its approval state for them.

    A finance manager sees their own branch as any manager does, plus the
    money requests of every other branch — and nothing else from those.
    """
    viewer = users.get(user["id"])
    team = {team_ids} if isinstance(team_ids, str) else set(team_ids)
    people: dict = {}
    visible = []
    for row in rows:
        if row["employee_id"] not in team and approvals.FINANCE_STAGE not in approvals.required_stages(request_type, row):
            continue
        if row["employee_id"] not in people:
            people[row["employee_id"]] = users.get(row["employee_id"])
        visible.append(approvals.describe(viewer, request_type, row, people[row["employee_id"]], users))
    return visible


def _branch_staff(user: dict) -> list:
    """The staff a manager answers for: their own branch only."""
    staff = (
        users.list_staff(include_inactive=False)
        if hasattr(users, "list_staff")
        else users.list_assignable_staff()
    )
    manager = users.get(user["id"])
    return [member for member in staff if manages(manager, member, users)]


# --------------------------------------------------------------------------- #
#  Leave cover (see `app.attendance.cover`)
# --------------------------------------------------------------------------- #
def _cover_colleagues(employee_id: str) -> list:
    """Who may be asked to cover: active colleagues of the same branch.

    Same branch because candidates are allocated by desk, and the other
    branch's desk does not work these destinations. Anyone active is offered
    if the branch has nobody else. A manager's cover is one of their staff:
    the person who will actually work the queue.
    """
    me = users.get(employee_id)
    colleagues = [
        member for member in (
            users.list_employees(include_inactive=False)
            if hasattr(users, "list_employees") else users.list_assignable_staff()
        )
        if member.id != employee_id and getattr(member, "active", True) and on_attendance(member)
    ]
    if me is not None and me.role in MANAGER_ROLES:
        colleagues = [member for member in colleagues if member.role == STAFF_ROLE]
    if me is None:
        return colleagues
    own = [member for member in colleagues if same_branch(member, me)]
    return own or colleagues


def _checked_cover(employee_id: str, cover_id: str) -> dict:
    """The stored cover fields, after checking the colleague may be asked."""
    if cover_id == employee_id:
        raise HTTPException(status_code=422, detail="You cannot cover your own leave")
    cover = next((member for member in _cover_colleagues(employee_id) if member.id == cover_id), None)
    if cover is None:
        raise HTTPException(status_code=422, detail="Choose an active colleague from your branch as cover")
    return {"cover_employee_id": cover.id, "cover_employee_name": cover.name or cover.email}


def _ask_cover(permission: dict) -> None:
    requester = users.get(permission["employee_id"])
    name = getattr(requester, "name", None) or permission["employee_id"]
    try:
        NotificationRepository().record(
            permission["cover_employee_id"], type=ATTENDANCE_REQUEST,
            title="Leave cover request",
            message=(
                f"{name} is on leave on {permission['attendance_date']} and asked you to "
                "handle their work that day. Accept or decline in Attendance."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Cover request %s could not notify the cover: %s", permission.get("id"), exc)


def _try_start_covers() -> None:
    """Start a handover now if the leave is today and everything is in place."""
    try:
        from app.attendance.cover import start_due_covers

        start_due_covers(AttendanceRepository(), users=users)
    except Exception as exc:  # noqa: BLE001 - the beat sweep will retry
        log.warning("Immediate leave-cover handover failed: %s", exc)


@router.get("/cover-colleagues")
def cover_colleagues(user: dict = Depends(current_user)) -> dict:
    """The colleagues this user may ask to cover a leave day."""
    items = [
        {"id": member.id, "name": member.name or member.email, "branch": branch_of(member)}
        for member in _cover_colleagues(user["id"])
    ]
    return {"items": items}


@router.get("/cover-requests")
def my_cover_requests(user: dict = Depends(current_user)) -> dict:
    """Leave days colleagues have asked this user to cover, recent and upcoming."""
    from datetime import timedelta

    since = date.today() - timedelta(days=7)
    items = AttendanceRepository().cover_requests_for(user["id"], since)
    for item in items:
        requester = users.get(item["employee_id"])
        item["employee_name"] = getattr(requester, "name", None) or item["employee_id"]
    return {"items": items, "count": len(items)}


@router.post("/permissions/{permission_id}/cover-response")
def respond_to_cover(permission_id: str, payload: CoverResponse, user: dict = Depends(current_user)) -> dict:
    """The named colleague accepts or declines covering the leave day."""
    repository = AttendanceRepository()
    result = repository.respond_to_cover(permission_id, user["id"], payload.accepted, payload.note)
    if not result:
        raise HTTPException(status_code=409, detail="No open cover request for you on this leave")
    who = users.get(user["id"])
    try:
        NotificationRepository().record(
            result["employee_id"], type=ATTENDANCE_REQUEST,
            title="Leave cover accepted" if payload.accepted else "Leave cover declined",
            message=(
                f"{getattr(who, 'name', None) or 'Your colleague'} "
                f"{'will' if payload.accepted else 'cannot'} cover your work on {result['attendance_date']}."
                + ("" if payload.accepted else " You can ask someone else.")
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Cover response %s could not notify the requester: %s", permission_id, exc)
    if payload.accepted:
        # Only now does the leave reach the manager (or, for a manager's own
        # leave, the super admins).
        _notify_approvers(
            result["employee_id"], approvals.PERMISSION, result, "Attendance request",
            f"{result['kind'].replace('_', ' ')} for {result['attendance_date']}"
            f" (cover accepted by {result.get('cover_employee_name') or 'a colleague'})",
        )
    return {"status": result["cover_status"], "permission": result}


@router.post("/permissions/{permission_id}/cover")
def nominate_cover(permission_id: str, payload: CoverNomination, user: dict = Depends(current_user)) -> dict:
    """Ask a colleague to cover an existing leave request (e.g. after a decline)."""
    repository = AttendanceRepository()
    existing = repository.permission(permission_id)
    if not existing or existing.get("employee_id") != user["id"]:
        raise HTTPException(status_code=404, detail="Leave request not found")
    if existing.get("kind") not in LEAVE_KINDS:
        raise HTTPException(status_code=422, detail="Only a leave day can have a cover")
    cover = _checked_cover(user["id"], payload.cover_employee_id)
    result = repository.nominate_cover(permission_id, user["id"], cover)
    if not result:
        raise HTTPException(status_code=409, detail="This leave is no longer waiting for a cover")
    _ask_cover(result)
    return {"status": "requested", "permission": result}


@router.post("/permissions", status_code=201)
def request_permission(payload: PermissionRequest, user: dict = Depends(current_user)) -> dict:
    """Ask for a permission or a leave day.

    Leave goes to the cover first: the colleague named to do the requester's
    work must accept before the manager (or, for a manager's own leave, the
    super admins) hears about it. A cover is required whenever the requester
    has anyone who could be asked.
    """
    employee = _employee_id(user, payload.employee_id)
    cover = None
    if payload.kind in LEAVE_KINDS:
        if payload.cover_employee_id:
            cover = _checked_cover(employee, payload.cover_employee_id)
        elif _cover_colleagues(employee):
            raise HTTPException(
                status_code=422,
                detail="Choose a colleague to handle your work on your leave day",
            )
    permission = _conflict(lambda: service().request_permission(employee, payload))
    if cover:
        permission = AttendanceRepository().nominate_cover(permission["id"], employee, cover) or permission
        _ask_cover(permission)
        return {"status": "awaiting_cover", "permission": permission}
    _notify_approvers(
        employee, approvals.PERMISSION, permission, "Attendance request",
        f"{payload.kind.replace('_', ' ')} for {payload.attendance_date}",
    )
    return {"status": "pending", "permission": permission}


@router.post("/extra-ot", status_code=201)
def request_extra_ot(payload: ExtraOTRequest, user: dict = Depends(current_user)) -> dict:
    employee = _employee_id(user, payload.employee_id)
    request = _conflict(lambda: service().request_extra_ot(employee, payload))
    _notify_approvers(
        employee, approvals.EXTRA_OT, request, "Extra OT request",
        f"{payload.requested_minutes} min Extra OT for {payload.attendance_date}",
    )
    return {"status": "pending", "request": request}


@router.post("/extra-ot/{request_id}/decision")
def decide_extra_ot(request_id: str, payload: ExtraOTDecision, approver: dict = Depends(require_attendance_manager)) -> dict:
    """One approver's answer. Extra OT needs the manager and the finance manager."""
    pending = AttendanceRepository().extra_ot(request_id)
    if not pending or pending.get("status") != "pending":
        raise HTTPException(status_code=409, detail="pending Extra OT request not found")
    request, final = _record_decision(approver, approvals.EXTRA_OT, pending, payload.approved, payload.reason)
    if final is not None:
        request = _conflict(lambda: service().decide_extra_ot(request_id, payload, approver["id"], approvals=final))
    _for_viewer(approver, approvals.EXTRA_OT, [request], request["employee_id"])
    return {"status": "decided" if final is not None else "pending", "request": request}


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
    if user.get("role") in _APPROVER_ROLES and not employee_id:
        if user.get("role") == ADMIN_ROLE:
            members = (
                users.list_employees(include_inactive=False)
                if hasattr(users, "list_employees")
                else users.list_assignable_staff()
            )
        else:
            members = _branch_staff(user)
        return [member.id for member in members if on_attendance(member)]
    return _employee_id(user, employee_id)


def _finance_scope(user: dict, employee_id: str | None, team):
    """Whose requests to read: the team, widened to every branch for a finance manager.

    `_for_viewer` then drops the other branches' rows that are not money
    requests, so the widening never shows a finance manager anything else.
    """
    if user.get("role") != FINANCE_MANAGER_ROLE or employee_id:
        return team
    everyone = (
        users.list_employees(include_inactive=False)
        if hasattr(users, "list_employees")
        else users.list_assignable_staff()
    )
    return [member.id for member in everyone if member.id != user["id"] and on_attendance(member)]


@router.get("/permissions")
def list_permissions(
    year: int,
    month: int,
    employee_id: str | None = Query(default=None),
    user: dict = Depends(current_user),
) -> dict:
    """Own history for employees; staff approvals for managers; all for admins."""
    start, end = _period(year, month)
    team = _visible_employee_ids(user, employee_id)
    items = _for_viewer(
        user, approvals.PERMISSION,
        AttendanceRepository().permissions_for_period(_finance_scope(user, employee_id, team), start, end),
        team,
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
    team = _visible_employee_ids(user, employee_id)
    items = _for_viewer(
        user, approvals.EXTRA_OT,
        AttendanceRepository().extra_ot_for_period(_finance_scope(user, employee_id, team), start, end),
        team,
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
    permission, final = _record_decision(admin, approvals.PERMISSION, pending, payload.approved, payload.reason)
    if final is not None:
        permission = _conflict(lambda: attendance.decide_permission(permission_id, payload, admin["id"], approvals=final))
    if permission.get("status") == "approved" and permission.get("cover_status") == "accepted":
        _try_start_covers()
    _for_viewer(admin, approvals.PERMISSION, [permission], permission["employee_id"])
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


@router.get("/shifts/{employee_id}")
def employee_work_timing(employee_id: str, admin: dict = Depends(require_attendance_manager)) -> dict:
    """The hours an employee works today, and every change of their timing."""
    employee_id = _employee_id(admin, employee_id)
    attendance = service()
    today = local_day(datetime.now(timezone.utc), attendance.policy.timezone_name)
    current = attendance._shift(employee_id, today)
    return {
        "employee_id": employee_id,
        "current": current.model_dump(mode="json"),
        "history": attendance.repository.recurring_shifts(employee_id),
    }


@router.post("/calendar", status_code=201)
def set_calendar_day(payload: CalendarDayRequest, admin: dict = Depends(require_attendance_manager)) -> dict:
    _employee_id(admin, payload.employee_id)
    return {"status": "recorded", "calendar_day": service().set_calendar_day(payload, admin["id"])}
