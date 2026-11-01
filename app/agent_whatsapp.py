"""Telling the Sourcing Hub's agents about a new job order on WhatsApp.

When a job order is created, every active agent in the Sourcing Hub gets one
WhatsApp message about it, from Rafi's line. The CRM decides who and when; the
bot (`POST /api/agent-job-order`) owns the template, the line and the send
budget, the same split as `app/staff_whatsapp.py`.

* **Once per job order.** The order id is claimed in
  `agent_job_order_broadcasts` before anything is sent, so a double-clicked
  save or a second create with the same id does not message agents twice.
* **Agents only.** Sourcing Hub rows with `type == "agent"` and
  `status == "ACTIVE"`, one message per agent, to the primary contact. Clients
  and associations are never messaged.
* **No client name.** Agents get the same facts the public Current Jobs page
  shows: title, country, vacancies, salary and experience, with the order's
  poster as the picture (required on a new order) — plus the order's agent
  terms (`terms`: gender, process time and B2B rate), its Remarks
  (`description`), and the staff member it is assigned to (`assignedStaff`:
  name and phone, for agents to contact), which are for agents and never go on
  the website. Which of them the WhatsApp message shows is the bot's Meta
  template's business (`agentJobOrderParameters` in the bot).
* **Off the request path.** The save returns at once; sending runs in a
  background thread. Nothing here raises.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.config import settings
from app.db.dedup import normalize_phone
from app.logging_config import get_logger
from app.staff_whatsapp import _post, relay_enabled
from app.website_jobs import poster_url

log = get_logger(__name__)

AGENT_JOB_ORDER_PATH = "/api/agent-job-order"
BROADCAST_COLLECTION = "agent_job_order_broadcasts"


def broadcast_enabled() -> bool:
    """Both the switch and a bot to talk to."""
    return bool(settings.agent_job_order_whatsapp) and relay_enabled()


def _primary_contact(record: Dict[str, Any]) -> tuple[str, str]:
    """(name, phone) of the agent's primary contact.

    `contacts` lists the primary contact first; the top-level `contact` and
    `phone` mirror it for records written before `contacts` existed.
    """
    for contact in record.get("contacts") or []:
        if isinstance(contact, dict) and str(contact.get("phone") or "").strip():
            return str(contact.get("name") or "").strip(), str(contact["phone"]).strip()
    return str(record.get("contact") or "").strip(), str(record.get("phone") or "").strip()


def agent_recipients(database) -> List[Dict[str, str]]:
    """Active Sourcing Hub agents with a phone, one entry per number."""
    projection = {"_id": 0, "id": 1, "name": 1, "type": 1, "status": 1,
                  "contact": 1, "phone": 1, "contacts": 1}
    recipients: List[Dict[str, str]] = []
    seen: set[str] = set()
    for record in database["sourcing_clients"].find({}, projection):
        if str(record.get("type") or "").strip().lower() != "agent":
            continue
        # The Sourcing Hub shows anything but ACTIVE as Pending or Inactive.
        if str(record.get("status") or "").strip().upper() != "ACTIVE":
            continue
        contact_name, phone = _primary_contact(record)
        key = normalize_phone(phone)
        if not key or key in seen:
            continue
        seen.add(key)
        recipients.append({
            "agent_id": str(record.get("id") or ""),
            "agent_name": contact_name or str(record.get("name") or "").strip(),
            "phone": phone,
        })
    return recipients


def _claim(database, order_id: str) -> bool:
    """True exactly once per job order id. `_id` is the order id, so it is unique."""
    from pymongo.errors import DuplicateKeyError

    try:
        database[BROADCAST_COLLECTION].insert_one({
            "_id": order_id,
            "claimed_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def staff_facts(order: Dict[str, Any]) -> Dict[str, str]:
    """The staff member the order is assigned to, as agents would contact them."""
    staff = order.get("assignedStaff") if isinstance(order.get("assignedStaff"), dict) else {}
    return {"staff_name": _text(staff.get("name")), "staff_phone": _text(staff.get("phone"))}


def terms_facts(order: Dict[str, Any]) -> Dict[str, Any]:
    """The order's agent terms (`terms` on the job order) for the bot, every key always present."""
    terms = order.get("terms") if isinstance(order.get("terms"), dict) else {}
    return {
        "industry": _text(order.get("industry")),
        "gender": _text(terms.get("gender")),
        "process_time": _text(terms.get("processTime")),
        "b2b_rate": _text(terms.get("b2bRate")),
    }


def broadcast_job_order(order: Dict[str, Any], *, database=None) -> Dict[str, Any]:
    """Message every active agent about one newly created job order. Never raises."""
    order_id = _text(order.get("id"))
    try:
        if not broadcast_enabled():
            return {"sent": 0, "reason": "disabled"}
        if not order_id:
            return {"sent": 0, "reason": "no_order_id"}
        if _text(order.get("status")).upper() != "OPEN":
            return {"sent": 0, "reason": "not_open"}

        if database is None:
            from app.db.mongo import get_db
            database = get_db()

        if not _claim(database, order_id):
            return {"sent": 0, "reason": "already_sent"}

        recipients = agent_recipients(database)
        facts = {
            "job_order_id": order_id,
            "title": _text(order.get("title") or order.get("designation")),
            "destination_country": _text(order.get("destinationCountry")),
            "headcount": order.get("headcount"),
            "salary": _text(order.get("salary")),
            "min_experience": _text(order.get("minExperience")),
            # The message's picture. The bot uses it if it is a JPG or PNG and
            # falls back to a default image otherwise.
            "poster_url": poster_url(order),
            **terms_facts(order),
            **staff_facts(order),
            # The Remarks box on the job order dialogs, written for agents.
            "remarks": _text(order.get("description")),
        }
        sent = 0
        failed: List[str] = []
        for recipient in recipients:
            if _post(
                AGENT_JOB_ORDER_PATH,
                {**facts, **recipient},
                f"job order {order_id} to agent {recipient['agent_id']}",
                require_sent=True,
            ):
                sent += 1
            else:
                failed.append(recipient["agent_id"])

        database[BROADCAST_COLLECTION].update_one(
            {"_id": order_id},
            {"$set": {
                "finished_at": datetime.now(timezone.utc),
                "recipients": len(recipients),
                "sent": sent,
                "failed_agent_ids": failed,
            }},
        )
        log.info("Job order %s sent to %d of %d agents on WhatsApp",
                 order_id, sent, len(recipients))
        return {"sent": sent, "recipients": len(recipients), "failed_agent_ids": failed}
    except Exception as exc:  # noqa: BLE001 - the order is saved; this is a side effect
        log.warning("Could not send job order %s to agents: %s", order_id, exc)
        return {"sent": 0, "reason": "error"}


def start_job_order_broadcast(order: Dict[str, Any]) -> None:
    """Run `broadcast_job_order` in the background so the save returns at once."""
    if not broadcast_enabled():
        return
    snapshot = {key: value for key, value in order.items() if key != "_id"}
    threading.Thread(
        target=broadcast_job_order,
        args=(snapshot,),
        name=f"agent-job-order-{_text(order.get('id'))[-12:]}",
        daemon=True,
    ).start()
