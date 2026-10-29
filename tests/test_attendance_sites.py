"""A punch counts only from the employee's own office; anywhere else, nothing is recorded."""
from __future__ import annotations

import json
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
            # No branch typed in: Royapettah by their desk (Singapore and Malaysia).
            "listed": User(id="listed", email="sreya.adira@gmail.com", name="Listed Person",
                           role=STAFF_ROLE, staff_code="AE010", phone="+91 7806822702"),
            "unlisted": User(id="unlisted", email="u@example.com", name="Other Person",
                             role=STAFF_ROLE, staff_code="AE011", phone="+91 9000000001",
                             branch=MOUNT_ROAD),
            # A new hire created at Royapettah, on the general desk.
            "filed": User(id="filed", email="f@example.com", name="Filed Person",
                          role=STAFF_ROLE, staff_code="AE012", phone="+91 9000000002",
                          branch=ROYAPETTAH),
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


def _event(location, message_id="loc-1", location_unavailable=False,
           sender_phone="917806822702", stated_name="Listed"):
    return WhatsAppAttendanceEvent(
        message_id=message_id, sender_phone=sender_phone, stated_name=stated_name,
        action="check_in", occurred_at=datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc),
        latitude=location[0] if location else None,
        longitude=location[1] if location else None,
        command_message_id="cmd-1",
        location_unavailable=location_unavailable,
    )


def answered(response) -> tuple[int, dict]:
    """A private-attendance reply as (status, payload).

    The route answers with a `Response` rather than raising, because every
    refusal now carries a machine-readable `code` the bot branches on.
    """
    return response.status_code, json.loads(response.body)


def test_the_office_is_the_branch_the_crm_files_them_under():
    with patch.object(sites, "SITES", TEST_SITES):
        assert sites.site_for(FakeUsers().get("listed")).name == ROYAPETTAH
        assert sites.site_for(FakeUsers().get("unlisted")).name == MOUNT_ROAD


def test_a_new_hire_created_at_royapettah_is_on_site_there(env):
    """The reported fault: created at Royapettah, filed off site at Mount Road.

    The office came from a fixed list of phone numbers, and a new hire's number
    was never on it.
    """
    status, result = answered(whatsapp_private_attendance(
        _event(ROYA, sender_phone="919000000002", stated_name="Filed")
    ))
    assert status == 201
    assert result["location_status"] == sites.ON_SITE
    assert result["site"] == ROYAPETTAH


def test_a_branch_without_an_office_punches_from_mount_road():
    elsewhere = User(id="elsewhere", email="e@example.com", name="Elsewhere Person",
                     role=STAFF_ROLE, staff_code="AE013", phone="+91 9000000003",
                     branch="Velachery")
    with patch.object(sites, "SITES", TEST_SITES):
        assert sites.site_for(elsewhere).name == MOUNT_ROAD


def test_a_punch_inside_the_radius_is_recorded_with_where_it_was_made(env):
    status, result = answered(whatsapp_private_attendance(_event(_north_of(ROYA, 340))))
    assert status == 201
    assert result["status"] == "recorded"
    evidence = result["event"]["evidence"]
    assert evidence["metadata"]["site"]["site"] == ROYAPETTAH
    assert evidence["metadata"]["site"]["distance_m"] <= 350
    assert evidence["latitude"] is not None
    # What the bot reads back to the employee: office time, not UTC, and where.
    assert result["recorded_time"] == "09:30"
    assert result["recorded_date"] == "2026-10-05"
    assert result["site"] == ROYAPETTAH


def _nothing_recorded(service, employee_id="listed") -> bool:
    return service.repository.events_for_day(employee_id, datetime(2026, 10, 5).date()) == []


def test_a_punch_outside_the_radius_is_refused_and_not_recorded(env):
    """No attendance and no manager review: the day stays absent.

    The user set this on 2026-10-29, replacing a spell in which an off-site
    punch was recorded and flagged for a manager.
    """
    status, refused = answered(whatsapp_private_attendance(_event(_north_of(ROYA, 500))))
    assert status == 403
    assert refused["code"] == "off_site"
    assert "not at the Royapettah office" in refused["detail"]
    assert _nothing_recorded(env)


def test_a_live_location_is_refused_with_its_own_explanation(env):
    """WhatsApp never delivers live location, so it can never be placed.

    It reaches the bot as an `unsupported` message with no coordinates at all,
    and the bot says so with `location_unavailable`. Asking again the same way
    would loop, so the employee is told what to send instead.
    """
    status, refused = answered(
        whatsapp_private_attendance(_event(None, location_unavailable=True))
    )
    assert status == 403
    assert refused["code"] == "off_site"
    assert "Send your current location" in refused["detail"]
    assert _nothing_recorded(env)


def test_the_other_office_is_refused(env):
    # Filed under Royapettah but punching from Mount Road: not their office.
    status, refused = answered(whatsapp_private_attendance(_event(MOUNT)))
    assert status == 403
    assert refused["code"] == "off_site"
    assert _nothing_recorded(env)


def test_a_punch_without_a_location_is_refused(env):
    """And says so under its own code, so the bot asks for one.

    This is the first step of every punch, not an error path: "Listed - check
    in" arrives with no location and has to come back as a request for one.
    `off_site` would have the bot tell them they were in the wrong place.
    """
    status, refused = answered(whatsapp_private_attendance(_event(None)))
    assert status == 403
    assert refused["code"] == "location_required"
    assert "Location is required" in refused["detail"]


def test_a_self_punch_from_the_web_is_held_to_the_same_office(env):
    away = PunchRequest(action="check_in", idempotency_key="web-1",
                        evidence=dict(zip(("latitude", "longitude"), _north_of(MOUNT, 2000))))
    with pytest.raises(HTTPException) as refused:
        record_punch(away, {"id": "unlisted", "role": STAFF_ROLE})
    assert refused.value.status_code == 403
    assert _nothing_recorded(env, "unlisted")
    here = PunchRequest(action="check_in", idempotency_key="web-2",
                        evidence={"latitude": MOUNT[0], "longitude": MOUNT[1]})
    recorded = record_punch(here, {"id": "unlisted", "role": STAFF_ROLE})
    assert recorded["event"]["evidence"]["metadata"]["site"]["status"] == sites.ON_SITE


def test_the_opening_command_still_goes_back_for_a_location(env):
    """`location_required`, not `off_site`, and the reason `location_unavailable` exists.

    The bare command is the first step of every punch. Refusing it as off site
    would tell the employee they were in the wrong place before they had been
    asked where they were.
    """
    status, refused = answered(whatsapp_private_attendance(_event(None)))
    assert status == 403
    assert refused["code"] == "location_required"


def test_an_office_without_a_pin_enforces_nothing(env):
    unpinned = {**TEST_SITES, ROYAPETTAH: sites.Site(ROYAPETTAH, "Royapettah", None, None, 350)}
    with patch.object(sites, "SITES", unpinned):
        status, result = answered(whatsapp_private_attendance(_event(None)))
    assert status == 201
    assert result["status"] == "recorded"
