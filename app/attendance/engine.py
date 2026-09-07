"""Pure attendance policy calculations.

The engine has no database or web dependencies.  Raw punches are inputs and
are never changed; corrections are supplied separately as adjustments.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from app.attendance.models import AttendanceStatus, Shift

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class AttendancePolicy:
    # Shared automatically across late arrivals and early departures each month.
    monthly_paid_minutes: int = 60
    monthly_paid_occasions: int = 2
    warning_minutes: int = 45
    missing_punch_deadline_days: int = 2
    timezone_name: str = "Asia/Kolkata"


def as_utc(value: datetime) -> datetime:
    """Return an aware UTC timestamp; reject ambiguous naive input."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("attendance timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def local_day(value: datetime, timezone_name: str = "Asia/Kolkata") -> date:
    return as_utc(value).astimezone(ZoneInfo(timezone_name)).date()


def shift_bounds(day: date, shift: Shift, timezone_name: str = "Asia/Kolkata") -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, shift.start, zone)
    end = datetime.combine(day, shift.end, zone)
    if end <= start:  # overnight shift
        end += timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _minutes(delta: timedelta) -> int:
    # Attendance deductions are whole elapsed minutes; partial minutes do not
    # silently become an additional minute of salary deduction.
    return max(0, int(delta.total_seconds() // 60))


def calculate_day(
    day: date,
    punches: Iterable[Mapping],
    *,
    shift: Shift | None = None,
    approved_kind: str | None = None,
    approved_minutes: int = 0,
    approved_permissions: Iterable[Mapping] | None = None,
    recovered_minutes: int = 0,
    non_working_status: AttendanceStatus | None = None,
    now: datetime | None = None,
    policy: AttendancePolicy | None = None,
) -> dict:
    """Calculate actual absence minutes for one scheduled day."""
    policy = policy or AttendancePolicy()
    shift = shift or Shift()
    now = as_utc(now or datetime.now(timezone.utc))
    start, end = shift_bounds(day, shift, policy.timezone_name)

    if non_working_status:
        scheduled = _minutes(end - start) - shift.break_minutes
        return _result(
            day,
            non_working_status,
            start,
            end,
            unpaid_minutes=max(0, scheduled) if non_working_status == AttendanceStatus.UNPAID_LEAVE else 0,
        )
    permissions = list(approved_permissions or [])
    kinds = {str(item.get("kind")) for item in permissions}
    if approved_kind:
        permissions.append({"kind": approved_kind, "requested_minutes": approved_minutes})
        kinds.add(approved_kind)
    duty_kind = "official_duty" if "official_duty" in kinds else "work_from_home" if "work_from_home" in kinds else None
    if duty_kind:
        status = AttendanceStatus.OFFICIAL_DUTY if duty_kind == "official_duty" else AttendanceStatus.WORK_FROM_HOME
        return _result(day, status, start, end)

    ordered = sorted(
        ((as_utc(p["occurred_at"]), p["action"]) for p in punches),
        key=lambda item: item[0],
    )
    check_ins = [ts for ts, action in ordered if action == "check_in"]
    check_outs = [ts for ts, action in ordered if action == "check_out"]
    check_in = check_ins[0] if check_ins else None
    check_out = check_outs[-1] if check_outs else None

    if not check_in or not check_out:
        deadline = end + timedelta(days=policy.missing_punch_deadline_days)
        expired = now > deadline
        scheduled = _minutes(end - start) - shift.break_minutes
        return _result(
            day,
            AttendanceStatus.ABSENT if expired else AttendanceStatus.MISSING_PUNCH,
            start,
            end,
            check_in=check_in,
            check_out=check_out,
            unpaid_minutes=max(0, scheduled) if expired else 0,
            provisional=not expired,
            regularisation_deadline=deadline,
        )

    late = _minutes(check_in - start)
    early = _minutes(end - check_out)
    actual = late + early
    approved_occurrences: list[int] = []
    remaining_late, remaining_early = late, early
    for item in permissions:
        kind = item.get("kind")
        requested = max(0, int(item.get("requested_minutes", 0)))
        eligible = min(remaining_late, requested) if kind == "late" else min(remaining_early, requested) if kind == "early_exit" else 0
        if eligible:
            approved_occurrences.append(eligible)
            if kind == "late":
                remaining_late -= eligible
            else:
                remaining_early -= eligible
    approved_actual = min(actual, sum(approved_occurrences))
    unapproved = actual - approved_actual
    recovered = min(unapproved, max(0, recovered_minutes))
    unapproved -= recovered

    if actual == 0:
        status = AttendanceStatus.PRESENT
    elif approved_actual:
        status = AttendanceStatus.PAID_PERMISSION
    elif late and not early:
        status = AttendanceStatus.LATE
    elif early and not late:
        status = AttendanceStatus.EARLY_EXIT
    else:
        status = AttendanceStatus.LATE
    return _result(
        day, status, start, end, check_in=check_in, check_out=check_out,
        late_minutes=late, early_minutes=early, approved_actual_minutes=approved_actual,
        unapproved_minutes=unapproved, recovered_minutes=recovered,
        approved_occurrence_minutes=approved_occurrences,
        emergency_override=any(bool(item.get("emergency_override")) for item in permissions),
    )


def calculate_month(days: Iterable[dict], policy: AttendancePolicy | None = None) -> list[dict]:
    """Allocate the monthly 60-minute grace balance chronologically."""
    policy = policy or AttendancePolicy()
    remaining = policy.monthly_paid_minutes
    occasions = 0
    results: list[dict] = []
    for raw in sorted(days, key=lambda row: row["date"]):
        row = dict(raw)
        eligible = max(0, int(row.pop("approved_actual_minutes", 0)))
        occurrence_minutes = [max(0, int(value)) for value in row.pop("approved_occurrence_minutes", [])]
        if not occurrence_minutes and eligible:
            occurrence_minutes = [eligible]
        emergency = bool(row.pop("emergency_override", False))
        paid = 0
        for occurrence in occurrence_minutes:
            if not occurrence:
                continue
            if occasions < policy.monthly_paid_occasions or emergency:
                applied = min(occurrence, remaining)
                paid += applied
            else:
                applied = 0
            if applied:
                occasions += 1
                remaining -= applied
        excess = eligible - paid
        fixed_unpaid = max(0, int(row.get("unpaid_minutes", 0)))
        unapproved = max(0, int(row.get("unapproved_minutes", 0)))
        automatic_grace = min(unapproved, remaining)
        remaining -= automatic_grace
        row["grace_minutes_applied"] = automatic_grace
        row["approved_permission_minutes"] = paid
        row["paid_permission_minutes"] = paid + automatic_grace
        row["unpaid_minutes"] = fixed_unpaid + unapproved - automatic_grace + excess
        row["permission_occasions_used"] = occasions
        row["permission_minutes_used"] = policy.monthly_paid_minutes - remaining
        row["permission_minutes_remaining"] = remaining
        row["permission_warning"] = row["permission_minutes_used"] >= policy.warning_minutes
        row["permission_exhausted"] = remaining == 0 or occasions >= policy.monthly_paid_occasions
        if paid:
            row["status"] = AttendanceStatus.PAID_PERMISSION.value
        results.append(row)
    return results


def lop_amount(unpaid_minutes: int, wage_basis: float, scheduled_payable_minutes: int) -> float:
    if wage_basis < 0 or scheduled_payable_minutes <= 0:
        raise ValueError("wage basis must be non-negative and scheduled minutes positive")
    return round(wage_basis * max(0, unpaid_minutes) / scheduled_payable_minutes, 2)


def _result(day, status, start, end, **values) -> dict:
    base = {
        "date": day,
        "status": status.value,
        "shift_start": start,
        "shift_end": end,
        "check_in": None,
        "check_out": None,
        "late_minutes": 0,
        "early_minutes": 0,
        "approved_actual_minutes": 0,
        "unapproved_minutes": 0,
        "unpaid_minutes": 0,
        "recovered_minutes": 0,
        "provisional": False,
    }
    base.update(values)
    return base
