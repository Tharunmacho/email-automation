"""Load a branch's monthly attendance register (.xlsx) into the live CRM.

The office keeps one register per branch and month: a block per employee with
a header row naming the hours ("10.00 am - 7.00 pm") and the day numbers, a
weekday row, then a "Check in" row and a "Check Out" row. A cell holds a time
written as hours.minutes (9.50 is 9:50 AM, 19.04 is 7:04 PM) or one of
"Leave", "Week off", "Holiday" or "On Duty".

Everything is written through the CRM's own API as an administrator, so the
attendance rules value each day exactly as they would any other punch:

* times                   -> check-in / check-out recorded by the administrator
* Leave                   -> a leave day on the calendar (see --leave)
* On Duty                 -> an official-duty request, approved
* Week off on a weekday   -> that week's weekly off, approved, which makes the
                             week's Sunday an ordinary working day (see --sunday)
* Holiday                 -> checked against the CRM's holidays, added if missing
* the hours in the header -> a work-timing change from the 1st when the CRM differs

`plan` only reads; `apply` writes. Re-running is safe: punches carry fixed
idempotency keys and anything already in the CRM is skipped. Days where the
CRM already holds *different* punches are reported and left alone.

    $env:CRM_PASSWORD='...'; python scripts/import_attendance_register.py login --email admin@example.com
    python scripts/import_attendance_register.py plan  REGISTER.xlsx --year 2026 --month 10 --branch "Mount Road"
    python scripts/import_attendance_register.py apply REGISTER.xlsx --year 2026 --month 10 --branch "Mount Road"
"""
from __future__ import annotations

import argparse
import calendar
import getpass
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
import requests

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_API = "https://email-automation-emailbackend-tfykto-dc98c8-147-79-71-40.sslip.io"
#: The signed-in session (a token that expires; delete the file to sign out).
TOKEN_FILE = Path.home() / ".adira-crm-session.json"
SUNDAY = 6
#: How long a Sunday worked under `--sunday short-day` runs, from the usual start.
SHORT_SUNDAY = timedelta(hours=6)

LEAVE, WEEK_OFF, HOLIDAY, ON_DUTY = "Leave", "Week off", "Holiday", "On Duty"
MARKS = {
    "leave": LEAVE,
    "week off": WEEK_OFF, "weekoff": WEEK_OFF, "week-off": WEEK_OFF, "wo": WEEK_OFF,
    "holiday": HOLIDAY,
    "on duty": ON_DUTY, "onduty": ON_DUTY, "od": ON_DUTY,
}


# --------------------------------------------------------------------------- #
#  Reading the register
# --------------------------------------------------------------------------- #
@dataclass
class RegisterDay:
    day: date
    mark: str | None = None
    check_in: time | None = None
    check_out: time | None = None


@dataclass
class RegisterEmployee:
    name: str
    start: time
    end: time
    days: dict[date, RegisterDay] = field(default_factory=dict)


def _clock(value, where: str) -> time:
    """9.5 -> 09:50, 19.04 -> 19:04, "10:10" -> 10:10. The sheet stores h.mm."""
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, (int, float)):
        hours = int(value)
        minutes = round((float(value) - hours) * 100)
    else:
        match = re.fullmatch(r"\s*(\d{1,2})(?:[.:](\d{1,2}))?\s*", str(value))
        if not match:
            raise ValueError(f"{where}: cannot read {value!r} as a time")
        hours = int(match.group(1))
        digits = match.group(2) or "0"
        minutes = int(digits) * (10 if len(digits) == 1 else 1)
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"{where}: {value!r} is not a time of day")
    return time(hours, minutes)


def _hours(text, where: str) -> tuple[time, time]:
    found = re.findall(r"(\d{1,2})(?:[.:](\d{2}))?\s*([ap])\.?m", str(text or "").lower())
    if len(found) != 2:
        raise ValueError(f"{where}: cannot read the hours {text!r} (expected e.g. '10.00 am - 7.00 pm')")
    times = []
    for hour, minute, half in found:
        hour = int(hour) % 12 + (12 if half == "p" else 0)
        times.append(time(hour, int(minute or 0)))
    return times[0], times[1]


def _mark(value) -> str | None:
    if not isinstance(value, str):
        return None
    return MARKS.get(re.sub(r"\s+", " ", value.strip().lower()))


