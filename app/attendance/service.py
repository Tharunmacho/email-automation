"""Application service enforcing attendance workflow invariants."""
from __future__ import annotations

from datetime import date, datetime, timezone
import calendar

from app.attendance.engine import AttendancePolicy, calculate_day, local_day, shift_bounds
from app.attendance.models import AttendanceStatus, AdjustmentRequest, CalendarDayRequest, PermissionDecision, PermissionRequest, PunchRequest, Shift, ShiftAssignmentRequest
from app.attendance.repository import AttendanceRepository


class AttendanceError(ValueError):
    pass


class AttendanceService:
    def __init__(self, repository: AttendanceRepository, policy: AttendancePolicy | None = None):
        self.repository = repository
        self.policy = policy or AttendancePolicy()

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
        if shift is None and assignment:
            shift = Shift.model_validate(assignment["shift"])
        shift = shift or Shift()
        calendar_day = self.repository.calendar_day(employee_id, day)
        employee_policy = getattr(self.repository, "employee_policy", lambda _id: {})(employee_id)
        is_sunday = day.weekday() == 6
        is_rotational_friday = (
            day.weekday() == 4
            and employee_policy.get("weekly_off_pattern") == "sunday_alternate_friday"
            and day.isocalendar().week % 2 == int(employee_policy.get("alternate_friday_parity", 0))
        )
        punches = self.repository.effective_punches_for_day(employee_id, day)
        permissions = self.repository.approved_permissions(employee_id, day)
        adjustments = self.repository.adjustments_for_day(employee_id, day)
        recovered = sum(int(row.get("recovered_minutes", 0)) for row in adjustments if row.get("kind") == "time_recovery")
        status_override = next((row.get("status") for row in reversed(adjustments) if row.get("kind") == "status_override"), None)
        result = calculate_day(
            day, punches, shift=shift, now=now, policy=self.policy,
            approved_permissions=permissions,
            recovered_minutes=recovered,
            non_working_status=(
                AttendanceStatus(calendar_day["status"])
                if calendar_day
                else AttendanceStatus.WEEKLY_OFF if is_sunday or is_rotational_friday else None
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
        assignment = self.repository.shift_for_day(employee_id, request.attendance_date)
        shift = Shift.model_validate(assignment["shift"]) if assignment else Shift()
        start, end = shift_bounds(request.attendance_date, shift, self.policy.timezone_name)
        if request.kind in {"late", "work_from_home"} and now >= start:
            raise AttendanceError(f"{request.kind} permission must be requested before shift start")
        if request.kind == "early_exit":
            punches = self.repository.events_for_day(employee_id, request.attendance_date)
            if now >= end or any(row["action"] == "check_out" for row in punches):
                raise AttendanceError("early-exit permission must be requested before leaving")
        return self.repository.create_permission({
            **request.model_dump(exclude={"employee_id"}),
            "attendance_date": request.attendance_date.isoformat(),
            "employee_id": employee_id,
        })

    def decide_permission(self, permission_id: str, decision: PermissionDecision, approver_id: str) -> dict:
        pending = self.repository.permission(permission_id)
        if not pending or pending.get("status") != "pending":
            raise AttendanceError("pending permission not found")
        if decision.approved and pending.get("kind") == "work_from_home":
            day = date.fromisoformat(pending["attendance_date"])
            assignment = self.repository.shift_for_day(pending["employee_id"], day)
            shift = Shift.model_validate(assignment["shift"]) if assignment else Shift()
            start, _ = shift_bounds(day, shift, self.policy.timezone_name)
            if datetime.now(timezone.utc) >= start:
                raise AttendanceError("work-from-home must be approved before shift start")
        result = self.repository.decide_permission(permission_id, {**decision.model_dump(), "decided_by": approver_id, "decided_at": datetime.now(timezone.utc)})
        if not result:
            raise AttendanceError("pending permission not found")
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
