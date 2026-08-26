"""B2B enquiries — a manpower requirement raised by an agent, not a candidate.

Why this is its own collection
------------------------------
The WhatsApp bot already talks to two kinds of people, and only one of them was
ever written down here. A *candidate* answers questions about themselves and
becomes a row in `candidates`. An *agent* — or an association, or a company
hiring under contract — does the opposite: they describe a vacancy and ask the
agency to fill it. Filing that as a candidate would put a company in a
recruiter's review queue and allocate a job order to a staff member as if it
were a person, so it gets a collection of its own.

What an enquiry is, and what it is not
--------------------------------------
An enquiry is *what was said*, captured verbatim from a conversation. It is not
a job order. A job order is the agency's own commitment to fill a role — it has
a due date, a shortlist and an owner — and the step between the two is a human
reading the enquiry and deciding it is real. `convert` records that decision by
stamping the enquiry with the order it produced; it never writes the order
itself, because the order belongs to the Job Orders screen and its shape lives
there.

Idempotency is per *enquiry*, not per sender
--------------------------------------------
The candidate intake keys on `whatsapp/{phone_number_id}/{wa_user_id}` because a
person registers once. An agent raises an enquiry on Monday and another on
Friday, and both are real. So the key the bot sends must be unique per enquiry —
a submission id, not a user id — and this module resolves a replay of the same
key to the enquiry it already created rather than to the sender's first one. The
unique index is what makes that true under concurrent retries; the lookup ahead
of it only makes the common case cheap.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.db.dedup import normalize_phone
from app.db.mongo import ensure_index, get_db
from app.logging_config import get_logger

log = get_logger(__name__)

COLLECTION = "b2b_enquiries"

#: Where an enquiry can be in its life, in the order it moves through them.
#:
#: Four, and deliberately not more. `new` is "nobody has looked at this",
#: `reviewing` is "someone is on it", and the last two are the only two ways it
#: ends: it became a job order, or it did not. A fifth state would have to
#: describe something a recruiter does differently, and there is nothing
#: between "I am working on it" and "it is finished" that they do.
STATUSES = ("new", "reviewing", "converted", "closed")

#: The states a recruiter may set by hand. `converted` is not one of them: it
#: means an order exists, and it is stamped by `convert` alongside the order's
#: id so the two can never disagree.
ASSIGNABLE_STATUSES = ("new", "reviewing", "closed")

#: What kind of party raised it. The same vocabulary the Sourcing Hub uses, so
#: an enquiry and the record of the person who sent it agree on the word.
PARTY_TYPES = ("agent", "association", "client")

#: Where the enquiry came in from.
SOURCES = ("whatsapp", "manual")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def collection():
    return get_db()[COLLECTION]


def ensure_b2b_indexes() -> None:
    """Called from `ensure_indexes`. Safe to run repeatedly."""
    coll = collection()

    # The one index that is load-bearing rather than merely fast. Without it,
    # two retries of the same submission both read an empty collection and both
    # insert, and the agency chases one vacancy twice. Sparse because an
    # enquiry an admin typed in has no key and every one of them would collide
    # on a missing field.
    ensure_index(
        coll,
        [("idempotency_key", ASCENDING)],
        "b2b_idempotency_key_unique",
        unique=True,
        sparse=True,
    )
    ensure_index(coll, [("id", ASCENDING)], "b2b_id_unique", unique=True)
    # The screen's default read: newest first, optionally narrowed to one state.
    ensure_index(
        coll,
        [("status", ASCENDING), ("received_at", DESCENDING)],
        "b2b_status_received_idx",
    )
    # "Has this agent been in touch before?" — asked when an enquiry is opened.
    ensure_index(coll, [("phone_key", ASCENDING)], "b2b_phone_key_idx", sparse=True)


def new_enquiry_id() -> str:
    """`ENQ-3F9A21C4` — short enough to read down a phone line, unique enough
    that two enquiries raised in the same second do not collide."""
    return f"ENQ-{uuid.uuid4().hex[:8].upper()}"


def normalise_status(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    return v if v in STATUSES else "new"


def normalise_party_type(value: Optional[str]) -> str:
    """An unrecognised type reads as `client`.

    The same widening the Sourcing Hub does, and for the same reason: a party
    whose type this build does not know about is more usefully shown as a
    company than dropped out of every tab.
    """
    v = (value or "").strip().lower()
    return v if v in PARTY_TYPES else "client"


def _clean(value: Any) -> Any:
    """Trim strings, drop nothing else.

    An empty string is preserved as an empty string rather than becoming None:
    the screen renders "Not provided" for both, and a document whose fields
    change type depending on what a candidate typed is harder to query than one
    that does not.
    """
    return value.strip() if isinstance(value, str) else value


def build_document(payload: Dict[str, Any], *, source: str = "whatsapp") -> Dict[str, Any]:
    """One enquiry, as it is stored.

    Every field the collection has is written on every insert, present in the
    payload or not. A document whose shape depends on what the sender happened
    to answer is a document every reader has to defend against, and the screen
    that renders it would need a fallback per field rather than one per record.
    """
    now = utcnow()
    phone = _clean(payload.get("phone")) or ""

    skills = payload.get("skills") or []
    if isinstance(skills, str):
        # The bot asks for skills as one line of free text; an admin's form
        # sends a list. Both arrive here and both are stored as a list.
        skills = [s.strip() for s in skills.split(",") if s.strip()]

    return {
        "id": _clean(payload.get("id")) or new_enquiry_id(),
        "source": source if source in SOURCES else "whatsapp",
        "status": "new",
        # ---- who raised it ---- #
        "party_type": normalise_party_type(payload.get("party_type")),
        "company_name": _clean(payload.get("company_name")) or "",
        "contact_name": _clean(payload.get("contact_name")) or "",
        "phone": phone,
        "phone_e164": _clean(payload.get("phone_e164")) or "",
        "email": _clean(payload.get("email")) or "",
        "country": _clean(payload.get("country")) or "",
        "city": _clean(payload.get("city")) or "",
        # ---- what they want ---- #
        "requirement": _clean(payload.get("requirement")) or "",
        "job_title": _clean(payload.get("job_title")) or "",
        # The taxonomy id when the bot offered a list and the sender picked from
        # it. Blank when they typed something of their own, which is why
        # `job_title` is stored beside it rather than resolved from it.
        "job_id": _clean(payload.get("job_id")) or "",
        "headcount": _coerce_headcount(payload.get("headcount")),
        "destination_country": _clean(payload.get("destination_country")) or "",
        "salary_budget": _clean(payload.get("salary_budget")) or "",
        "experience_required": _clean(payload.get("experience_required")) or "",
        "skills": [str(s).strip() for s in skills if str(s).strip()],
        "needed_by": _clean(payload.get("needed_by")) or "",
        "notes": _clean(payload.get("notes")) or "",
        # ---- provenance ---- #
        "idempotency_key": _clean(payload.get("idempotency_key")) or None,
        "wa_user_id": _clean(payload.get("wa_user_id")) or "",
        # Last ten digits, the same normalisation candidate dedup uses. Stored
        # rather than derived at read time so "has this number been in touch
        # before?" is an index lookup and not a scan.
        "phone_key": normalize_phone(phone),
        # Filled in by `attach_sourcing_match` when the sender is already on
        # file, so the screen can say "known agent" instead of showing a number.
        "sourcing_client_id": None,
        "sourcing_client_name": "",
        # ---- what the agency did about it ---- #
        "converted_job_order_id": None,
        "handled_by": "",
        "handled_at": None,
        "received_at": now,
        "updated_at": now,
    }


def _coerce_headcount(value: Any) -> Optional[int]:
    """A headcount, or nothing at all — never a zero standing in for "unknown".

    The bot collects this by asking, and "a few" is an answer people give. A
    number that cannot be read is dropped rather than guessed, because a job
    order raised for 0 seats is immediately FILLED by `deriveStatus` and
    disappears from the very list it was raised to appear on.
    """
    if value in (None, "", []):
        return None
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def attach_sourcing_match(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Name the sender, if the agency already knows them.

    Matched on the phone number first — it is the one thing a WhatsApp sender
    cannot mistype, because they did not type it — and on the company name only
    as a fallback, folded the same way the Sourcing Hub folds it when it rolls
    job orders up per client.

    Advisory, and only ever additive: a match writes two display fields and
    nothing else. A wrong match therefore costs a misleading label on one card,
    which a recruiter can see and correct, rather than an enquiry filed against
    the wrong company's account.
    """
    phone_key = doc.get("phone_key")
    name_key = _fold(doc.get("company_name"))
    if not phone_key and not name_key:
        return doc

    try:
        clients = get_db()["sourcing_clients"]
        match = None
        if phone_key:
            # `sourcing_clients` stores phones as typed, so the comparison has
            # to happen here rather than in the query.
            for client in clients.find({}, {"_id": 0, "id": 1, "name": 1, "phone": 1}):
                if normalize_phone(client.get("phone")) == phone_key:
                    match = client
                    break
        if match is None and name_key:
            for client in clients.find({}, {"_id": 0, "id": 1, "name": 1}):
                if _fold(client.get("name")) == name_key:
                    match = client
                    break
    except Exception as exc:  # noqa: BLE001 — a label must not fail an intake
        log.warning("Could not match enquiry %s to a sourcing client: %s", doc.get("id"), exc)
        return doc

    if match:
        doc["sourcing_client_id"] = match.get("id")
        doc["sourcing_client_name"] = match.get("name") or ""
    return doc