def read_register(path: Path, year: int, month: int) -> list[RegisterEmployee]:
    sheet = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    cell = lambda row, col: sheet.cell(row, col).value  # noqa: E731
    employees: list[RegisterEmployee] = []
    columns: dict[int, date] = {}
    hours: tuple[time, time] | None = None
    for row in range(1, sheet.max_row + 1):
        label = str(cell(row, 1) or "").strip()
        kind = str(cell(row, 2) or "").strip().lower()
        if label.lower() == "employee name":
            hours = _hours(cell(row, 2), f"row {row}")
            columns = {}
            for col in range(3, sheet.max_column + 1):
                try:
                    number = int(str(cell(row, col)).strip())
                except ValueError:
                    continue
                day = date(year, month, number)
                shown = str(cell(row + 1, col) or "").strip()[:3].lower()
                if shown and shown != day.strftime("%a").lower():
                    raise ValueError(
                        f"row {row + 1}: day {number} is marked {shown!r} but {day:%d %b %Y} is a {day:%A}; "
                        "check --year/--month"
                    )
                columns[col] = day
            continue
        if not label or not kind.startswith("check in"):
            continue
        if hours is None or not columns:
            raise ValueError(f"row {row}: {label} has no 'Employee Name' header above it")
        if not str(cell(row + 1, 2) or "").strip().lower().startswith("check out"):
            raise ValueError(f"row {row + 1}: expected the 'Check Out' row for {label}")
        employee = RegisterEmployee(label, *hours)
        for col, day in columns.items():
            first, second = cell(row, col), cell(row + 1, col)
            entry = RegisterDay(day)
            if isinstance(first, str) and _mark(first):
                entry.mark = _mark(first)
            elif isinstance(first, str) and first.strip() and not re.fullmatch(r"[\d.:\s]+", first):
                raise ValueError(f"{sheet.cell(row, col).coordinate}: unknown entry {first!r} for {label}")
            else:
                if first not in (None, ""):
                    entry.check_in = _clock(first, sheet.cell(row, col).coordinate)
                if second not in (None, "") and not _mark(second):
                    entry.check_out = _clock(second, sheet.cell(row + 1, col).coordinate)
            employee.days[day] = entry
        employees.append(employee)
    if not employees:
        raise ValueError("no employee blocks found (expected 'Employee Name' headers and 'Check in' rows)")
    return employees


# --------------------------------------------------------------------------- #
#  The CRM
# --------------------------------------------------------------------------- #
class Crm:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, **params) -> dict:
        response = self.session.get(self.base + path, params=params, timeout=60)
        if response.status_code >= 400:
            raise RuntimeError(f"GET {path} -> {response.status_code}: {response.text[:300]}")
        return response.json()

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        response = self.session.post(self.base + path, json=body, timeout=60)
        try:
            data = response.json()
        except ValueError:
            data = {"detail": response.text[:300]}
        return response.status_code, data


def _saved_session() -> dict:
    if os.environ.get("CRM_TOKEN"):
        return {"token": os.environ["CRM_TOKEN"], "api": os.environ.get("CRM_API", DEFAULT_API)}
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    sys.exit(f"Not signed in. Run: python {sys.argv[0]} login --email <admin email>  (password in CRM_PASSWORD)")


