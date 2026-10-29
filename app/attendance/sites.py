"""Where an employee was standing when they punched.

Each office is a point and a radius, and each employee is measured against
their own office only. The Royapettah staff are listed by number below;
everyone else works from Mount Road, whatever branch or desk they are filed
under. A punch made at the other office is recorded and flagged like any other
off-site punch.

Until 2026-10-29 any office counted. That was added while an off-site pin was
still refused outright, and Royapettah staff missing from the list were being
locked out at their desks. Now that nothing is refused, the looser rule only
hid who was punching from where.

**The measurement no longer refuses the punch.** It used to: a pin outside
every radius was a 403 and nothing was recorded. That cost honest staff their
attendance, because the coordinate WhatsApp sends is not evidence of where
somebody is. There is no accuracy figure in the payload, the phone falls back
to WiFi and cell towers indoors, and tapping a *nearby place* in WhatsApp's
location picker sends that place's coordinates rather than the sender's. From
here all of those look exactly like a real pin from the wrong side of the city.

So the office radius is now evidence, not a gate: the punch is recorded either
way and carries `status` saying what was established. A manager reviews what
comes back `off_site` or `unverified`. An employee silently losing a day's
attendance to a 400 m WiFi error is the worse failure of the two.

`unverified` is the live-location case. WhatsApp does not deliver live location
to the Cloud API at all — it arrives as `unsupported` (error 131051) with no
coordinates — so a punch made that way can never be placed. It is still filed,
and still flagged.

An office without coordinates is measured against nothing at all.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

from app.branches import MOUNT_ROAD, ROYAPETTAH


@dataclass(frozen=True)
class Site:
    name: str
    address: str
    latitude: float | None
    longitude: float | None
    radius_m: int

    @property
    def located(self) -> bool:
        return self.latitude is not None and self.longitude is not None


# Pins supplied by the user from Google Maps: Mount Road on 2026-10-07, and
# Royapettah on 2026-10-29 when its address was corrected to 35 Westcott Road.
SITES: dict[str, Site] = {
    MOUNT_ROAD: Site(
        name=MOUNT_ROAD,
        address="758/MF Mount Chambers, Mount Road, Vasan Ave, Anna Salai, Thousand Lights, Chennai 600002",
        latitude=13.060263,
        longitude=80.259109,
        radius_m=500,
    ),
    ROYAPETTAH: Site(
        name=ROYAPETTAH,
        address="35, Westcott Rd, Indira Garden, Royapettah, Chennai, Tamil Nadu 600014",
        latitude=13.054036,
        longitude=80.264377,
        radius_m=500,
    ),
}

#: The staff who punch from the Royapettah office, by the last ten digits of
#: their number. Everyone not listed here punches from Mount Road.
ROYAPETTAH_STAFF: dict[str, str] = {
    "7806822702": "Sreya",
    "8438588507": "Bakkiammal",
    "8870277929": "Sharmila",
    "9884448455": "Hajira",
    "6381748354": "Sahi",
    "7200786679": "Noorul",
}


class OffSiteError(ValueError):
    """The punch cannot be placed yet and the employee has to be asked.

    Only `location_required` raises now: the bare command that starts a punch,
    which the bot answers by asking for a location. A punch that *is* located
    and lands outside every office is no longer an error — it is recorded and
    flagged. See the module docstring.
    """

    def __init__(self, message: str, *, distance_m: float | None = None, code: str = "off_site"):
        super().__init__(message)
        #: How far away the punch was made, for the log; never shown to staff.
        self.distance_m = distance_m
        #: Which of the two refusals this is, for a caller that must act on it
        #: rather than relay prose. ``location_required`` means no location was
        #: supplied at all and the employee has to be asked for one;
        #: ``off_site`` means they sent one and it was not at any office. The
        #: WhatsApp workflow answers them differently, and matching on the
        #: wording of the message would break the moment the wording changed.
        self.code = code


def _phone_key(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:]


def site_for(employee) -> Site:
    """The office this employee punches from: Royapettah if listed, else Mount Road."""
    if _phone_key(getattr(employee, "phone", None)) in ROYAPETTAH_STAFF:
        return SITES[ROYAPETTAH]
    return SITES[MOUNT_ROAD]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


#: What `check_on_site` established about where a punch was made.
ON_SITE = "on_site"
OFF_SITE = "off_site"
UNVERIFIED = "unverified"


def check_on_site(
    employee,
    latitude: float | None,
    longitude: float | None,
    *,
    location_unavailable: bool = False,
) -> dict | None:
    """Place a punch against the offices, for the event's evidence.

    Returns what was established, or None when this employee has no located
    office to be measured against.

    This does not refuse an off-site punch — see the module docstring. The one
    thing it still refuses is a punch that arrived with **no location and no
    word on why**, because that is the opening move of every punch: "Rafi -
    check in" is sent on its own and the bot has to be told to go and ask for a
    location. Answering it with a filed, unverified punch would mean nobody was
    ever asked for a location again.

    `location_unavailable` is how the caller says it already asked and the
    answer could not be used — a live location, which the Cloud API delivers
    with no coordinates at all. That one is filed as `unverified` rather than
    sent round the same loop a second time.
    """
    site = site_for(employee)
    if not site.located:
        return None
    if latitude is None or longitude is None:
        if not location_unavailable:
            raise OffSiteError(
                f"Location is required. Share your current location from the {site.name} office to mark attendance.",
                code="location_required",
            )
        return {
            "site": site.name,
            "distance_m": None,
            "radius_m": site.radius_m,
            "status": UNVERIFIED,
        }
    distance = distance_m(latitude, longitude, site.latitude, site.longitude)
    return {
        "site": site.name,
        "distance_m": round(distance),
        "radius_m": site.radius_m,
        "status": ON_SITE if distance <= site.radius_m else OFF_SITE,
    }
