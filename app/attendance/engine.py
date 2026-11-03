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
    """The monthly allowances that keep late and early time off the payslip.

    Two separate balances, both reset on the 1st:

    * **Grace** (`monthly_grace_minutes`, 60) is automatic. Arriving late or
      leaving early without a permission is forgiven day by day, oldest first,
      until it runs out.
    * **Paid permission** (`monthly_permission_minutes`, 180 — three hours) is
      asked for in advance as a late-arrival or early-leaving permission and
      approved by the finance manager (Noorul). Approved time is paid from this
      balance and never touches the grace.

    Approved time beyond the three hours is ordinary late/early time again, so
    any grace left may still forgive it.

    `shared_pool` is the rule before November 2026: one 60-minute balance that
    approved permissions (at most two a month) and the grace both drew from.
    Payroll is recalculated on every read, so earlier months keep that rule.
    """

    monthly_grace_minutes: int = 60
    monthly_permission_minutes: int = 180
    #: Approved permissions paid per month; None means only the minutes limit.
    monthly_paid_occasions: int | None = None
    shared_pool: bool = False
    #: `permission_warning` turns on once this much paid permission is used.
    warning_minutes: int = 150
    missing_punch_deadline_days: int = 2
    timezone_name: str = "Asia/Kolkata"
    # Flexible hours: presence counts from this long before the shift start
    # until this long after its end — 09:00 to 20:00 on the default shift.
    flex_minutes: int = 60


#: The first month with the separate three-hour paid permission.
PAID_PERMISSION_START = date(2026, 11, 1)

#: Before `PAID_PERMISSION_START`: one shared 60-minute balance, two occasions.
SHARED_POOL_POLICY = AttendancePolicy(
    monthly_grace_minutes=60, monthly_permission_minutes=60,
    monthly_paid_occasions=2, shared_pool=True, warning_minutes=45,
)


def policy_for_month(day: date) -> AttendancePolicy:
    """The allowance rule in force for the month containing ``day``."""
    return SHARED_POOL_POLICY if day < PAID_PERMISSION_START else AttendancePolicy()


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

    `required` is the shift span minus its break — 480 minutes of work on the
    default 10:00–19:00 shift. `covered` is the work actually done: first
    check-in to last check-out, less the same break. Nine hours in the office
    is therefore a full eight-hour day wherever it falls.

    Hours are flexible. Presence counts anywhere inside the flexible window —
    `policy.flex_minutes` either side of the shift, 09:00 to 20:00 by default —
    so 09:30→18:30 and 10:30→19:30 are both full days. Time outside the window
    earns nothing, and time beyond the requirement is absorbed by the max(0, …)
    and never becomes overtime; that is a separate approved request (see
    `approved_extra_ot_minutes`). `late_minutes` and `early_minutes` still
    record the clock against the nominal shift, so punctuality stays visible
    even when no salary is affected.
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

    if not check_in and not check_out:
        # No attendance marked at all. While the day's flexible window is open
        # the person may still arrive, so nothing is decided yet. Once it has
        # closed, no punch could earn anything for this day: it is an absence,
        # shown and charged as one straight away rather than after the missing
        # punch deadline below, which is for a day with half its punches. A
        # manager's added punches still recalculate it, as days are computed on
        # every read.
        window_closed = end + timedelta(minutes=policy.flex_minutes)
        if now > window_closed:
            return _result(
                day,
                AttendanceStatus.ABSENT,
                start,
                end,
                required_shift_minutes=required,
                actual_covered_minutes=0,
                uncovered_minutes=0,
                unpaid_minutes=required,
            )
        return _result(
            day,
            AttendanceStatus.MISSING_PUNCH,
            start,
            end,
            required_shift_minutes=required,
            actual_covered_minutes=0,
            uncovered_minutes=0,
            unpaid_minutes=0,
            provisional=True,
        )

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

    # Punctuality, recorded whether or not it costs anything. These are facts
    # about the clock, not terms in the coverage equation.
    late = _minutes(check_in - start)
    early = _minutes(end - check_out)

    # Coverage: presence inside the flexible window, less the break. An early
    # check-in is always recorded; only an approved one earns the time before
    # the window opens.
    flex = timedelta(minutes=policy.flex_minutes)
    window_start, window_end = start - flex, end + flex
    presence_start = check_in if "early_check_in" in kinds else max(check_in, window_start)
    presence_end = min(check_out, window_end)
    present = _minutes(presence_end - presence_start) if presence_end > presence_start else 0
    covered = max(0, present - shift.break_minutes)

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
    """Allocate the month's paid-permission and grace balances chronologically.

    Without an explicit ``policy`` the rule in force for the month is used
    (`policy_for_month`).
    """
    ordered = sorted(days, key=lambda row: row["date"])
    if policy is None:
        policy = policy_for_month(ordered[0]["date"]) if ordered else AttendancePolicy()
    grace_left = policy.monthly_grace_minutes
    permission_left = grace_left if policy.shared_pool else policy.monthly_permission_minutes
    occasions = 0
    results: list[dict] = []
    for raw in ordered:
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
            limit = policy.monthly_paid_occasions
            if limit is None or occasions < limit or emergency:
                applied = min(occurrence, permission_left)
                paid += applied
            else:
                applied = 0
            if applied:
                occasions += 1
                permission_left -= applied
                if policy.shared_pool:
                    grace_left = permission_left
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
        # Grace forgives late/early time no permission paid for: the uncovered
        # shortfall and, with separate balances, approved time beyond the three
        # hours. Under the old shared balance an unpaid excess means the one
        # balance (or its two occasions) is spent, so grace never reached it.
        graceable = uncovered if policy.shared_pool else uncovered + excess
        automatic_grace = min(graceable, grace_left)
        grace_left -= automatic_grace
        if policy.shared_pool:
            permission_left = grace_left
        row["grace_minutes_applied"] = automatic_grace
        row["approved_permission_minutes"] = paid
        row["paid_permission_minutes"] = paid + automatic_grace
        row["uncovered_minutes"] = uncovered
        row["unpaid_minutes"] = fixed_unpaid + uncovered + excess - automatic_grace
        # The part-day share of that charge: arriving late, leaving early, or a
        # permission beyond the monthly allowance. Payroll lets approved
        # overtime cancel this share, never a whole missing day.
        row["late_unpaid_minutes"] = uncovered + excess - automatic_grace
        permission_total = policy.monthly_grace_minutes if policy.shared_pool else policy.monthly_permission_minutes
        row["permission_occasions_used"] = occasions
        row["permission_minutes_allowed"] = permission_total
        row["permission_minutes_used"] = permission_total - permission_left
        row["permission_minutes_remaining"] = permission_left
        row["grace_minutes_allowed"] = policy.monthly_grace_minutes
        row["grace_minutes_used"] = policy.monthly_grace_minutes - grace_left
        row["grace_minutes_remaining"] = grace_left
        row["permission_warning"] = row["permission_minutes_used"] >= policy.warning_minutes
        row["permission_exhausted"] = permission_left == 0 or (
            policy.monthly_paid_occasions is not None and occasions >= policy.monthly_paid_occasions
        )
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
