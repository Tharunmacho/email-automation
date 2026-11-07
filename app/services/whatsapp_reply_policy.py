"""Decide whether the WhatsApp bot may reply to an inbound sender.

Sourcing contacts and internal users share the public WhatsApp number with
candidates, but they are not candidates.  Letting the bot start its recruitment
flow with either group creates misleading conversations and, eventually,
candidate records for people already known to the agency.

Phone values are compared with the same normalisation used throughout the CRM,
so ``+91 98765 43210``, ``919876543210`` and ``98765-43210`` match.  The lookup
is intentionally fail-closed: if the CRM cannot prove that a sender is external,
the bot must stay silent rather than risk replying to an internal contact.
"""
from __future__ import annotations

from typing import Any

from app.agent_whatsapp import handling_staff
from app.db.dedup import normalize_phone
from app.db.mongo import get_db
from app.db.users import UserRepository
from app.logging_config import get_logger

log = get_logger(__name__)


def _phone_key(value: Any) -> str | None:
    return normalize_phone(str(value)) if value not in (None, "") else None


def _sourcing_match(db: Any, phone_key: str) -> tuple[str | None, dict | None]:
    """The Sourcing Hub type this number belongs to, and the agent row if it is an agent.

    ``(None, None)`` if no row has the number. Every number on a row counts: the
    row's own phone and every contact's, so a client is matched on all its
    contacts whether it has one or several. ``"agent"`` wins when the number
    sits on more than one row, because only an agent's messages go to the bot's
    agent workers (``app/agent_resumes.py`` applies the same rule). Clients,
    associations and rows with no type are all left for a person, and the bot
    sends them nothing.

    Of several agent rows with the number, the first one with a staff member
    assigned is the one returned.
    """
    found: str | None = None
    agent: dict | None = None
    projection = {"_id": 0, "type": 1, "phone": 1, "contacts.phone": 1, "handlingStaff": 1}
    for account in db["sourcing_clients"].find({}, projection):
        phones = [account.get("phone")]
        phones.extend(
            contact.get("phone")
            for contact in account.get("contacts") or []
            if isinstance(contact, dict)
        )
        if not any(_phone_key(contact_phone) == phone_key for contact_phone in phones):
            continue
        kind = str(account.get("type") or "").strip().lower() or "unknown"
        if kind == "agent":
            if isinstance(account.get("handlingStaff"), dict):
                return "agent", account
            agent = agent or account
            continue
        found = found or kind
    return ("agent", agent) if agent is not None else (found, None)


def reply_policy(
    phone: str,
    *,
    user_repository: UserRepository,
    database: Any | None = None,
) -> dict[str, Any]:
    """Return the bot's reply decision without exposing the matched contact.

    The administrator's suppression list is checked first and wins over every
    other match: a suppressed number gets ``suppressed_number`` even when it is
    also staff or a Sourcing Hub contact. When it came last, a suppressed agent
    was reported as an agent and the bot's agent workers answered them.

    All user accounts are checked, including inactive staff and administrators:
    deactivation changes access and allocation, not whether a number belongs to
    the agency.  Every Sourcing Hub row is checked for the same reason; an
    inactive commercial relationship is still not a candidate conversation.
    A Sourcing Hub match also reports ``sourcing_type`` so the bot can hand
    agents' files and questions to its agent workers and send nobody else
    anything. An agent's match also reports ``agent_staff``, the name and
    number of the staff member who handles that agent, when one is assigned:
    the bot points the agent to them.
    """
    phone_key = normalize_phone(phone)
    if not phone_key:
        return {
            "should_reply": False,
            "action": "ignore",
            "reason": "invalid_sender_number",
        }

    try:
        db = database if database is not None else get_db()

        if db["bot_suppression_numbers"].count_documents(
            {"phone_key": phone_key}, limit=1
        ):
            return {
                "should_reply": False,
                "action": "ignore",
                "reason": "suppressed_number",
            }

        for member in user_repository.list_all(include_inactive=True):
            if normalize_phone(member.phone) == phone_key:
                return {
                    "should_reply": False,
                    "action": "ignore",
                    "reason": "internal_user_number",
                }

        sourcing_type, agent = _sourcing_match(db, phone_key)
        if sourcing_type is not None:
            decision: dict[str, Any] = {
                "should_reply": False,
                "action": "ignore",
                "reason": "sourcing_contact_number",
                "sourcing_type": sourcing_type,
            }
            if agent is not None:
                staff = handling_staff(agent, getattr(user_repository, "get", None))
                if staff["staff_name"] or staff["staff_phone"]:
                    decision["agent_staff"] = {
                        "name": staff["staff_name"],
                        "phone": staff["staff_phone"],
                    }
            return decision
    except Exception as exc:  # noqa: BLE001 - this boundary must fail closed
        log.error("WhatsApp reply-policy lookup failed; suppressing reply: %s", exc)
        return {
            "should_reply": False,
            "action": "ignore",
            "reason": "policy_lookup_unavailable",
        }

    return {
        "should_reply": True,
        "action": "continue",
        "reason": "external_sender",
    }