def _fold(value: Optional[str]) -> str:
    """Case- and space-insensitive form of a company name, for comparison only."""
    return " ".join((value or "").split()).casefold()


def record_enquiry(payload: Dict[str, Any], *, source: str = "whatsapp") -> tuple[Dict[str, Any], bool]:
    """Store one enquiry. Returns `(document, created)`.

    `created` is False when this exact submission has already been accepted, in
    which case the document returned is the one that was stored the first time.
    A caller that retries after a timeout gets the same enquiry id back and the
    recruiter sees one enquiry, which is the whole point of the key.
    """
    doc = attach_sourcing_match(build_document(payload, source=source))
    key = doc.get("idempotency_key")

    if key:
        existing = collection().find_one({"idempotency_key": key}, {"_id": 0})
        if existing:
            log.info("Idempotent replay of B2B enquiry %s -> %s", key, existing.get("id"))
            return existing, False

    try:
        collection().insert_one(dict(doc))
    except DuplicateKeyError:
        # The lookup above lost the race it cannot win: another writer inserted
        # the same key between the read and this write. Theirs is the enquiry.
        existing = collection().find_one({"idempotency_key": key}, {"_id": 0}) if key else None
        if existing:
            return existing, False
        raise

    doc.pop("_id", None)
    return doc, True


