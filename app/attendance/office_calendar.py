"""Office-wide attendance defaults: festival holidays and standard shift hours.

These apply to both branches (Royapettah and Mount Road) without anyone having
to mark each employee's day by hand. Anything recorded explicitly still wins:
a calendar entry for the day, a duty plan rostering the holiday as a working
day, or a rolling shift assignment from User/Attendance management.
"""
from __future__ import annotations

from datetime import date, time

from app.attendance.models import Shift

#: Festival holidays allowed in the year 2026, as circulated by the office.
FESTIVAL_HOLIDAYS = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 15): "Pongal",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 21): "Ramzan (Idu'l Fitr)",
    date(2026, 4, 14): "Tamil New Year",
    date(2026, 5, 1): "May Day",
    date(2026, 5, 28): "Bakrid (Idul Azha)",
    date(2026, 8, 15): "Independence Day",
    date(2026, 9, 14): "Vinayagar Chathurthi",
    date(2026, 10, 2): "Gandhi Jayanthi",
    date(2026, 10, 19): "Ayudha Pooja",
    date(2026, 11, 8): "Deepavali",
    date(2026, 12, 25): "Christmas",
}

#: The list is applied in the CRM from October 2026. Earlier months were
#: already settled by hand and are left exactly as recorded.
FESTIVAL_HOLIDAYS_FROM = date(2026, 10, 1)

#: This circulated list covers 2026 only. From 2027 onwards every holiday is
#: declared by an admin from Data Management (see `app.holidays`).
FESTIVAL_HOLIDAYS_YEAR = 2026


def festival_holiday(day: date) -> str | None:
    """The festival this date is a holiday for, if any."""
    if day.year != FESTIVAL_HOLIDAYS_YEAR or day < FESTIVAL_HOLIDAYS_FROM:
        return None
    return FESTIVAL_HOLIDAYS.get(day)


def builtin_holidays() -> list[dict]:
    """The circulated 2026 list, for display beside the declared holidays."""
    return [
        {
            "date": day.isoformat(),
            "name": name,
            # Before October 2026 these were settled by hand, not by the CRM.
            "applied": day >= FESTIVAL_HOLIDAYS_FROM,
        }
        for day, name in sorted(FESTIVAL_HOLIDAYS.items())
        if day.year == FESTIVAL_HOLIDAYS_YEAR
    ]


#: Royapettah staff who work 9:30 AM to 6:30 PM instead of the default
#: 10:00 AM to 7:00 PM. Keyed by email so it survives account recreation.
_ROYAPETTAH_EARLY_SHIFT = Shift(start=time(9, 30), end=time(18, 30))
STANDARD_SHIFTS = {
    "bakkimamal.adira@gmail.com": _ROYAPETTAH_EARLY_SHIFT,
    "sreya.adira@gmail.com": _ROYAPETTAH_EARLY_SHIFT,
    "dsharmila.adira@gmail.com": _ROYAPETTAH_EARLY_SHIFT,
}
STANDARD_SHIFTS_FROM = date(2026, 10, 1)


def standard_shift(email: str | None, day: date) -> Shift:
    """The hours an employee works when no shift has been assigned to them."""
    if day >= STANDARD_SHIFTS_FROM and email:
        shift = STANDARD_SHIFTS.get(email.strip().casefold())
        if shift is not None:
            return shift
    return Shift()
