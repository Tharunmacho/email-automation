"""Group intake: the receiving half, and the blocker in front of it.

The ingress is disabled because the bot cannot forward group messages on the
WhatsApp Cloud API (see `app/whatsapp/groups.py`). These tests cover the part
this system does own — recognising a group, refusing one it does not know, and
handing a recognised message to the existing attendance and candidate paths —
so that flipping `whatsapp_group_intake_enabled` is all that remains when the
upstream can feed it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import mongomock
import pytest

from app.attendance.models import Shift
from app.attendance.repository import AttendanceRepository
from app.attendance.service import AttendanceService
from app.whatsapp.groups import (
    GroupIntakeDisabled,
    GroupIntakeError,
    GroupMessage,
    GroupRegistry,
    read_punch_action,
    resolve_employee,
    route_group_message,
)


class FakeEmployee:
    def __init__(self, user_id, name, phone, staff_code="", active=True):
        self.id, self.name, self.phone = user_id, name, phone
        self.staff_code, self.active = staff_code, active
        self.role = "staff"


class FakeUsers:
    def __init__(self, employees):
        self._employees = employees

    def list_employees(self, include_inactive=False):
        return [e for e in self._employees if include_inactive or e.active]


RAVI = FakeEmployee("staff-1", "Ravi Kumar", "+91 98765 43210", "ADR-001")
PRIYA = FakeEmployee("staff-2", "Priya Nair", "+91 98765 11111", "ADR-002")
USERS = FakeUsers([RAVI, PRIYA])


def message(**overrides) -> GroupMessage:
    body = {
        "message_id": "wamid.1",
        "group_id": "group-timesheet",
        "group_name": "Chennai Team",
        "sender_phone": "+91 98765 43210",
        "sender_name": "Ravi",
        "text": "Ravi check in",
        "sent_at": datetime(2026, 9, 7, 4, 30, tzinfo=timezone.utc),
    }
    body.update(overrides)
    return GroupMessage(**body)


@pytest.fixture()
def registry():
    registry = GroupRegistry(collection=mongomock.MongoClient()["wa"]["whatsapp_groups"])
    registry.register(group_id="group-timesheet", purpose="timesheet", name="Chennai Team")
    registry.register(
        group_id="group-agents", purpose="agent_profiles", name="Agent Desk",
        party_id="agent-7", party_name="Gulf Manpower",
    )
    return registry


@pytest.fixture()
def attendance():
    return AttendanceService(AttendanceRepository(mongomock.MongoClient()["wa-att"]))


def route(msg, registry, attendance=None, **kwargs):
    return route_group_message(
        msg, registry=registry, users=USERS, attendance=attendance, enabled=True, **kwargs,
    )


# --------------------------------------------------------------------------- #
#  The blocker
# --------------------------------------------------------------------------- #
def test_the_ingress_is_disabled_and_says_why(registry):
    """Disabled by default, and the refusal names the upstream limitation."""
    with pytest.raises(GroupIntakeDisabled) as raised:
        route_group_message(message(), registry=registry, enabled=False)
    assert "cannot forward group messages" in str(raised.value).lower()


def test_the_flag_defaults_to_off():
    from app.config import settings

    assert settings.whatsapp_group_intake_enabled is False


# --------------------------------------------------------------------------- #
#  The registry is the trust boundary
# --------------------------------------------------------------------------- #
def test_an_unregistered_group_is_refused(registry):
    with pytest.raises(GroupIntakeError) as raised:
        route(message(group_id="some-random-group"), registry)
    assert raised.value.code == "unregistered_group"


def test_a_deactivated_group_stops_being_listened_to(registry, attendance):
    assert route(message(), registry, attendance).action == "recorded"
    registry.deactivate("group-timesheet")
    with pytest.raises(GroupIntakeError) as raised:
        route(message(message_id="wamid.2"), registry, attendance)
    assert raised.value.code == "unregistered_group"


def test_a_private_message_cannot_be_posted_as_a_group_event():
    with pytest.raises(Exception):
        GroupMessage(
            message_id="m", group_id="g", sender_phone="+911", sent_at=datetime.now(timezone.utc),
            chat_type="private",
        )


# --------------------------------------------------------------------------- #
#  Timesheet
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Ravi check in", "check_in"),
        ("check in", "check_in"),
        ("Checkin", "check_in"),
        ("Ravi check out", "check_out"),
        ("log out", "check_out"),
        ("Good morning everyone", None),
        ("", None),
        ("can someone check the invoice", None),
    ],
)
def test_only_recognisable_commands_are_punches(text, expected):
    assert read_punch_action(text) == expected


def test_group_conversation_is_ignored_rather_than_rejected(registry, attendance):
    """A team group carries chatter; it must not be answered with errors."""
    result = route(message(text="Morning all, running late"), registry, attendance)
    assert result.action == "ignored"


def test_a_group_punch_reaches_the_attendance_ledger(registry, attendance):
    result = route(message(), registry, attendance)
    assert result.action == "recorded"
    assert result.employee_id == "staff-1"

    # The punch is on the ledger and the day reads it back. The resulting
    # status depends on whether a check-out followed and how long ago the day
    # was, which is the engine's business, not this integration's.
    day = attendance.day("staff-1", date(2026, 9, 7))
    assert day["check_in"] is not None
    assert day["employee_id"] == "staff-1"


def test_a_group_punch_records_where_it_came_from(registry, attendance):
    route(message(), registry, attendance)
    event = attendance.repository.events_for_day("staff-1", date(2026, 9, 7))[0]
    metadata = event["evidence"]["metadata"]
    assert metadata["chat_type"] == "group"
    assert metadata["group_id"] == "group-timesheet"
    assert metadata["group_name"] == "Chennai Team"


def test_a_redelivered_group_message_does_not_punch_twice(registry, attendance):
    first = route(message(), registry, attendance)
    second = route(message(), registry, attendance)
    assert first.action == "recorded"
    assert second.duplicate is True
    assert len(attendance.repository.events_for_day("staff-1", date(2026, 9, 7))) == 1


def test_a_sender_the_crm_does_not_know_is_refused(registry, attendance):
    with pytest.raises(GroupIntakeError) as raised:
        route(message(sender_phone="+91 90000 00000"), registry, attendance)
    assert raised.value.code == "unknown_sender"


def test_the_written_name_must_match_the_account(registry, attendance):
    """A handset proves a phone, not a person, so the name is a second factor."""
    with pytest.raises(GroupIntakeError) as raised:
        route(message(sender_name="Priya"), registry, attendance)
    assert raised.value.code == "name_mismatch"


def test_nobody_can_punch_for_somebody_else_from_a_group(registry, attendance):
    """Posting another person's name from your own handset does not punch them."""
    with pytest.raises(GroupIntakeError):
        route(message(text="Priya check in", sender_name="Priya"), registry, attendance)
    assert attendance.repository.events_for_day("staff-2", date(2026, 9, 7)) == []


