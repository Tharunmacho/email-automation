"""Résumés that Sourcing Hub agents send on WhatsApp become CRM candidates.

An agent sends a candidate's CV to Rafi's number. The bot downloads the file
and posts it to `POST /agent-resumes`. This module:

1. **Checks the sender is an agent.** Their number has to belong to a Sourcing
   Hub row with `type == "agent"` (the row's phone or any of its contacts).
   Clients, associations, inactive agents and unknown numbers are ignored, and
   the bot stays silent for them, as it always has.
2. **Reads and checks the résumé with VeriIS.** It uses the same intake as an
   admin upload (`intake_uploaded_candidate`). A file that is not a résumé, is
   too unclear, has no name or contact, or duplicates an existing candidate is
   refused with a code the bot turns into a reply.
3. **Labels the candidate.** `source="agent"` and `submitted_by_agent` record
   the agency, the contact person and their number.
4. **Assigns it** through the normal least-loaded allocation, like an admin
   upload.

Each WhatsApp message is processed once. `agent_resume_submissions` is keyed on
the message id (`wamid`) and stores the outcome, so a retried job gets the same
answer instead of a second candidate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.core.crm_ids import candidate_code
from app.core.models import AgentSubmission
from app.db.dedup import normalize_phone
from app.logging_config import get_logger

log = get_logger(__name__)

SUBMISSIONS = "agent_resume_submissions"

#: A claim older than this is from a request that died; the next try takes over.
STALE_PROCESSING = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def find_agent(database, phone: str) -> Optional[Dict[str, str]]:
    """The Sourcing Hub agent whose number this is, or None.

    Matches the row's own phone and every contact's phone, compared on their
    last ten digits (`normalize_phone`), since the Sourcing Hub stores numbers
    as typed.
    """
    key = normalize_phone(phone)
    if not key:
        return None
    projection = {"_id": 0, "id": 1, "name": 1, "type": 1, "status": 1,
                  "contact": 1, "phone": 1, "contacts": 1}
    for record in database["sourcing_clients"].find({}, projection):
        if str(record.get("type") or "").strip().lower() != "agent":
            continue
        people = [
            (str(c.get("name") or ""), str(c.get("phone") or ""))
            for c in record.get("contacts") or []
            if isinstance(c, dict)
        ]
        people.append((str(record.get("contact") or ""), str(record.get("phone") or "")))
        for name, number in people:
            if normalize_phone(number) == key:
                return {
                    "agent_id": str(record.get("id") or ""),
                    "agent_name": str(record.get("name") or "").strip(),
                    "contact_name": name.strip(),
                    "phone": number.strip(),
                    "status": str(record.get("status") or "").strip().upper(),
                }
    return None


def _claim(database, wamid: str) -> Optional[Dict[str, Any]]:
    """Take this message, or return what is already known about it.

    None means "yours, go ahead". Otherwise it is the stored outcome, or
    `{"status": "processing"}` while another request is still working on it.
    """
    from pymongo.errors import DuplicateKeyError

    collection = database[SUBMISSIONS]
    try:
        collection.insert_one({"_id": wamid, "started_at": _now()})
        return None
    except DuplicateKeyError:
        pass

    existing = collection.find_one({"_id": wamid}) or {}
    if existing.get("result"):
        return existing["result"]
    started = _aware(existing.get("started_at"))
    if started and _now() - started < STALE_PROCESSING:
        return {"status": "processing"}
    # The earlier attempt died. Take it over.
    collection.update_one({"_id": wamid}, {"$set": {"started_at": _now()}})
    return None


def _allocate(repository, candidate) -> None:
    """Normal least-loaded allocation, as for an admin upload. Never raises."""
    from app.assignment.balancer import assign_candidate
    from app.notifications import notify_candidate_assigned

    try:
        assignment = assign_candidate(
            candidate.id, candidate.profile, repo=repository, only_if_unassigned=True,
        )
        if assignment.assigned and assignment.staff_id:
            notify_candidate_assigned(
                assignment.staff_id,
                {
                    "id": candidate.id,
                    "full_name": candidate.profile.full_name,
                    "email": candidate.profile.email,
                },
                staff_name=assignment.staff_name or "",
            )
    except Exception as exc:  # noqa: BLE001 - the candidate exists either way
        log.warning("Could not allocate agent candidate %s: %s", candidate.id, exc)


def submit_agent_resume(
    *,
    data: bytes,
    filename: str,
    mime_type: str,
    phone: str,
    wamid: str,
    repository,
    line_id: str = "",
    database=None,
    parser=None,
) -> Dict[str, Any]:
    """Turn one agent's WhatsApp file into a candidate. Returns the outcome.

    `status` is one of:

    * `created`    - `candidate_id`, `candidate_code`, `full_name`, `agent_name`
    * `rejected`   - `code` and `message` from the intake (not a résumé,
                     duplicate, unreadable, ...)
    * `error`      - VeriIS or storage failed (`code`, `message`)
    * `ignored`    - `reason`: the sender is not an active agent
    * `processing` - another attempt at this message is still running
    """
    from app.services.candidate_upload_intake import (
        CandidateUploadError,
        UploadedDocument,
        intake_uploaded_candidate,
    )

    if database is None:
        from app.db.mongo import get_db
        database = get_db()

    agent = find_agent(database, phone)
    if not agent:
        return {"status": "ignored", "reason": "not_an_agent"}
    if agent["status"] == "INACTIVE":
        return {"status": "ignored", "reason": "inactive_agent"}

    if wamid:
        known = _claim(database, wamid)
        if known is not None:
            return known

    submission = AgentSubmission(
        agent_id=agent["agent_id"],
        agent_name=agent["agent_name"] or None,
        contact_name=agent["contact_name"] or None,
        phone=agent["phone"] or None,
        line_id=line_id or None,
        wamid=wamid or None,
        received_at=_now(),
    )
    try:
        result = intake_uploaded_candidate(
            resume=UploadedDocument(data=data, filename=filename, mime_type=mime_type),
            repository=repository,
            uploader_id=f"agent:{agent['agent_id']}",
            parser=parser,
            submitted_by_agent=submission,
        )
    except CandidateUploadError as exc:
        outcome: Dict[str, Any] = {
            "status": "error" if exc.status_code >= 500 else "rejected",
            "code": exc.code,
            "message": exc.message,
            "agent_name": agent["agent_name"],
        }
    except Exception:
        # Unexpected: let the bot retry the whole message.
        if wamid:
            database[SUBMISSIONS].delete_one({"_id": wamid})
        raise
    else:
        candidate = result.candidate
        _allocate(repository, candidate)
        stored = repository.get(candidate.id) or candidate
        outcome = {
            "status": "created",
            "candidate_id": candidate.id,
            "candidate_code": stored.candidate_code or candidate_code(candidate.id),
            "full_name": stored.profile.full_name,
            "agent_name": agent["agent_name"],
        }

    if wamid:
        database[SUBMISSIONS].update_one(
            {"_id": wamid},
            {"$set": {
                "result": outcome,
                "finished_at": _now(),
                "agent_id": agent["agent_id"],
                "filename": filename,
            }},
        )
    log.info(
        "Agent résumé %s from %s (%s): %s %s",
        wamid, agent["agent_name"], agent["agent_id"],
        outcome["status"], outcome.get("code") or outcome.get("candidate_code") or "",
    )
    return outcome
