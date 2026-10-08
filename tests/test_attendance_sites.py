"""A punch only counts when it is made from the employee's office."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import mongomock
import pytest

from app.api.routes import app as _app  # noqa: F401
from fastapi import HTTPException

from app.attendance import sites
from app.attendance.api import WhatsAppAttendanceEvent, record_punch, whatsapp_private_attendance
from app.attendance.models import PunchRequest
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.branches import MOUNT_ROAD, ROYAPETTAH
from app.db.users import STAFF_ROLE, User

# Test pins: one point per office, a few kilometres apart.
MOUNT = (13.0600, 80.2600)
ROYA = (13.0560, 80.2645)
TEST_SITES = {
    MOUNT_ROAD: sites.Site(MOUNT_ROAD, "Mount Road", *MOUNT, radius_m=300),
    ROYAPETTAH: sites.Site(ROYAPETTAH, "Royapettah", *ROYA, radius_m=350),
}


def _north_of(point, metres):
    return point[0] + metres / 111_320, point[1]


class FakeUsers:
    def __init__(self):
        self.members = {
            # Listed under Royapettah by number, although their desk is Mount Road.
            "listed": User(id="listed", email="l@example.com", name="Listed Person",
                           role=STAFF_ROLE, staff_code="AE010", phone="+91 7806822702"),
            "unlisted": User(id="unlisted", email="u@example.com", name="Other Person",
                             role=STAFF_ROLE, staff_code="AE011", phone="+91 9000000001",
                             branch=MOUNT_ROAD),
        }

    def get(self, user_id):
        return self.members.get(user_id)

    def list_employees(self, include_inactive=False):
        return list(self.members.values())


@pytest.fixture()
def env():
    repository = AttendanceRepository(mongomock.MongoClient()["attendance-sites"])
    service = AttendanceService(repository)
    with patch("app.attendance.api.users", FakeUsers()), \
         patch("app.attendance.api.service", return_value=service), \
         patch.object(sites, "SITES", TEST_SITES):
        yield service


def _event(location, message_id="loc-1"):
    return WhatsAppAttendanceEvent(
        message_id=message_id, sender_phone="917806822702", stated_name="Listed",
        action="check_in", occurred_at=datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc),
        latitude=location[0] if location else None,
        longitude=location[1] if location else None,
        command_message_id="cmd-1",
    )


def test_a_listed_number_is_held_to_its_listed_office():
    with patch.object(sites, "SITES", TEST_SITES):
        employee = FakeUsers().get("listed")
        assert sites.site_for(employee).name == ROYAPETTAH
        assert sites.site_for(FakeUsers().get("unlisted")).name == MOUNT_ROAD


def test_a_punch_inside_the_radius_is_recorded_with_where_it_was_made(env):
    result = whatsapp_private_attendance(_event(_north_of(ROYA, 340)))
    assert result["status"] == "recorded"
    evidence = result["event"]["evidence"]
    assert evidence["metadata"]["site"]["site"] == ROYAPETTAH
    assert evidence["metadata"]["site"]["distance_m"] <= 350
    assert evidence["latitude"] is not None


def test_a_punch_outside_the_radius_is_refused_and_not_recorded(env):
    with pytest.raises(HTTPException) as refused:
        whatsapp_private_attendance(_event(_north_of(ROYA, 400)))
    assert refused.value.status_code == 403
    assert refused.value.detail == "Attendance not recorded. You are at the wrong location."
    assert env.repository.events_for_day("listed", datetime(2026, 10, 5).date()) == []


def test_the_other_office_counts_too(env):
    # Listed under Royapettah but punching from Mount Road: still at an office.
    result = whatsapp_private_attendance(_event(MOUNT))
    assert result["status"] == "recorded"
    assert result["event"]["evidence"]["metadata"]["site"]["site"] == MOUNT_ROAD


def test_a_punch_without_a_location_is_refused(env):
    with pytest.raises(HTTPException) as refused:
        whatsapp_private_attendance(_event(None))
    assert refused.value.status_code == 403
    assert "Location is required" in refused.value.detail


def test_a_self_punch_from_the_web_is_held_to_the_office_too(env):
    away = PunchRequest(action="check_in", idempotency_key="web-1",
                        evidence=dict(zip(("latitude", "longitude"), _north_of(MOUNT, 2000))))
    with pytest.raises(HTTPException) as refused:
        record_punch(away, {"id": "unlisted", "role": STAFF_ROLE})
    assert refused.value.status_code == 403
    here = PunchRequest(action="check_in", idempotency_key="web-2",
                        evidence={"latitude": MOUNT[0], "longitude": MOUNT[1]})
    assert record_punch(here, {"id": "unlisted", "role": STAFF_ROLE})["status"] == "recorded"


def test_an_office_without_a_pin_enforces_nothing(env):
    unpinned = {**TEST_SITES, ROYAPETTAH: sites.Site(ROYAPETTAH, "Royapettah", None, None, 350)}
    with patch.object(sites, "SITES", unpinned):
        assert whatsapp_private_attendance(_event(None))["status"] == "recorded"
