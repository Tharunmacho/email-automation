"""Where an Aadhaar card and a passport go once they have been read.

They do not go on the candidate record. Two reasons, and the second is the one
that matters:

* A candidate is created from a résumé, and dedup merges two résumés of the same
  person into one record. An identity document is evidence about a *file* — this
  scan, from this email — and merging it away loses the provenance an audit
  needs.
* These are government identity numbers. Keeping them in their own collections
  means the reads that populate the recruiter's list, which project the
  candidate document wholesale, cannot accidentally serve an Aadhaar number to
  a browser. Nothing in the candidate pipeline touches these collections.

One record per ``(provider, account, message, attachment, mode)`` — the same
natural key the ingestion state machine uses, and literally the same ``_id``, so
a redelivered email overwrites its own record instead of accumulating copies.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING

from app.config import settings
from app.core.models import utcnow
from app.db.mongo import get_db
from app.logging_config import get_logger

log = get_logger(__name__)


def get_aadhaar_collection():
    return get_db()[settings.mongo_aadhaar_collection]


def get_passport_collection():
    return get_db()[settings.mongo_passport_collection]


#: The two document types this module files, and where each one goes. Anything
#: not named here is not an identity document as far as this system is
#: concerned, which is what makes it safe to take the type from a URL.
DOCUMENT_TYPES = ("aadhaar", "passport", "document")

def get_document_collection():
    return get_db()[settings.mongo_document_collection]

def collection_for(document_type: str):
    """The collection holding ``document_type``, or ``None`` if there isn't one."""
    if document_type == "aadhaar":
        return get_aadhaar_collection()
    if document_type == "passport":
        return get_passport_collection()
    if document_type == "document":
        return get_document_collection()
    return None


def _mask_aadhaar(number: Optional[str]) -> str:
    """``XXXXXXXX9017`` — enough to recognise the card, not enough to use it.

    Stored alongside the full number so every screen that only needs to show
    *which* card this is has something safe to show, and nobody has to remember
    to mask it at the point of display.
    """
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if len(digits) < 4:
        return ""
    return "X" * (len(digits) - 4) + digits[-4:]


def printed_fields_from(fields: Any) -> Optional[Dict[str, str]]:
    """``{label: value}`` for what is printed on the data page.

    The service returns these as a *list* of records — each carrying the label,
    the value, and how it was read: ``{"label": "Place of Issue", "value":
    "MADURAI", "category": "Document", "page": 1, "source": "vision-llm",
    "confidence": 0.95}``. Stored in that shape, a screen showing "the printed
    fields" has a list of objects where it expects named values, and renders

        0   Label: Surname Value: ANGURAJ Category: Personal Page: 1 …

    which is the extraction's working notes, not the passport. A recruiter needs
    the place of issue; how confident the reader was, and which pass produced
    it, are answers to a question nobody asked — and `raw` keeps every one of
    them for the case where somebody does.

    Later entries win, so a page listing a label twice keeps the last reading —
    which on a passport data page is the printed value rather than a header.
    """
    if isinstance(fields, dict):
        # Already a mapping (an older record, or a service that sends one).
        return {str(k): str(v) for k, v in fields.items() if v not in (None, "")} or None
    if not isinstance(fields, list):
        return None

    found: Dict[str, str] = {}
    for entry in fields:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        value = entry.get("value")
        if not label or value in (None, ""):
            continue
        found[label] = str(value).strip()
    return found or None


