"""Mobile numbers the recruitment bot must never answer."""
from __future__ import annotations

import uuid

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.core.models import utcnow
from app.db.dedup import normalize_phone
from app.db.mongo import ensure_index, get_db

COLLECTION = "bot_suppression_numbers"


def collection():
    return get_db()[COLLECTION]


def ensure_bot_suppression_indexes() -> None:
    coll = collection()
    ensure_index(coll, [("phone_key", ASCENDING)], "bot_suppression_phone_unique", unique=True)
    ensure_index(coll, [("created_at", DESCENDING)], "bot_suppression_created_idx")


def list_numbers() -> list[dict]:
    rows = []
    for doc in collection().find({}, {"phone_key": 0}).sort("created_at", DESCENDING):
        rows.append({"id": str(doc["_id"]), **{k: v for k, v in doc.items() if k != "_id"}})
    return rows


def directory_numbers() -> list[str]:
    """Normalized deny-list consumed by the WhatsApp bot."""
    return sorted(
        str(doc["phone_key"])
        for doc in collection().find({}, {"_id": 0, "phone_key": 1})
        if doc.get("phone_key")
    )


def add_number(phone: str, label: str = "", created_by: str = "") -> dict:
    phone = (phone or "").strip()
    phone_key = normalize_phone(phone)
    if not phone_key:
        raise ValueError("Enter a valid mobile number with at least 7 digits.")

    now = utcnow()
    existing = collection().find_one({"phone_key": phone_key})
    if existing:
        raise ValueError("This mobile number is already suppressed.")

    doc = {
        "_id": uuid.uuid4().hex,
        "phone": phone,
        "phone_key": phone_key,
        "label": (label or "").strip(),
        "created_by": (created_by or "").strip() or None,
        "created_at": now,
    }
    try:
        collection().insert_one(doc)
    except DuplicateKeyError as exc:
        # The unique index is the final guard when two admins add the same
        # formatted number at once after both passed the friendly lookup.
        raise ValueError("This mobile number is already suppressed.") from exc
    return {"id": doc["_id"], **{k: v for k, v in doc.items() if k not in {"_id", "phone_key"}}}


def delete_number(number_id: str) -> bool:
    return collection().delete_one({"_id": number_id}).deleted_count > 0