def login(args) -> None:
    password = os.environ.get("CRM_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            sys.exit("Set CRM_PASSWORD, e.g.  $env:CRM_PASSWORD='...'; python ... login --email you@example.com")
        password = getpass.getpass(f"Password for {args.email}: ")
    response = requests.post(f"{args.api.rstrip('/')}/auth/login",
                             json={"email": args.email, "password": password}, timeout=60)
    if response.status_code != 200:
        sys.exit(f"Login failed ({response.status_code}): {response.text[:200]}")
    body = response.json()
    if body["user"].get("role") != "admin":
        sys.exit(f"{args.email} is a {body['user'].get('role')}; the import needs a Super Admin account")
    TOKEN_FILE.write_text(json.dumps({"api": args.api, "token": body["token"], "email": args.email}), encoding="utf-8")
    print(f"Signed in as {body['user'].get('name') or args.email} (admin). Session saved to {TOKEN_FILE}")


def _ist(value) -> time | None:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(IST).time().replace(second=0, microsecond=0)


def _hhmm(value: time | None) -> str:
    return value.strftime("%H:%M") if value else "--:--"


def _match_employee(name: str, staff: list[dict], overrides: dict[str, str]) -> dict | None:
    wanted = overrides.get(name.casefold(), name).casefold().strip()
    for person in staff:
        if wanted in {str(person.get("id", "")).casefold(), str(person.get("email", "")).casefold()}:
            return person
    exact = [p for p in staff if (p.get("name") or "").casefold().strip() == wanted]
    if len(exact) == 1:
        return exact[0]
    # "Atba" is "Atba Fathima" in the CRM; "Mohamed Rafi" is just "Rafi".
    words = set(wanted.split())
    loose = [p for p in staff
             if (theirs := set((p.get("name") or "").casefold().split())) and (words <= theirs or theirs <= words)]
    return loose[0] if len(loose) == 1 else None


# --------------------------------------------------------------------------- #
#  Planning
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    order: int
    day: date | None
    what: str
    method: str
    path: str
    body: dict
    then_approve: bool = False


@dataclass
class EmployeePlan:
    sheet_name: str
    person: dict
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    minor_differences: int = 0


def plan_employee(crm: Crm, entry: RegisterEmployee, person: dict, args, reason: str) -> EmployeePlan:
    plan = EmployeePlan(entry.name, person)
    employee_id = person["id"]
    first = min(entry.days)
    crm_days = {day: crm.get(f"/attendance/day/{day.isoformat()}", employee_id=employee_id) for day in sorted(entry.days)}
    permissions = crm.get("/attendance/permissions", year=args.year, month=args.month, employee_id=employee_id)["items"]
    duty_plans = {row["effective_from"] for row in crm.get(
        "/attendance/duty-plans", year=args.year, month=args.month, employee_id=employee_id)["items"]}

    def permission_for(day: date, kind: str) -> dict | None:
        return next((row for row in permissions if row.get("attendance_date") == day.isoformat()
                     and row.get("kind") == kind and row.get("status") in {"pending", "approved"}), None)

    def add(order, day, what, method, path, body, then_approve=False):
        plan.steps.append(Step(order, day, what, method, path, body, then_approve))

    # 1. Hours: the register's header against what the CRM runs the 1st to.
    crm_start, crm_end = _ist(crm_days[first].get("shift_start")), _ist(crm_days[first].get("shift_end"))
    if (crm_start, crm_end) != (entry.start, entry.end):
        add(1, first, f"work timing {_hhmm(entry.start)}-{_hhmm(entry.end)} from {first:%d %b} "
            f"(CRM has {_hhmm(crm_start)}-{_hhmm(crm_end)})", "POST", "/attendance/shifts",
            {"employee_id": employee_id, "effective_from": first.isoformat(), "kind": "recurring", "reason": reason,
             "shift": {"start": entry.start.isoformat(), "end": entry.end.isoformat(), "break_minutes": 60}})

    # 2. Weekly offs. One per Monday-Sunday week; Sunday unless the register
    #    names a weekday, in which case that day is approved as the week's off
    #    and the Sunday becomes a working day.
    weeks: dict[date, list[RegisterDay]] = defaultdict(list)
    for day in sorted(entry.days):
        weeks[day - timedelta(days=day.weekday())].append(entry.days[day])
    off_days: set[date] = set()
    for week_start, days in weeks.items():
        sunday = week_start + timedelta(days=6)
        weekday_offs = [d.day for d in days if d.mark == WEEK_OFF and d.day.weekday() != SUNDAY]
        if not weekday_offs or args.sunday == "off":
            off_days.add(sunday)
            off_days.update(weekday_offs)
            for day in weekday_offs:
                if crm_days[day].get("status") != "WO":
                    add(3, day, f"{day:%a %d %b}: extra weekly off (Sunday stays off too)", "POST",
                        "/attendance/calendar",
                        {"employee_id": employee_id, "attendance_date": day.isoformat(), "status": "WO", "reason": reason})
            continue
        swap, *extra = weekday_offs
        off_days.add(swap)
        if extra:
            plan.notes.append(f"week of {week_start:%d %b}: more than one weekday off "
                              f"({', '.join(f'{d:%a %d}' for d in weekday_offs)}); the others are extra offs")
            for day in extra:
                off_days.add(day)
                add(3, day, f"{day:%a %d %b}: extra weekly off", "POST", "/attendance/calendar",
                    {"employee_id": employee_id, "attendance_date": day.isoformat(), "status": "WO", "reason": reason})
        if not permission_for(swap, "weekly_off") and crm_days[swap].get("status") != "WO":
            add(2, swap, f"{swap:%a %d %b}: weekly off for the week (instead of Sunday {sunday:%d %b})",
                "POST", "/attendance/permissions",
                {"employee_id": employee_id, "attendance_date": swap.isoformat(), "kind": "weekly_off", "reason": reason},
                then_approve=True)
        elif (pending := permission_for(swap, "weekly_off")) and pending.get("status") == "pending":
            add(2, swap, f"{swap:%a %d %b}: approve the pending weekly-off request", "DECIDE",
                f"/attendance/permissions/{pending['id']}/decision", {})
        sunday_entry = entry.days.get(sunday)
        if (sunday_entry and sunday_entry.mark != WEEK_OFF and args.sunday == "short-day"
                and sunday.isoformat() not in duty_plans):
            start = datetime.combine(sunday, entry.start)
            add(2, sunday, f"Sun {sunday:%d %b}: working day {_hhmm(entry.start)}-{_hhmm((start + SHORT_SUNDAY).time())}",
                "POST", "/attendance/duty-plans",
                {"employee_id": employee_id, "attendance_date": sunday.isoformat(), "reason": reason,
                 "shift": {"start": entry.start.isoformat(), "end": (start + SHORT_SUNDAY).time().isoformat(),
                           "break_minutes": 0}})
        if sunday_entry and sunday_entry.mark == WEEK_OFF:
            off_days.add(sunday)
            add(3, sunday, f"Sun {sunday:%d %b}: also off", "POST", "/attendance/calendar",
                {"employee_id": employee_id, "attendance_date": sunday.isoformat(), "status": "WO", "reason": reason})

    # 3. Day by day.
    for day in sorted(entry.days):
        row, crm = entry.days[day], crm_days[day]
        status = crm.get("status")
        holiday = status == "H"
        off = day in off_days or holiday
        label = f"{day:%a %d %b}"
        if crm.get("status_overridden"):
            plan.notes.append(f"{label}: an administrator fixed this day's status to {status} by hand; it will stay {status}")
        if row.mark == HOLIDAY and not holiday:
            add(3, day, f"{label}: holiday", "POST", "/attendance/calendar",
                {"employee_id": employee_id, "attendance_date": day.isoformat(), "status": "H", "reason": reason})
        elif row.mark == LEAVE:
            wanted = "UL" if args.leave == "unpaid" else "PL"
            if off and not args.charge_leave_on_off_days:
                plan.notes.append(f"{label}: Leave falls on a {'holiday' if holiday else 'weekly off'}; not charged")
            elif status in {"PL", "UL"}:
                pass
            else:
                add(4, day, f"{label}: leave ({'unpaid' if wanted == 'UL' else 'paid if it is the first this month, else unpaid'})",
                    "POST", "/attendance/calendar",
                    {"employee_id": employee_id, "attendance_date": day.isoformat(), "status": wanted, "reason": reason})
        elif row.mark == ON_DUTY:
            if off:
                plan.notes.append(f"{label}: On Duty on a {'holiday' if holiday else 'weekly off'}; nothing is owed that day anyway")
            elif status == "OD":
                pass
            elif (pending := permission_for(day, "official_duty")) and pending.get("status") == "pending":
                add(5, day, f"{label}: approve the pending on-duty request", "DECIDE",
                    f"/attendance/permissions/{pending['id']}/decision", {})
            else:
                add(5, day, f"{label}: on duty", "POST", "/attendance/permissions",
                    {"employee_id": employee_id, "attendance_date": day.isoformat(), "kind": "official_duty",
                     "reason": reason}, then_approve=True)
        elif row.check_in or row.check_out:
            have_in, have_out = _ist(crm.get("check_in")), _ist(crm.get("check_out"))
            if not have_in and not have_out and crm.get("raw_event_ids"):
                # A day off shows no times, but its punches are still on file.
                plan.notes.append(f"{label}: the CRM already has punches on this {'holiday' if holiday else 'day off'}; left as is")
                continue
            # A punch the CRM already holds is kept: it was made at the time,
            # the register was copied down later. Only a missing half is added.
            gaps = [abs(_minute(have) - _minute(want))
                    for have, want in ((have_in, row.check_in), (have_out, row.check_out)) if have and want]
            if any(gap > args.tolerance for gap in gaps):
                plan.conflicts.append(f"{label}: CRM has {_hhmm(have_in)}-{_hhmm(have_out)}, register says "
                                      f"{_hhmm(row.check_in)}-{_hhmm(row.check_out)}; the CRM's times are kept")
            elif any(gaps):
                plan.minor_differences += 1
            # A day the CRM already has half of is completed with a correction,
            # which needs no matching raw check-in; a fresh day gets real punches.
            started = bool(have_in or have_out)
            if row.check_in and not have_in:
                add(6, day, f"{label}: check-in {_hhmm(row.check_in)}", "POST",
                    *_entry(employee_id, day, row.check_in, "check_in", reason, started))
            if row.check_out and not have_out and (row.check_in or have_in):
                add(7, day, f"{label}: check-out {_hhmm(row.check_out)}" + (" (completes the CRM's day)" if started else ""),
                    "POST", *_entry(employee_id, day, row.check_out, "check_out", reason, started))
            if not row.check_out:
                plan.notes.append(f"{label}: checked in {_hhmm(row.check_in)} with no check-out; the CRM counts a day "
                                  "with half its punches as absent")
            if not row.check_in and not have_in:
                plan.notes.append(f"{label}: a check-out with no check-in; not recorded")
            if off:
                plan.notes.append(f"{label}: worked on a {'holiday' if holiday else 'weekly off'}; recorded, no extra pay")
        elif row.mark is None and not off and status not in {"PL", "UL", "OD", "WFH", "WO", "H"} and not crm.get("check_in"):
            plan.notes.append(f"{label}: blank in the register; the CRM will count it absent")
    plan.steps.sort(key=lambda step: (step.order, step.day or date.min))
    return plan


def _minute(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def _entry(employee_id: str, day: date, clock: time, action: str, reason: str, correction: bool) -> tuple[str, dict]:
    """A punch for a day the CRM has nothing for, else an added-punch correction."""
    occurred_at = datetime.combine(day, clock, IST).isoformat()
    if correction:
        return "/attendance/adjustments", {
            "employee_id": employee_id, "attendance_date": day.isoformat(), "kind": "add_punch",
            "action": action, "occurred_at": occurred_at, "reason": reason,
        }
    return "/attendance/punch", {
        "employee_id": employee_id,
        "action": action,
        "occurred_at": occurred_at,
        "source": "admin",
        "idempotency_key": f"register:{employee_id}:{day.isoformat()}:{action}",
        "evidence": {"note": reason},
    }


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
def build_plans(args) -> tuple[Crm, list[EmployeePlan]]:
    session = _saved_session()
    crm = Crm(session["api"], session["token"])
    register = read_register(Path(args.register), args.year, args.month)
    staff = crm.get("/attendance/employees")["items"]
    if args.branch:
        # Accounts with no branch recorded may still work there; the other
        # branch's staff are never matched.
        staff = [p for p in staff if (p.get("branch") or "").strip().casefold() in {"", args.branch.casefold()}]
    overrides = {}
    for item in args.match or []:
        sheet_name, _, crm_name = item.partition("=")
        overrides[sheet_name.strip().casefold()] = crm_name.strip()
    reason = f"{args.branch or 'Branch'} attendance register, {calendar.month_name[args.month]} {args.year}"
    plans, missing = [], []
    for entry in register:
        if args.only and entry.name.casefold() not in {name.casefold() for name in args.only}:
            continue
        person = _match_employee(entry.name, staff, overrides)
        if person is None:
            missing.append(entry.name)
            continue
        print(f"Reading the CRM for {entry.name} ...", file=sys.stderr)
        plans.append(plan_employee(crm, entry, person, args, reason))
    if missing:
        names = ", ".join(f"{p.get('name')} <{p.get('email')}>" for p in staff)
        sys.exit(f"No CRM employee matches: {', '.join(missing)}.\nEmployees here: {names}\n"
                 "Pair them with --match \"Register Name=CRM name or email\".")
    return crm, plans


def print_plan(plans: list[EmployeePlan]) -> None:
    for plan in plans:
        person = plan.person
        print(f"\n=== {plan.sheet_name}  ->  {person.get('name')} <{person.get('email')}> "
              f"({person.get('role')}, {person.get('branch') or 'no branch'})")
        counts = Counter(step.path.rsplit("/", 1)[-1] if step.method != "DECIDE" else "decision" for step in plan.steps)
        print(f"    {len(plan.steps)} changes: " + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none"))
        for step in plan.steps:
            print(f"    + {step.what}")
        for note in plan.notes:
            print(f"    i {note}")
        if plan.minor_differences:
            print(f"    = {plan.minor_differences} day(s) where the CRM's punches differ from the register by a few "
                  "minutes; the CRM's times are kept")
        for conflict in plan.conflicts:
            print(f"    ! {conflict}")


def apply(crm: Crm, plans: list[EmployeePlan]) -> int:
    failures = 0
    for plan in plans:
        for step in plan.steps:
            if step.method == "DECIDE":
                status, body = crm.post(step.path, {"approved": True, "reason": "Recorded from the attendance register"})
            else:
                status, body = crm.post(step.path, step.body)
                if status < 400 and step.then_approve:
                    permission_id = body["permission"]["id"]
                    status, body = crm.post(f"/attendance/permissions/{permission_id}/decision",
                                            {"approved": True, "reason": "Recorded from the attendance register"})
            if status >= 400:
                failures += 1
                print(f"  FAILED {plan.sheet_name}: {step.what}: {status} {body.get('detail', body)}")
            else:
                print(f"  ok     {plan.sheet_name}: {step.what}")
    return failures


def summarise(crm: Crm, plans: list[EmployeePlan], args) -> None:
    print(f"\nWhat the CRM now shows for {calendar.month_name[args.month]} {args.year}:")
    payroll = {row["employee_id"]: row for row in crm.get(f"/payroll/{args.year}/{args.month}")["items"]}
    for plan in plans:
        employee_id = plan.person["id"]
        month = crm.get(f"/attendance/month/{args.year}/{args.month}", employee_id=employee_id)
        statuses = Counter(day["status"] for day in month["days"])
        pay = payroll.get(employee_id, {})
        print(f"  {plan.person.get('name'):<22} " + " ".join(f"{k}:{v}" for k, v in sorted(statuses.items()))
              + f" | unpaid {month['totals']['unpaid_minutes']} min"
              + (f" | salary {pay.get('monthly_salary', 0):,.0f} - LOP {pay.get('deduction', 0):,.2f}"
                 f" = {pay.get('total_payable', 0):,.2f}" if pay else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    sign_in = commands.add_parser("login", help="sign in as a Super Admin and keep the session")
    sign_in.add_argument("--email", required=True)
    sign_in.add_argument("--api", default=os.environ.get("CRM_API", DEFAULT_API))
    for name in ("plan", "apply"):
        command = commands.add_parser(name, help="show the changes" if name == "plan" else "make the changes")
        command.add_argument("register", help="the .xlsx attendance register")
        command.add_argument("--year", type=int, required=True)
        command.add_argument("--month", type=int, required=True)
        command.add_argument("--branch", help="only match employees of this branch, e.g. 'Mount Road'")
        command.add_argument("--only", nargs="+", help="only these register names")
        command.add_argument("--match", action="append", help="'Register Name=CRM name or email' when names differ")
        command.add_argument("--leave", choices=("paid", "unpaid"), default="paid",
                             help="paid: the CRM's rule, first leave day of the month paid and the rest unpaid")
        command.add_argument("--sunday", choices=("full-day", "short-day", "off"), default="full-day",
                             help="a Sunday worked in a week whose off was a weekday: an ordinary working day "
                                  "(the office's rule), a 6-hour day, or still off")
        command.add_argument("--tolerance", type=int, default=15,
                             help="minutes a CRM punch may differ from the register before it is listed")
        command.add_argument("--charge-leave-on-off-days", action="store_true",
                             help="record Leave even on a holiday or weekly off")
    args = parser.parse_args()
    if args.command == "login":
        login(args)
        return
    crm, plans = build_plans(args)
    print_plan(plans)
    if args.command == "apply":
        print("\nApplying ...")
        failures = apply(crm, plans)
        summarise(crm, plans, args)
        if failures:
            sys.exit(f"\n{failures} change(s) failed; fix them and run apply again (finished ones are skipped).")
    else:
        print("\nNothing was changed. Run the same command with 'apply' to write it.")


if __name__ == "__main__":
    main()
