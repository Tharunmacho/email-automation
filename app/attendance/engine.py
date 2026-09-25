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


#: Statuses that carry no duty at all, so nothing can be "uncovered" on them.
_NO_DUTY_STATUSES = frozenset({
    AttendanceStatus.WEEKLY_OFF,
    AttendanceStatus.HOLIDAY,
    AttendanceStatus.PAID_LEAVE,
})


def required_shift_minutes(shift: Shift, policy: AttendancePolicy | None = None, day: date | None = None) -> int:
    """The payable duty a full day owes: the shift span minus its break.

    This is *the* definition of a day's requirement. Everything that needs to
    know how long a working day is — the engine, payroll, the month roll-up —
    reads it from here so there is exactly one answer.
    """
    policy = policy or AttendancePolicy()
    start, end = shift_bounds(day or date(2000, 1, 1), shift, policy.timezone_name)
    return max(0, _minutes(end - start) - shift.break_minutes)


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
    """Calculate one day's coverage against the duty it owed.

    The whole calculation is one equation, and every field below is a named term
    in it:

        uncovered = max(0, required - covered - approved_permission - recovered)

    `required` is the shift span minus its break — 480 minutes on the default
    10:00–19:00 shift. `covered` is the employee's actual presence: first
    check-in to last check-out, and *not* reduced by the break again. The break
    has already been taken out of `required`; subtracting it from presence too
    charges the employee for it twice, which is precisely the bug that made a
    10:24→19:21 day (8h57m present against an 8h duty) report 25 uncovered
    minutes.

    Measuring coverage as elapsed presence is also what makes a late start
    self-correcting: somebody who arrives 24 minutes late and stays 21 minutes
    past the end has still given the day the hours it asked for, and owes
    nothing. `late_minutes` and `early_minutes` still record what happened, so
    punctuality remains visible even when no salary is affected.
    """
    policy = policy or AttendancePolicy()
    shift = shift or Shift()
    now = as_utc(now or datetime.now(timezone.utc))
    start, end = shift_bounds(day, shift, policy.timezone_name)
    required = max(0, _minutes(end - start) - shift.break_minutes)

    if non_working_status:
        # A weekly off, holiday or paid leave owes nothing, so it can never be
        # short. Unpaid leave owes the full day and is charged for all of it
        # here — `unpaid_minutes` is the final figure, so `uncovered_minutes`
        # stays zero and the month roll-up does not charge for it a second time.
        unpaid = required if non_working_status == AttendanceStatus.UNPAID_LEAVE else 0
        return _result(
            day,
            non_working_status,
            start,
            end,
            required_shift_minutes=0 if non_working_status in _NO_DUTY_STATUSES else required,
            actual_covered_minutes=0,
            uncovered_minutes=0,
            unpaid_minutes=unpaid,
        )
    permissions = list(approved_permissions or [])
    kinds = {str(item.get("kind")) for item in permissions}
    if approved_kind:
        permissions.append({"kind": approved_kind, "requested_minutes": approved_minutes})
        kinds.add(approved_kind)
    duty_kind = "official_duty" if "official_duty" in kinds else "work_from_home" if "work_from_home" in kinds else None
    if duty_kind:
        # Approved duty away from the office is a full day's coverage by
        # definition: there are no punches to measure and nothing is owed.
        status = AttendanceStatus.OFFICIAL_DUTY if duty_kind == "official_duty" else AttendanceStatus.WORK_FROM_HOME
        return _result(
            day, status, start, end,
            required_shift_minutes=required,
            actual_covered_minutes=required,
            uncovered_minutes=0,
        )

    ordered = sorted(
        ((as_utc(p["occurred_at"]), p["action"]) for p in punches),
        key=lambda item: item[0],
    )
    check_ins = [ts for ts, action in ordered if action == "check_in"]
    check_outs = [ts for ts, action in ordered if action == "check_out"]
    check_in = check_ins[0] if check_ins else None
    check_out = check_outs[-1] if check_outs else None

    if not check_in or not check_out:
        # Half a day's punches prove nothing about how long the person stayed.
        # Until the regularisation window closes the day is provisional and
        # charges nothing; after it closes the whole duty is unpaid. As with
        # unpaid leave, `unpaid_minutes` is final and `uncovered_minutes` stays
        # zero so the month roll-up neither double-charges it nor spends the
        # monthly grace balance on a day with no work session at all.
        deadline = end + timedelta(days=policy.missing_punch_deadline_days)
        expired = now > deadline
        return _result(
            day,
            AttendanceStatus.ABSENT if expired else AttendanceStatus.MISSING_PUNCH,
            start,
            end,
            check_in=check_in,
            check_out=check_out,
            required_shift_minutes=required,
            actual_covered_minutes=0,
            uncovered_minutes=0,
            unpaid_minutes=required if expired else 0,
            provisional=not expired,
            regularisation_deadline=deadline,
        )

    if check_in < start and "early_check_in" not in kinds:
        # The API rejects this punch in normal operation; retain the guard for
        # direct engine callers so an unapproved early arrival is not credited.
        check_in = start

    # Punctuality, recorded whether or not it costs anything. These are facts
    # about the clock, not terms in the coverage equation.
    late = _minutes(check_in - start)
    early = _minutes(end - check_out)

    # Coverage: elapsed presence, first in to last out. Approved early arrival
    # counts from the moment they actually arrived; an unapproved one has
    # already been clamped to the shift start above. The end is deliberately
    # uncapped so staying late genuinely compensates for arriving late — extra
    # minutes beyond the requirement are absorbed by the max(0, ...) below and
    # never become overtime by accident, because overtime is a separate
    # approved request (see `approved_extra_ot_minutes`).
    presence_start = check_in if "early_check_in" in kinds else max(check_in, start)
    presence_end = check_out
    covered = _minutes(presence_end - presence_start) if presence_end > presence_start else 0

    shortfall = max(0, required - covered)

    # Permissions are only worth what the day is actually short. A 30-minute
    # late permission on a day that was fully covered pays for nothing.
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
    approved_actual = min(shortfall, sum(approved_occurrences))
    recovered = min(shortfall - approved_actual, max(0, recovered_minutes))

    # The one equation. Monthly grace is the remaining term and is applied by
    # `calculate_month`, which owns the balance across the whole month.
    uncovered = max(0, required - covered - approved_actual - recovered)

    if shortfall == 0:
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
        required_shift_minutes=required,
        actual_covered_minutes=covered,
        uncovered_minutes=uncovered,
        late_minutes=late, early_minutes=early, approved_actual_minutes=approved_actual,
        unapproved_minutes=uncovered, recovered_minutes=recovered,
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
        # A full-day charge (unpaid leave, an expired missing punch) is final
        # and arrives here already priced; `uncovered_minutes` is the part-day
        # shortfall the monthly grace balance may still absorb. The two are
        # deliberately disjoint, so adding them cannot double-charge a day.
        fixed_unpaid = max(0, int(row.get("unpaid_minutes", 0)))
        # `uncovered_minutes` is the current name; every branch of
        # `calculate_day` sets it, including to zero, so the presence of the key
        # is what decides. `unapproved_minutes` is its long-standing alias and
        # is still honoured for rows built by older callers.
        raw_uncovered = row["uncovered_minutes"] if "uncovered_minutes" in row else row.get("unapproved_minutes", 0)
        uncovered = max(0, int(raw_uncovered))
        automatic_grace = min(uncovered, remaining)
        remaining -= automatic_grace
        row["grace_minutes_applied"] = automatic_grace
        row["approved_permission_minutes"] = paid
        row["paid_permission_minutes"] = paid + automatic_grace
        row["uncovered_minutes"] = uncovered
        row["unpaid_minutes"] = fixed_unpaid + uncovered - automatic_grace + excess
        # The part-day share of that charge: arriving late, leaving early, or a
        # permission beyond the monthly allowance. Payroll lets approved
        # overtime cancel this share, never a whole missing day.
        row["late_unpaid_minutes"] = uncovered - automatic_grace + excess
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
        # Every branch sets these three explicitly. They are listed here so a
        # day record has the same shape whatever produced it, and so no reader
        # — payroll, the month roll-up, the browser — has to guess a default
        # for the terms of the coverage equation.
        "required_shift_minutes": 0,
        "actual_covered_minutes": 0,
        "uncovered_minutes": 0,
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
