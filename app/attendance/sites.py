"""Where an employee must be standing for a punch to count.

Each office is a point and a radius. An employee whose phone number or branch
names an office is held to the offices: a punch must carry a location inside
the radius of any located office, or it is refused and nothing is recorded.
Any office counts, not only their own, so staff listed under the wrong office
(or falling to it by desk) are not locked out while sitting at their desk.

An office without coordinates enforces nothing. That keeps a half-configured
office from refusing everybody, but it also means an office is not protected
until its pin is filled in below.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

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


# Pins supplied by the user from Google Maps on 2026-10-07.
SITES: dict[str, Site] = {
    MOUNT_ROAD: Site(
        name=MOUNT_ROAD,
        address="758/MF Mount Chambers, Mount Road, Vasan Ave, Anna Salai, Thousand Lights, Chennai 600002",
        latitude=13.05976,
        longitude=80.25873,
        radius_m=300,
    ),
    ROYAPETTAH: Site(
        name=ROYAPETTAH,
        address="Modern Tower, 23 Westcott Road, Royapettah, Chennai 600014",
        latitude=13.058932,
        longitude=80.264005,
        radius_m=350,
    ),
}

#: Numbers whose office is fixed regardless of their branch, by the last ten digits.
PHONE_SITES: dict[str, str] = {
    **dict.fromkeys(
        ("9884447455", "7448337753", "9967926889", "8925726036", "9884949735"),
        MOUNT_ROAD,
    ),
    **dict.fromkeys(
        ("7806822702", "8438588507", "8870277929", "9884448455", "6381748354", "7200786679"),
        ROYAPETTAH,
    ),
}


class OffSiteError(ValueError):
    """The punch carried no location, or one outside the employee's office."""

    def __init__(self, message: str, *, distance_m: float | None = None):
        super().__init__(message)
        #: How far away the punch was made, for the log; never shown to staff.
        self.distance_m = distance_m


def _phone_key(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:]


def site_for(employee) -> Site | None:
    """The office this employee must punch from, if any."""
    listed = PHONE_SITES.get(_phone_key(getattr(employee, "phone", None)))
    if listed:
        return SITES[listed]
    return SITES.get(branch_of(employee))


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def check_on_site(employee, latitude: float | None, longitude: float | None) -> dict | None:
    """Refuse a punch made away from every office.

    Returns what was checked, for the event's evidence, or None when this
    employee has no located office to be held to.
    """
    site = site_for(employee)
    if site is None or not site.located:
        return None
    if latitude is None or longitude is None:
        raise OffSiteError(
            f"Location is required. Share your current location from the {site.name} office to mark attendance."
        )
    # Their own office first, so a punch inside both radii is filed under it.
    offices = [site, *(other for other in SITES.values() if other.located and other is not site)]
    distances = [(office, distance_m(latitude, longitude, office.latitude, office.longitude)) for office in offices]
    for office, distance in distances:
        if distance <= office.radius_m:
            return {"site": office.name, "distance_m": round(distance), "radius_m": office.radius_m}
    raise OffSiteError(
        "Attendance not recorded. You are at the wrong location.",
        distance_m=round(min(distance for _, distance in distances)),
    )
