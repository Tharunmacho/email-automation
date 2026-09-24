"""Inbound WhatsApp *group* traffic: timesheets and agent-posted profiles.

Where this sits
---------------
The CRM does not hold the WhatsApp credentials. A separate bot service owns the
Meta connection, the agency's number, the send budget and the rate limiter (see
`settings.wa_bot_url`), and forwards what it receives to this system over
service-key-authenticated endpoints. `POST /attendance/events` is the existing
example: the bot reads a private chat and posts a normalised attendance command
here.

Group traffic is meant to arrive the same way, through
`POST /whatsapp/group-events`, and this module is what happens next: recognise
the group, decide what that group is for, and hand the message to the existing
machinery for it.

The external blocker
--------------------
Nothing in this module can start working on its own, because nothing upstream
sends group messages yet.

The deployment's bot is built on the **WhatsApp Cloud API**, which delivers
one-to-one conversations only — its webhook has no group message event, and a
group has no identifier in any payload it sends. `WhatsAppReplyPolicyIn` names
this directly: "the sender identity *Meta* supplies with every inbound message".
So group delivery is not a feature this system can switch on; it needs either a
provider that exposes group messages (the On-Premises API, or a broker that
bridges one) or a different transport entirely, and either way the change is in
the bot service, not here.

What exists here is therefore the receiving half, deliberately:

* `GroupMessage` — a provider-neutral shape. It is not a guess at Meta's
  webhook format: it is what this system needs to know, which the bot maps onto
  from whatever the provider gives it. That mapping is the bot's job precisely
  so a provider change does not reach into the CRM.
* `GroupRegistry` — which groups are trusted, and what each is for.
* `route_group_message` — dispatch to the existing attendance and candidate
  intake paths.

`settings.whatsapp_group_intake_enabled` gates the ingress and defaults to off,
so the endpoint answers honestly (503, naming the blocker) rather than
pretending to accept traffic that cannot arrive. When the bot can forward
groups, the flag is the only thing that has to change.

Why a registry, and not "any group"
-----------------------------------
A group message is an instruction from whoever is in the group. Without a
registry, being added to a group would be enough to punch somebody else's
attendance or inject candidates into the CRM. A group therefore does nothing at
all until an administrator has registered it and said what it is for.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.logging_config import get_logger

log = get_logger(__name__)

COLLECTION = "whatsapp_groups"

#: What a registered group is used for. A group has exactly one purpose: the
#: same message cannot sensibly be both a punch and a candidate.
GROUP_PURPOSES = ("timesheet", "agent_profiles")

GroupPurpose = Literal["timesheet", "agent_profiles"]


class GroupIntakeError(Exception):
    """The message cannot be processed, with a reason fit to show a human."""

    def __init__(self, reason: str, *, code: str = "rejected"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class GroupIntakeDisabled(GroupIntakeError):
    """The ingress is switched off because nothing upstream can feed it."""


# --------------------------------------------------------------------------- #
#  The inbound contract
# --------------------------------------------------------------------------- #
class GroupAttachment(BaseModel):
    """A file posted with the message, already stored by the bot.

    The CRM fetches by `media_id` through the bot rather than holding provider
    credentials of its own — the same division of labour as everywhere else in
    this integration.
    """

    media_id: str = Field(min_length=1, max_length=200)
    filename: str = Field(default="", max_length=300)
    mime_type: str = Field(default="", max_length=150)
    sha256: str = Field(default="", max_length=64)


class GroupMessage(BaseModel):
    """One message the bot observed in a group, normalised.

    Every field here is something the CRM actually uses. If a provider cannot
    supply `group_id`, this integration cannot work at all — that is the single
    piece of metadata the whole feature turns on, and the reason the blocker
    above is a blocker rather than an inconvenience.
    """

    #: Stable per message. Doubles as the idempotency key, so a redelivered
    #: webhook neither punches twice nor creates a second candidate.
    message_id: str = Field(min_length=1, max_length=200)
    group_id: str = Field(min_length=1, max_length=200)
    #: Display name, for the audit trail. Groups get renamed; the id does not.
    group_name: str = Field(default="", max_length=300)
    sender_phone: str = Field(min_length=5, max_length=50)
    sender_name: str = Field(default="", max_length=150)
    text: str = Field(default="", max_length=8000)
    sent_at: datetime
    attachments: list[GroupAttachment] = Field(default_factory=list)
    #: Present so a misrouted private message is refused rather than silently
    #: processed under group rules.
    chat_type: Literal["group"] = "group"


@dataclass(frozen=True)
class GroupIntakeResult:
    """What the CRM did, in terms the bot can log or reply with."""

    action: str
    group_id: str
    detail: str = ""
    candidate_id: Optional[str] = None
    employee_id: Optional[str] = None
    duplicate: bool = False

    def to_public(self) -> dict:
        return {
            "action": self.action,
            "group_id": self.group_id,
            "detail": self.detail,
            "candidate_id": self.candidate_id,
            "employee_id": self.employee_id,
            "duplicate": self.duplicate,
        }


# --------------------------------------------------------------------------- #
#  The registry
# --------------------------------------------------------------------------- #
class GroupRegistry:
    """Which groups this system listens to, and what each one is for."""

    def __init__(self, collection=None):
        if collection is None:
            from app.db.mongo import get_db

            collection = get_db()[COLLECTION]
        self._coll = collection

    def register(
        self,
        *,
        group_id: str,
        purpose: GroupPurpose,
        name: str = "",
        party_id: str = "",
        party_name: str = "",
        registered_by: str = "",
    ) -> dict:
        if purpose not in GROUP_PURPOSES:
            raise GroupIntakeError(f"Unknown group purpose {purpose!r}", code="unknown_purpose")
        doc = {
            "_id": group_id,
            "group_id": group_id,
            "name": name.strip(),
            "purpose": purpose,
            # For an agent group: who the agency deals with. Stamped onto every
            # candidate the group produces so a profile's provenance survives
            # the group being renamed or closed.
            "party_id": party_id,
            "party_name": party_name.strip(),
            "active": True,
            "registered_by": registered_by,
            "updated_at": datetime.now(timezone.utc),
        }
        self._coll.replace_one({"_id": group_id}, doc, upsert=True)
        return self._public(doc)

    def get(self, group_id: str) -> Optional[dict]:
        return self._public(self._coll.find_one({"_id": group_id}))

    def list(self, *, active_only: bool = False) -> list[dict]:
        query = {"active": True} if active_only else {}
        return [self._public(row) for row in self._coll.find(query).sort("name", 1)]

    def deactivate(self, group_id: str) -> bool:
        result = self._coll.update_one(
            {"_id": group_id},
            {"$set": {"active": False, "updated_at": datetime.now(timezone.utc)}},
        )
        return result.matched_count > 0

    @staticmethod
    def _public(doc: dict | None) -> Optional[dict]:
        if not doc:
            return None
        row = dict(doc)
        row.pop("_id", None)
        return row


# --------------------------------------------------------------------------- #
#  Identity
# --------------------------------------------------------------------------- #
def name_key(value: str) -> str:
    return " ".join((value or "").casefold().split())


def resolve_employee(users, sender_phone: str, stated_name: str):
    """The one employee this sender is, or a refusal explaining why not.

    Shared with the private-chat attendance route rather than reimplemented:
    a WhatsApp number proves a handset, so the written name is checked against
    the CRM account as a second factor, and both paths have to agree on what
    counts as a match.
    """
    from app.db.dedup import normalize_phone

    sender = normalize_phone(sender_phone)
    matches = [
        employee
        for employee in users.list_employees(include_inactive=False)
        if sender and normalize_phone(employee.phone) == sender
    ]
    if len(matches) != 1:
        raise GroupIntakeError(
            "The sender's WhatsApp number is not linked to one active CRM employee",
            code="unknown_sender",
        )
    employee = matches[0]
    full_name = name_key(employee.name)
    allowed = {full_name, full_name.split(" ", 1)[0], name_key(employee.staff_code)}
    if name_key(stated_name) not in allowed:
        raise GroupIntakeError(
            "The written name does not match the sender's CRM employee account",
            code="name_mismatch",
        )
    return employee


# --------------------------------------------------------------------------- #
#  Handlers
# --------------------------------------------------------------------------- #
#: What a timesheet post has to say. Kept narrow on purpose: a group is noisy,
#: and anything this does not recognise is ignored rather than guessed at.
_CHECK_IN_WORDS = ("check in", "checkin", "check-in", "in", "login", "log in")
_CHECK_OUT_WORDS = ("check out", "checkout", "check-out", "out", "logout", "log out")


def read_punch_action(text: str) -> Optional[str]:
    """`"Ravi check in"` -> `"check_in"`. Unrecognised text is not a punch.

    Returning None rather than raising matters: a group carries conversation as
    well as punches, and "morning all" must be ignored quietly, not rejected
    loudly back at everybody in the group.
    """
    body = name_key(text)
    if not body:
        return None
    for word in _CHECK_OUT_WORDS:
        if body == word or body.endswith(f" {word}") or body.startswith(f"{word} "):
            return "check_out"
    for word in _CHECK_IN_WORDS:
        if body == word or body.endswith(f" {word}") or body.startswith(f"{word} "):
            return "check_in"
    return None


def handle_timesheet(message: GroupMessage, group: dict, *, users, attendance) -> GroupIntakeResult:
    """A punch posted in a team group.

    Goes through `AttendanceService.punch`, exactly as the private-chat route
    does, so every rule — shift bounds, the early check-in permission, one
    check-in a day, idempotency — applies identically whichever chat it came
    from. Nothing about attendance is re-decided here.
    """
    from app.attendance.models import PunchRequest

    action = read_punch_action(message.text)
    if not action:
        return GroupIntakeResult(
            action="ignored", group_id=message.group_id,
            detail="Not an attendance command",
        )

    employee = resolve_employee(users, message.sender_phone, message.sender_name)
    request = PunchRequest(
        action=action,
        idempotency_key=message.message_id,
        employee_id=employee.id,
        occurred_at=message.sent_at,
        source="whatsapp",
        evidence={
            "metadata": {
                "chat_type": "group",
                "group_id": message.group_id,
                "group_name": message.group_name or group.get("name", ""),
                "stated_name": message.sender_name,
            }
        },
    )
    event, created = attendance.punch(employee.id, request, allow_recorded_time=True)
    return GroupIntakeResult(
        action="recorded" if created else "duplicate",
        group_id=message.group_id,
        detail=f"{action.replace('_', ' ')} for {employee.name}",
        employee_id=employee.id,
        duplicate=not created,
    )


def handle_agent_profile(
    message: GroupMessage,
    group: dict,
    *,
    profile: Any = None,
    intake=None,
) -> GroupIntakeResult:
    """A candidate profile an agent posted in a group.

    Deliberately not the B2B enquiry path. An agent raising a manpower
    requirement is a `b2b_enquiries` row about a vacancy; an agent posting
    somebody's details is a person, and belongs in `candidates` with everybody
    else.

    Creation goes through `intake_whatsapp_candidate`, which already owns
    identity resolution, passport claiming and de-duplication — so a candidate
    an agent posts twice, or posts after registering themselves, is matched to
    the existing record rather than added beside it. The idempotency key is the
    group message id, so a redelivered webhook resolves to the same candidate.
    """
    from app.services.candidate_intake import intake_whatsapp_candidate

    if profile is None:
        profile = extract_profile(message)
    if profile is None:
        return GroupIntakeResult(
            action="ignored", group_id=message.group_id,
            detail="No candidate details could be read from this message",
        )

    intake = intake or intake_whatsapp_candidate
    result = intake(
        profile=profile,
        idempotency_key=f"whatsapp-group/{message.group_id}/{message.message_id}",
        # An agent posting on somebody's behalf has not been through the bot's
        # CV question, so the policy is not claimed either way here.
        cv_required_claim=None,
    )
    candidate_id = getattr(result, "candidate_id", None) or getattr(
        getattr(result, "candidate", None), "id", None
    )
    return GroupIntakeResult(
        action="candidate_created" if getattr(result, "created", True) else "candidate_updated",
        group_id=message.group_id,
        detail=f"Posted by {message.sender_name or message.sender_phone}",
        candidate_id=candidate_id,
        duplicate=not getattr(result, "created", True),
    )


def extract_profile(message: GroupMessage):
    """Read a candidate profile out of what the agent actually typed.

    Reuses the résumé parser rather than growing a second extractor: an agent's
    post is a short unstructured description of a person, which is the same
    problem a CV is, and a second implementation would drift from the first.

    Returns None when nothing usable is found, which is the common case in a
    group full of ordinary conversation.
    """
    from app.ai.resume_parser import ResumeParser

    text = (message.text or "").strip()
    if len(text) < 20:
        return None
    try:
        profile = ResumeParser().parse(text, hint="Candidate profile posted by an agent")
    except Exception as exc:  # noqa: BLE001 - a parse failure is not a crash
        log.warning("Group %s: could not read a profile from a message: %s", message.group_id, exc)
        return None
    return profile if getattr(profile, "full_name", None) else None


# --------------------------------------------------------------------------- #
#  Routing
# --------------------------------------------------------------------------- #
def route_group_message(
    message: GroupMessage,
    *,
    registry: GroupRegistry | None = None,
    users=None,
    attendance=None,
    enabled: bool | None = None,
) -> GroupIntakeResult:
    """Recognise the group, then hand the message to what that group is for."""
    if enabled is None:
        from app.config import settings

        enabled = settings.whatsapp_group_intake_enabled
    if not enabled:
        raise GroupIntakeDisabled(
            "WhatsApp group intake is disabled. The bot service cannot forward "
            "group messages on the WhatsApp Cloud API; see app/whatsapp/groups.py.",
            code="group_intake_disabled",
        )

    registry = registry or GroupRegistry()
    group = registry.get(message.group_id)
    if not group or not group.get("active", True):
        # Never an error the bot should retry: an unregistered group is a
        # decision nobody has made yet, not a failure.
        raise GroupIntakeError(
            "This WhatsApp group is not registered for intake", code="unregistered_group",
        )

    purpose = group.get("purpose")
    if purpose == "timesheet":
        if users is None or attendance is None:
            from app.api.routes import users as user_repository
            from app.attendance.repository import AttendanceRepository
            from app.attendance.service import AttendanceService

            users = users or user_repository
            attendance = attendance or AttendanceService(AttendanceRepository())
        return handle_timesheet(message, group, users=users, attendance=attendance)
    if purpose == "agent_profiles":
        return handle_agent_profile(message, group)
    raise GroupIntakeError(f"Group has no usable purpose ({purpose!r})", code="unknown_purpose")


def ensure_group_indexes() -> None:
    from pymongo import ASCENDING

    from app.db.mongo import ensure_index, get_db

    ensure_index(
        get_db()[COLLECTION],
        [("active", ASCENDING), ("purpose", ASCENDING)],
        "whatsapp_group_active_purpose",
    )
