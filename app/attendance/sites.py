"""Where an employee must be standing for a punch to count.

Each office is a point and a radius, and each employee is measured against
their own office only: the branch the CRM files them under (`branch_of`, the
Location column in User Management). A branch with no office of its own falls
to Mount Road. A punch made at the other office is refused like any other.

**A punch from outside the radius is refused and nothing is recorded.** With
no punch the day is absent, and nothing goes to a manager. The user set this
on 2026-10-29, ending a spell in which an off-site punch was recorded and
flagged for a manager to review instead.

The known cost: the pin WhatsApp sends carries no accuracy figure, and a phone
indoors can fall back to WiFi or cell towers, so somebody at their desk can now
and then be refused. They are told why, and the bot holds their command for ten
minutes so that sharing their location again completes it.

A live location is refused the same way. WhatsApp does not deliver it to the
Cloud API at all — it arrives as `unsupported` (error 131051) with no
coordinates — so it can never be placed, and the employee is told to use "Send
your current location" instead.

The office used to come from a fixed list of phone numbers. A staff member
created at Royapettah in the CRM was not on it, so their first punch from the
Royapettah office was filed as off site at Mount Road. Moving someone between
offices is now a User Management change, not a code change.

An office without coordinates is measured against nothing at all.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from app.branches import MOUNT_ROAD, ROYAPETTAH, branch_of


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
# Royapettah on 2026-10-29 when its address was corrected to 35 Westcott Road
# and its radius set to 400 m.
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
        latitude=13.054280,
        longitude=80.264568,
        radius_m=400,
    ),
}


class OffSiteError(ValueError):
    """The punch is not recorded: no location yet, or not at the office."""

    def __init__(self, message: str, *, distance_m: float | None = None, code: str = "off_site"):
        super().__init__(message)
        #: How far away the punch was made, for the log; never shown to staff.
        self.distance_m = distance_m
        #: Which of the two refusals this is, for a caller that must act on it
        #: rather than relay prose. ``location_required`` means no location was
        #: supplied at all and the employee has to be asked for one;
        #: ``off_site`` means what they sent did not place them at their
        #: office, or could not be placed at all (a live location). The
        #: WhatsApp workflow answers them differently, and matching on the
        #: wording of the message would break the moment the wording changed.
        self.code = code


def site_for(employee) -> Site:
    """The office of the branch this employee is filed under, else Mount Road."""
    branch = branch_of(employee).casefold()
    for name, site in SITES.items():
        if name.casefold() == branch:
            return site
    return SITES[MOUNT_ROAD]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


#: Recorded on every punch that passes `check_on_site`.
ON_SITE = "on_site"


def check_on_site(
    employee,
    latitude: float | None,
    longitude: float | None,
    *,
    location_unavailable: bool = False,
) -> dict | None:
    """Hold a punch to the employee's office, or refuse it.

    Returns where the punch was placed, for the event's evidence, or None when
    this employee has no located office to be measured against. Raises
    `OffSiteError` otherwise — see the module docstring.

    A punch with **no location and no word on why** is `location_required`,
    because that is the opening move of every punch: "Rafi - check in" is sent
    on its own and the bot has to be told to go and ask for a location.

    `location_unavailable` is how the caller says it already asked and the
    answer could not be used — a live location, which the Cloud API delivers
    with no coordinates at all. That is `off_site` with its own explanation,
    rather than `location_required`, which would send the employee round the
    same loop again.
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
        raise OffSiteError(
            "Attendance not recorded: live location does not reach us. "
            f'Use "Send your current location" from the {site.name} office.',
        )
    distance = distance_m(latitude, longitude, site.latitude, site.longitude)
    if distance > site.radius_m:
        raise OffSiteError(
            f"Attendance not recorded: you are not at the {site.name} office. "
            "Share your current location from the office to mark attendance.",
            distance_m=distance,
        )
    return {
        "site": site.name,
        "distance_m": round(distance),
        "radius_m": site.radius_m,
        "status": ON_SITE,
    }
