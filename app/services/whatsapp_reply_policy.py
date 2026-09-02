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

from app.db.dedup import normalize_phone
from app.db.mongo import get_db
from app.db.users import UserRepository
from app.logging_config import get_logger

log = get_logger(__name__)


def reply_policy(
    phone: str,
    *,
    user_repository: UserRepository,
    database: Any | None = None,
) -> dict[str, str | bool]:
    """Return the bot's reply decision without exposing the matched contact.

    All user accounts are checked, including inactive staff and administrators:
    deactivation changes access and allocation, not whether a number belongs to
    the agency.  Every Sourcing Hub row is checked for the same reason; an
    inactive commercial relationship is still not a candidate conversation.
    """
    phone_key = normalize_phone(phone)
    if not phone_key:
        return {
            "should_reply": False,
            "action": "ignore",
            "reason": "invalid_sender_number",
        }

    try:
        for member in user_repository.list_all(include_inactive=True):
            if normalize_phone(member.phone) == phone_key:
                return {
                    "should_reply": False,
                    "action": "ignore",
                    "reason": "internal_user_number",
                }

        db = database if database is not None else get_db()
        sourcing = db["sourcing_clients"]
        projection = {"_id": 0, "phone": 1, "contacts.phone": 1}
        for account in sourcing.find({}, projection):
            phones = [account.get("phone")]
            phones.extend(
                contact.get("phone")
                for contact in account.get("contacts", [])
                if isinstance(contact, dict)
            )
            if any(normalize_phone(contact_phone) == phone_key for contact_phone in phones):
                return {
                    "should_reply": False,
                    "action": "ignore",
                    "reason": "sourcing_contact_number",
                }
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