def _base_document(
    record_id: str,
    *,
    document_type: str,
    candidate_id: Optional[str],
    provider: str,
    account_id: str,
    message_id: str,
    attachment_id: str,
    filename: str,
    sha256: str,
    pages: List[int],
    ocr_job_id: Optional[str],
    result: Dict[str, Any],
    file: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    doc = {
        "_id": record_id,
        "document_type": document_type,
        "candidate_id": candidate_id,
        "source": {
            "provider": provider,
            "account_id": account_id,
            "message_id": message_id,
            "attachment_id": attachment_id,
            "filename": filename,
            "sha256": sha256,
            # Which pages of the bundle this came off — the difference between
            # "page 54 of the application PDF" and an unattributable extract.
            "pages": list(pages or []),
        },
        "ocr_job_id": ocr_job_id,
        # The service's answer, untouched. Every projected field below is
        # derived from it, so a mapping bug is always recoverable.
        "raw": result,
        "warnings": list(result.get("warnings") or []),
        "updated_at": utcnow(),
    }
    # Only when there is one. Every write here is a `$set` upsert, and a
    # redelivered email re-runs the extraction without the bytes to hand — a
    # `"file": None` in that payload would delete the scan a recruiter can
    # currently download, on a pass that changed nothing else.
    if file:
        doc["file"] = dict(file)
    return doc


def store_aadhaar_record(
    record_id: str,
    result: Dict[str, Any],
    *,
    candidate_id: Optional[str] = None,
    provider: str = "email",
    account_id: str = "",
    message_id: str = "",
    attachment_id: str = "",
    filename: str = "",
    sha256: str = "",
    pages: Optional[List[int]] = None,
    ocr_job_id: Optional[str] = None,
    file: Optional[Dict[str, Any]] = None,
    collection=None,
) -> str:
    """Upsert one Aadhaar extraction. Returns the record id."""
    coll = collection if collection is not None else get_aadhaar_collection()
    data = dict(result.get("aadhaar") or {})

    doc = _base_document(
        record_id,
        document_type="aadhaar",
        candidate_id=candidate_id,
        provider=provider,
        account_id=account_id,
        message_id=message_id,
        attachment_id=attachment_id,
        filename=filename,
        sha256=sha256,
        pages=pages or [],
        ocr_job_id=ocr_job_id,
        result=result,
        file=file,
    )
    number = data.get("aadhaar_number")
    doc.update(
        {
            "name": data.get("name"),
            "aadhaar_number": number,
            # The service masks it too, but only when it saw a masked card. This
            # one is always present.
            "masked_aadhaar_number": data.get("masked_aadhaar_number") or _mask_aadhaar(number),
            "aadhaar_number_valid": data.get("aadhaar_number_valid"),
            "date_of_birth": data.get("date_of_birth"),
            "year_of_birth": data.get("year_of_birth"),
            "gender": data.get("gender"),
            "mobile_number": data.get("mobile_number"),
            "address": data.get("address"),
            "care_of": data.get("care_of"),
            "pincode": data.get("pincode"),
            "vid": data.get("vid"),
            "enrollment_id": data.get("enrollment_id"),
            "document_side": data.get("document_side"),
        }
    )

    coll.update_one(
        {"_id": record_id},
        {"$set": doc, "$setOnInsert": {"created_at": utcnow()}},
        upsert=True,
    )
    log.info(
        "Stored aadhaar record %s for candidate %s (number valid=%s)",
        record_id, candidate_id, doc.get("aadhaar_number_valid"),
    )
    return record_id


def store_passport_record(
    record_id: str,
    result: Dict[str, Any],
    *,
    candidate_id: Optional[str] = None,
    provider: str = "email",
    account_id: str = "",
    message_id: str = "",
    attachment_id: str = "",
    filename: str = "",
    sha256: str = "",
    pages: Optional[List[int]] = None,
    ocr_job_id: Optional[str] = None,
    file: Optional[Dict[str, Any]] = None,
    collection=None,
) -> str:
    """Upsert one passport extraction. Returns the record id."""
    coll = collection if collection is not None else get_passport_collection()
    mrz = result.get("mrz") or {}
    fields = printed_fields_from(result.get("fields"))

    doc = _base_document(
        record_id,
        document_type="passport",
        candidate_id=candidate_id,
        provider=provider,
        account_id=account_id,
        message_id=message_id,
        attachment_id=attachment_id,
        filename=filename,
        sha256=sha256,
        pages=pages or [],
        ocr_job_id=ocr_job_id,
        result=result,
        file=file,
    )
    doc.update(
        {
            "passport_number": mrz.get("passport_number"),
            "surname": mrz.get("surname"),
            "given_names": mrz.get("given_names"),
            "nationality": mrz.get("nationality"),
            "issuing_country": mrz.get("issuing_country"),
            "date_of_birth": mrz.get("date_of_birth"),
            "sex": mrz.get("sex"),
            "expiry_date": mrz.get("expiry_date"),
            "date_of_issue": mrz.get("date_of_issue"),
            "personal_number": mrz.get("personal_number"),
            # The MRZ check digits are the passport's own integrity test. A
            # false here means the OCR misread a character — worth surfacing,
            # never worth silently trusting.
            "check_digits_valid": mrz.get("all_check_digits_valid"),
            "mrz_source": result.get("mrz_source"),
            "raw_mrz": result.get("raw_mrz"),
            "confidence": result.get("confidence"),
            # Fields read off the printed data page rather than the MRZ; they
            # carry the place of issue, which the MRZ does not encode.
            "printed_fields": fields or None,
        }
    )

    coll.update_one(
        {"_id": record_id},
        {"$set": doc, "$setOnInsert": {"created_at": utcnow()}},
        upsert=True,
    )
    log.info(
        "Stored passport record %s for candidate %s (check digits valid=%s)",
        record_id, candidate_id, doc.get("check_digits_valid"),
    )
    return record_id


def store_document_record(
    record_id: str,
    result: Dict[str, Any],
    *,
    candidate_id: Optional[str] = None,
    provider: str = "email",
    account_id: str = "",
    message_id: str = "",
    attachment_id: str = "",
    filename: str = "",
    sha256: str = "",
    pages: Optional[List[int]] = None,
    ocr_job_id: Optional[str] = None,
    collection=None,
) -> str:
    """Upsert one raw generic document extraction. Returns the record id."""
    coll = collection if collection is not None else get_document_collection()

    doc = _base_document(
        record_id,
        document_type="document",
        candidate_id=candidate_id,
        provider=provider,
        account_id=account_id,
        message_id=message_id,
        attachment_id=attachment_id,
        filename=filename,
        sha256=sha256,
        pages=pages or [],
        ocr_job_id=ocr_job_id,
        result=result,
    )

    coll.update_one(
        {"_id": record_id},
        {"$set": doc, "$setOnInsert": {"created_at": utcnow()}},
        upsert=True,
    )
    log.info(
        "Stored document record %s for candidate %s",
        record_id, candidate_id,
    )
    return record_id


def find_for_candidate(candidate_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Every identity document read out of this candidate's application."""
    return {
        "aadhaar": list(get_aadhaar_collection().find({"candidate_id": candidate_id})),
        "passport": list(get_passport_collection().find({"candidate_id": candidate_id})),
        "document": list(get_document_collection().find({"candidate_id": candidate_id})),
    }


def find_one(
    candidate_id: str, document_type: str, record_id: str
) -> Optional[Dict[str, Any]]:
    """One identity document, or ``None``.

    Matched on the candidate id as well as the record id, so a record id
    belonging to somebody else does not resolve just because the caller can
    read *a* candidate. The pair is the authorisation, not the id alone.
    """
    coll = collection_for(document_type)
    if coll is None:
        return None
    return coll.find_one({"_id": record_id, "candidate_id": candidate_id})


def find_by_record_id(document_type: str, record_id: str) -> Optional[Dict[str, Any]]:
    """One identity document by its id alone, whoever it belongs to.

    The only caller is the check that refuses to re-file a document under a
    different candidate. Everything a *reader* reaches for goes through
    `find_one`, which takes the candidate id as well — this exists precisely to
    find the record the caller is not entitled to and stop there.
    """
    coll = collection_for(document_type)
    if coll is None:
        return None
    return coll.find_one({"_id": record_id})


def attach_file(
    document_type: str, record_id: str, candidate_id: str, file: Dict[str, Any]
) -> bool:
    """Hang a stored scan on an identity record. False if there is no such record.

    Scoped to the candidate as well as the record, so a file cannot be attached
    to a document belonging to somebody else even by a caller holding the right
    record id. `updated_at` moves because the record did.
    """
    coll = collection_for(document_type)
    if coll is None:
        return False
    result = coll.update_one(
        {"_id": record_id, "candidate_id": candidate_id},
        {"$set": {"file": dict(file), "updated_at": utcnow()}},
    )
    return result.matched_count > 0


def ensure_identity_indexes() -> None:
    from app.db.mongo import ensure_index

    aadhaar = get_aadhaar_collection()
    ensure_index(aadhaar, [("candidate_id", ASCENDING)], "aadhaar_candidate_idx", sparse=True)
    ensure_index(aadhaar, [("aadhaar_number", ASCENDING)], "aadhaar_number_idx", sparse=True)
    ensure_index(aadhaar, [("source.message_id", ASCENDING)], "aadhaar_msg_idx")
    ensure_index(aadhaar, [("created_at", DESCENDING)], "aadhaar_created_idx")

    passport = get_passport_collection()
    ensure_index(passport, [("candidate_id", ASCENDING)], "passport_candidate_idx", sparse=True)
    ensure_index(passport, [("passport_number", ASCENDING)], "passport_number_idx", sparse=True)
    ensure_index(passport, [("source.message_id", ASCENDING)], "passport_msg_idx")
    ensure_index(passport, [("created_at", DESCENDING)], "passport_created_idx")

    document = get_document_collection()
    ensure_index(document, [("candidate_id", ASCENDING)], "document_candidate_idx", sparse=True)
    ensure_index(document, [("source.message_id", ASCENDING)], "document_msg_idx")
    ensure_index(document, [("created_at", DESCENDING)], "document_created_idx")