def test_group_attendance_obeys_the_same_rules_as_private_attendance(registry, attendance):
    """An unapproved early arrival is refused in a group exactly as in a chat."""
    early = message(
        message_id="wamid.early",
        sent_at=datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc),  # 08:30 IST
    )
    with pytest.raises(ValueError, match="early check-in"):
        route(early, registry, attendance)


def test_a_planned_sunday_duty_is_punchable_from_a_group(registry, attendance):
    attendance.repository.set_employee_policy("staff-1", {"weekly_off_pattern": "sunday"})
    attendance.repository.assign_shift({
        "employee_id": "staff-1", "effective_from": "2026-09-06",
        "shift": Shift().model_dump(), "kind": "planned_duty",
    })
    result = route(
        message(message_id="wamid.sun", sent_at=datetime(2026, 9, 6, 4, 30, tzinfo=timezone.utc)),
        registry, attendance,
    )
    assert result.action == "recorded"
    assert attendance.day("staff-1", date(2026, 9, 6))["status"] != "WO"


# --------------------------------------------------------------------------- #
#  Agent-posted profiles
# --------------------------------------------------------------------------- #
def test_an_agent_profile_goes_to_candidate_intake_not_b2b(registry):
    """An agent posting a person creates a candidate, never a B2B enquiry."""
    from app.core.models import CandidateProfile

    captured = {}

    def fake_intake(*, profile, idempotency_key, cv_required_claim=None, **_kwargs):
        captured["profile"] = profile
        captured["idempotency_key"] = idempotency_key

        class Result:
            candidate_id = "cand-new"
            created = True

        return Result()

    from app.whatsapp.groups import handle_agent_profile

    profile = CandidateProfile(is_resume=True, confidence=0.8, full_name="Suresh Babu")
    result = handle_agent_profile(
        message(group_id="group-agents", text="Posting a welder profile"),
        registry.get("group-agents"),
        profile=profile,
        intake=fake_intake,
    )
    assert result.action == "candidate_created"
    assert result.candidate_id == "cand-new"
    assert captured["profile"].full_name == "Suresh Babu"
    # Keyed on the group message, so a redelivery resolves to the same person.
    assert captured["idempotency_key"] == "whatsapp-group/group-agents/wamid.1"


def test_the_same_agent_post_twice_is_one_candidate(registry):
    """De-duplication is the intake service's, not a second copy of it here."""
    from app.core.models import CandidateProfile
    from app.whatsapp.groups import handle_agent_profile

    seen = {}

    def fake_intake(*, profile, idempotency_key, **_kwargs):
        created = idempotency_key not in seen
        seen[idempotency_key] = "cand-1"

        class Result:
            candidate_id = "cand-1"

        Result.created = created
        return Result()

    profile = CandidateProfile(is_resume=True, confidence=0.8, full_name="Suresh Babu")
    args = (message(group_id="group-agents"), registry.get("group-agents"))
    first = handle_agent_profile(*args, profile=profile, intake=fake_intake)
    second = handle_agent_profile(*args, profile=profile, intake=fake_intake)

    assert first.action == "candidate_created"
    assert second.action == "candidate_updated"
    assert second.duplicate is True
    assert len(seen) == 1


def test_a_message_with_no_readable_profile_is_ignored(registry):
    from app.whatsapp.groups import handle_agent_profile

    result = handle_agent_profile(
        message(group_id="group-agents", text="ok"),
        registry.get("group-agents"),
        profile=None,
        intake=lambda **_kwargs: pytest.fail("intake must not be called"),
    )
    assert result.action == "ignored"


def test_the_agent_group_records_which_party_posted(registry):
    group = registry.get("group-agents")
    assert group["party_id"] == "agent-7"
    assert group["party_name"] == "Gulf Manpower"


# --------------------------------------------------------------------------- #
#  Private attendance must not regress
# --------------------------------------------------------------------------- #
def test_the_shared_resolver_still_authenticates_a_private_sender():
    """Private chat and group intake resolve a sender through one function."""
    assert resolve_employee(USERS, "+91 98765 43210", "Ravi").id == "staff-1"
    assert resolve_employee(USERS, "919876543210", "Ravi Kumar").id == "staff-1"
    assert resolve_employee(USERS, "+91 98765 43210", "ADR-001").id == "staff-1"
    with pytest.raises(GroupIntakeError):
        resolve_employee(USERS, "+91 98765 43210", "Somebody Else")
