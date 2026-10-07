"""Application service enforcing attendance workflow invariants."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import calendar

from app.attendance.engine import AttendancePolicy, calculate_day, local_day, shift_bounds
from app.attendance.models import AttendanceStatus, AdjustmentRequest, CalendarDayRequest, DutyPlanRequest, ExtraOTDecision, ExtraOTRequest, PermissionDecision, PermissionRequest, PunchRequest, Shift, ShiftAssignmentRequest
from app.attendance.office_calendar import festival_holiday, standard_shift
from app.attendance.repository import AttendanceRepository
from app.holidays import government_holiday

#: Permission kinds that take the whole day off, and so may name a cover.
LEAVE_KINDS = frozenset({"paid_leave", "unpaid_leave"})

#: `date.weekday()` order. A week's stored weekly-off day is one of these.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class AttendanceError(ValueError):
    pass


#: Calendar entries a duty plan may override. Rostering somebody for a date
#: overrides a non-working marker on it, because that is what rostering means.
#: Approved leave is not in this set: leave the employee was granted outranks a
#: roster, and silently working through it would be a decision nobody made.
_OVERRIDABLE_BY_DUTY_PLAN = frozenset({AttendanceStatus.WEEKLY_OFF, AttendanceStatus.HOLIDAY})


def _non_working_status(
    calendar_day: dict | None, *, planned_duty: bool, weekly_off: bool
) -> AttendanceStatus | None:
    """Whether the day carries no duty, and if so under which status."""
    if calendar_day:
        declared = AttendanceStatus(calendar_day["status"])
        if planned_duty and declared in _OVERRIDABLE_BY_DUTY_PLAN:
            return None
        return declared
    return AttendanceStatus.WEEKLY_OFF if weekly_off else None


class AttendanceService:
    def __init__(self, repository: AttendanceRepository, policy: AttendancePolicy | None = None):
        self.repository = repository
        self.policy = policy or AttendancePolicy()

    def _shift(self, employee_id: str, day: date, assignment: dict | None = None) -> Shift:
        """An assigned shift, else the employee's standard office hours."""
        assignment = assignment or self.repository.shift_for_day(employee_id, day)
        if assignment:
            return Shift.model_validate(assignment["shift"])
        email = getattr(self.repository, "employee_email", lambda *_args: None)(employee_id)
        return standard_shift(email, day)

    def punch(self, employee_id: str, request: PunchRequest, *, allow_recorded_time: bool = False) -> tuple[dict, bool]:
        delivered = self.repository.event_by_idempotency_key(request.idempotency_key)
        if delivered:
            if delivered.get("employee_id") != employee_id or delivered.get("action") != request.action:
                raise AttendanceError("idempotency key belongs to a different attendance event")
            return delivered, False
        occurred = request.occurred_at if allow_recorded_time and request.occurred_at else datetime.now(timezone.utc)
        if occurred.tzinfo is None or occurred.utcoffset() is None:
            raise AttendanceError("occurred_at must include a timezone")
        occurred = occurred.astimezone(timezone.utc)
        day = local_day(occurred, self.policy.timezone_name)
        # Hours are flexible, so a punch is never refused for its time of day.
        # The day calculation decides what the time is worth.
        existing = self.repository.events_for_day(employee_id, day)
        if request.action == "check_in" and any(row["action"] == "check_in" for row in existing):
            # A genuinely duplicated delivery is resolved by append_event below;
            # a different key is a conflicting second business event.
            duplicate = next((row for row in existing if row.get("idempotency_key") == request.idempotency_key), None)
            if duplicate:
                return duplicate, False
            raise AttendanceError("check-in already recorded for this attendance date")
        if request.action == "check_out" and not any(row["action"] == "check_in" for row in existing):
            raise AttendanceError("check-in is required before check-out")
        if request.action == "check_out" and any(row["action"] == "check_out" for row in existing):
            duplicate = next((row for row in existing if row.get("idempotency_key") == request.idempotency_key), None)
            if duplicate:
                return duplicate, False
            raise AttendanceError("check-out already recorded for this attendance date")
        event, created = self.repository.append_event({
            "employee_id": employee_id,
            "action": request.action,
            "occurred_at": occurred,
            "local_date": day.isoformat(),
            "source": request.source,
            "idempotency_key": request.idempotency_key,
            "evidence": request.evidence.model_dump(),
        })
        if event.get("employee_id") != employee_id or event.get("action") != request.action:
            raise AttendanceError("idempotency key belongs to a different attendance event")
        return event, created

    def day(self, employee_id: str, day: date, *, shift: Shift | None = None, now: datetime | None = None) -> dict:
        assignment = self.repository.shift_for_day(employee_id, day)
        if shift is None:
            shift = self._shift(employee_id, day, assignment)
        calendar_day = self.repository.calendar_day(employee_id, day)
        # Government holidays declared in Data Management come first, then the
        # built-in festival list. Either way the day is "H": no attendance is
        # owed and payroll deducts nothing for it.
        holiday = government_holiday(day) or festival_holiday(day)
        if calendar_day is None and holiday:
            calendar_day = {"status": AttendanceStatus.HOLIDAY, "reason": holiday}
        # One weekly off per Monday-to-Sunday week: Sunday, unless another day
        # was taken for that week — Friday from the self-service picker, or any
        # day through an approved rotational weekly-off request. Either way that
        # day is off and the week's Sunday is a normal working day.
        week_start = day - timedelta(days=day.weekday())
        weekly_off_day = getattr(
            self.repository, "weekly_off_choice", lambda *_args: None,
        )(employee_id, week_start) or "sunday"
        weekly_off_index = WEEKDAYS.index(weekly_off_day) if weekly_off_day in WEEKDAYS else 6
        #
        # A *duty plan* — one date, explicitly rostered — is what turns a weekly
        # off into a working day. A rolling shift assignment is not: it says
        # which hours apply from a date onwards and nothing about which days are
        # worked, so treating it as a plan made every Sunday after the first
        # rostered one a scheduled day the employee was then marked absent for.
        planned_duty = bool(assignment and assignment.get("kind") == "planned_duty")
        is_weekly_off = day.weekday() == weekly_off_index and not planned_duty
        punches = self.repository.effective_punches_for_day(employee_id, day)
        permissions = self.repository.approved_permissions(employee_id, day)
        adjustments = self.repository.adjustments_for_day(employee_id, day)
        recovered = sum(int(row.get("recovered_minutes", 0)) for row in adjustments if row.get("kind") == "time_recovery")
        status_override = next((row.get("status") for row in reversed(adjustments) if row.get("kind") == "status_override"), None)
        result = calculate_day(
            day, punches, shift=shift, now=now, policy=self.policy,
            approved_permissions=permissions,
            recovered_minutes=recovered,
            non_working_status=_non_working_status(
                calendar_day, planned_duty=planned_duty,
                weekly_off=is_weekly_off,
            ),
        )
        if status_override:
            result["status"] = str(status_override)
            result["status_overridden"] = True
        result["employee_id"] = employee_id
        result["raw_event_ids"] = [row.get("id") for row in punches if row.get("id")]
        result["shift_assignment_id"] = assignment.get("id") if assignment else None
        return result

    def request_permission(self, employee_id: str, request: PermissionRequest) -> dict:
        now = datetime.now(timezone.utc)
        shift = self._shift(employee_id, request.attendance_date)
        start, end = shift_bounds(request.attendance_date, shift, self.policy.timezone_name)
        if request.kind in {"late", "early_check_in", "work_from_home"} and now >= start:
            raise AttendanceError(f"{request.kind} permission must be requested before shift start")
        if request.kind == "early_exit":
            punches = self.repository.events_for_day(employee_id, request.attendance_date)
            if now >= end or any(row["action"] == "check_out" for row in punches):
                raise AttendanceError("early-exit permission must be requested before leaving")
        if request.kind == "weekly_off":
            self._check_weekly_off_request(employee_id, request.attendance_date)
        record = {
            **request.model_dump(exclude={"employee_id", "cover_employee_id"}),
            "attendance_date": request.attendance_date.isoformat(),
            "employee_id": employee_id,
        }
        if request.cover_employee_id and request.kind in LEAVE_KINDS:
            record.update(cover_employee_id=request.cover_employee_id, cover_status="requested",
                          status="awaiting_cover")
        return self.repository.create_permission(record)

    def _check_weekly_off_request(self, employee_id: str, day: date) -> None:
        """One rotational weekly off per week, and never on the Sunday it replaces."""
        if day.weekday() == 6:
            raise AttendanceError("Sunday is already the weekly off; choose the day you want instead of Sunday")
        week_start = day - timedelta(days=day.weekday())
        existing = self.repository.permissions_for_period(employee_id, week_start, week_start + timedelta(days=6))
        if any(row.get("kind") == "weekly_off" and row.get("status") in {"pending", "approved"} for row in existing):
            raise AttendanceError("a weekly off has already been requested for this week")

    def decide_permission(self, permission_id: str, decision: PermissionDecision, approver_id: str) -> dict:
        pending = self.repository.permission(permission_id)
        if not pending or pending.get("status") != "pending":
            raise AttendanceError("pending permission not found")
        if decision.approved and pending.get("kind") == "work_from_home":
            day = date.fromisoformat(pending["attendance_date"])
            shift = self._shift(pending["employee_id"], day)
            start, _ = shift_bounds(day, shift, self.policy.timezone_name)
            if datetime.now(timezone.utc) >= start:
                raise AttendanceError("work-from-home must be approved before shift start")
        result = self.repository.decide_permission(permission_id, {**decision.model_dump(), "decided_by": approver_id, "decided_at": datetime.now(timezone.utc)})
        if not result:
            raise AttendanceError("pending permission not found")
        if decision.approved and pending.get("kind") == "weekly_off":
            # The approved day becomes this week's weekly off, which is what
            # turns the week's Sunday back into a working day.
            day = date.fromisoformat(pending["attendance_date"])
            self.repository.set_weekly_off_choice(
                pending["employee_id"], day - timedelta(days=day.weekday()), WEEKDAYS[day.weekday()],
                permission_id=permission_id,
            )
            result["weekly_off_day"] = WEEKDAYS[day.weekday()]
        if decision.approved and pending.get("kind") in {"paid_leave", "unpaid_leave"}:
            calendar_day = self.set_calendar_day(
                CalendarDayRequest(
                    employee_id=pending["employee_id"],
                    attendance_date=date.fromisoformat(pending["attendance_date"]),
                    status="PL" if pending["kind"] == "paid_leave" else "UL",
                    reason=pending.get("reason") or decision.reason,
                ),
                approver_id,
            )
            result["calendar_status"] = calendar_day["status"]
            result["converted_from_paid_leave"] = calendar_day.get("converted_from_paid_leave", False)
        return result

    def request_extra_ot(self, employee_id: str, request: ExtraOTRequest) -> dict:
        return self.repository.create_extra_ot({**request.model_dump(exclude={"employee_id"}),
                                                "attendance_date": request.attendance_date.isoformat(),
                                                "employee_id": employee_id})

    def decide_extra_ot(self, request_id: str, decision: ExtraOTDecision, approver_id: str) -> dict:
        result = self.repository.decide_extra_ot(request_id, {**decision.model_dump(),
                                                               "decided_by": approver_id,
                                                               "decided_at": datetime.now(timezone.utc)})
        if not result:
            raise AttendanceError("pending Extra OT request not found")
        return result

    def adjust(self, request: AdjustmentRequest, approver_id: str) -> dict:
        if request.kind == "add_punch" and (not request.action or not request.occurred_at):
            raise AttendanceError("add_punch requires action and occurred_at")
        if request.kind == "time_recovery" and request.recovered_minutes <= 0:
            raise AttendanceError("time_recovery requires recovered_minutes")
        if request.kind == "status_override" and not request.status:
            raise AttendanceError("status_override requires status")
        values = request.model_dump(exclude={"approved_by"})
        values["attendance_date"] = request.attendance_date.isoformat()
        if values.get("occurred_at"):
            timestamp = values["occurred_at"]
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise AttendanceError("occurred_at must include a timezone")
            values["occurred_at"] = timestamp.astimezone(timezone.utc)
        values.update(approved_by=approver_id, approved_at=datetime.now(timezone.utc))
        return self.repository.append_adjustment(values)

    def assign_shift(self, request: ShiftAssignmentRequest, approver_id: str) -> dict:
        return self.repository.assign_shift({
            **request.model_dump(exclude={"effective_from"}),
            "effective_from": request.effective_from.isoformat(),
            "assigned_by": approver_id,
        })

    def plan_duty(self, request: DutyPlanRequest, approver_id: str) -> dict:
        """Roster one date as a working day, replacing any existing plan for it.

        Stored as a shift assignment so a rostered day carries its own hours,
        and so the day calculation has one place to look for "which shift does
        this date run to". Re-planning the same date supersedes the previous
        plan rather than stacking a second row behind it.
        """
        existing = self.repository.duty_plan_for_day(request.employee_id, request.attendance_date)
        if existing:
            self.repository.delete_duty_plan(existing["id"])
        return self.repository.assign_shift({
            "employee_id": request.employee_id,
            "effective_from": request.attendance_date.isoformat(),
            "shift": request.shift.model_dump(),
            "reason": request.reason,
            "kind": "planned_duty",
            "assigned_by": approver_id,
        })

    def remove_duty_plan(self, plan_id: str) -> bool:
        """Hand a rostered date back to the normal weekly-off pattern."""
        return self.repository.delete_duty_plan(plan_id) is not None

    def duty_plans(self, employee_ids, start: date, end: date) -> list[dict]:
        return self.repository.duty_plans_for_period(employee_ids, start, end)

    def set_calendar_day(self, request: CalendarDayRequest, approver_id: str) -> dict:
        status = request.status
        converted_from_paid_leave = False
        if request.status == "PL":
            start = request.attendance_date.replace(day=1)
            end = request.attendance_date.replace(
                day=calendar.monthrange(request.attendance_date.year, request.attendance_date.month)[1]
            )
            existing = getattr(self.repository, "calendar_days_for_period", lambda *_args: [])(
                request.employee_id, start, end
            )
            if any(row.get("status") == "PL" and row.get("attendance_date") != request.attendance_date.isoformat() for row in existing):
                status = AttendanceStatus.UNPAID_LEAVE
                converted_from_paid_leave = True
        return self.repository.set_calendar_day({
            **request.model_dump(exclude={"attendance_date"}),
            "attendance_date": request.attendance_date.isoformat(),
            "status": str(status),
            "converted_from_paid_leave": converted_from_paid_leave,
            "recorded_by": approver_id,
        })