def list_enquiries(
    *,
    status: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Newest first, optionally narrowed to one state."""
    query: Dict[str, Any] = {}
    if status and status in STATUSES:
        query["status"] = status
    cursor = collection().find(query, {"_id": 0}).sort("received_at", DESCENDING).limit(limit)
    return list(cursor)


def get_enquiry(enquiry_id: str) -> Optional[Dict[str, Any]]:
    return collection().find_one({"id": enquiry_id}, {"_id": 0})


def update_enquiry(enquiry_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply an allow-listed set of edits and return the enquiry as it now is.

    An allow-list rather than a `$set` of whatever arrived: `received_at`,
    `idempotency_key` and `source` are the record of what happened, and an edit
    screen that can rewrite them can rewrite the evidence that the enquiry came
    from where it says it did.
    """
    editable = {
        "status",
        "party_type",
        "company_name",
        "contact_name",
        "phone",
        "email",
        "country",
        "city",
        "requirement",
        "job_title",
        "job_id",
        "headcount",
        "destination_country",
        "salary_budget",
        "experience_required",
        "skills",
        "needed_by",
        "notes",
        "handled_by",
    }

    updates: Dict[str, Any] = {}
    for field, value in changes.items():
        if field not in editable or value is None:
            continue
        if field == "status":
            # `converted` is `convert`'s to write — see the note on
            # ASSIGNABLE_STATUSES. A caller asking for it here is refused
            # rather than silently downgraded, because "I marked it converted"
            # and "it quietly stayed new" look identical from the screen.
            status = (value or "").strip().lower()
            if status not in ASSIGNABLE_STATUSES:
                raise ValueError(
                    f"status must be one of {', '.join(ASSIGNABLE_STATUSES)}; "
                    "'converted' is set by converting the enquiry into a job order"
                )
            updates["status"] = status
        elif field == "headcount":
            updates["headcount"] = _coerce_headcount(value)
        elif field == "skills":
            items = value if isinstance(value, list) else str(value).split(",")
            updates["skills"] = [str(s).strip() for s in items if str(s).strip()]
        elif field == "party_type":
            updates["party_type"] = normalise_party_type(value)
        else:
            updates[field] = _clean(value)

    if not updates:
        return get_enquiry(enquiry_id)

    updates["updated_at"] = utcnow()
    # A recruiter touching an enquiry for the first time is what "someone is on
    # it" means, so the timestamp is stamped by the act rather than asked for.
    if updates.get("status") in ("reviewing", "closed"):
        updates["handled_at"] = utcnow()

    result = collection().update_one({"id": enquiry_id}, {"$set": updates})
    if result.matched_count == 0:
        return None
    return get_enquiry(enquiry_id)


def mark_converted(
    enquiry_id: str, job_order_id: str, *, handled_by: str = ""
) -> Optional[Dict[str, Any]]:
    """Record that this enquiry became a job order.

    The two writes are one act and belong together: an enquiry marked
    `converted` with no order id is a dead end on the screen, and an order id on
    an enquiry still reading `new` is a job somebody will raise twice.
    """
    now = utcnow()
    result = collection().update_one(
        {"id": enquiry_id},
        {
            "$set": {
                "status": "converted",
                "converted_job_order_id": job_order_id,
                "handled_by": handled_by,
                "handled_at": now,
                "updated_at": now,
            }
        },
    )
    if result.matched_count == 0:
        return None
    return get_enquiry(enquiry_id)


def delete_enquiry(enquiry_id: str) -> bool:
    return collection().delete_one({"id": enquiry_id}).deleted_count > 0


def status_counts() -> Dict[str, int]:
    """How many enquiries sit in each state, including the states with none.

    Every key is present whether or not the database has a row for it, so the
    screen's tabs render at a stable width instead of appearing as the first
    enquiry of each kind arrives.
    """
    counts = {status: 0 for status in STATUSES}
    try:
        for row in collection().aggregate(
            [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
        ):
            status = normalise_status(row.get("_id"))
            counts[status] = counts.get(status, 0) + int(row.get("n") or 0)
    except Exception as exc:  # noqa: BLE001 — a count is not worth a 500
        log.warning("Could not count B2B enquiries: %s", exc)
    return counts
