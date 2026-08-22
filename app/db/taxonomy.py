"""The jobs the agency recruits for, and the countries it sends people to.

Why these are rows and not constants
------------------------------------
They used to be a tuple in `app/policy/cv_policy.py` and a list in the bot's
`flow.ts`, which meant adding a job was a two-repository code change and a
deploy of each. An agency that opens a Kuwait desk on Monday cannot wait for
that, and the person who knows a new job has opened is an admin, not a
programmer.

So a job is a document, an admin creates it, and both systems read it: the CRM
resolves the CV requirement from it, and the bot offers it in the question it
asks candidates. One place to add it, two places it shows up.

The primary key is worth stating plainly
----------------------------------------
`job_id` — a slug like `general_worker` — is what the CV policy keys on, what
the bot sends back, and what is written on every candidate record. It is
generated from the title once, at creation, and never changes afterwards: a
title is a label a person reads and may want to reword, and an id is an
identifier a database depends on. Renaming "General Worker" to "General
Labourer" must not orphan the policy rules or the thousand candidates already
filed under it, and keeping the two separate is what makes that true.

Free text is never a policy key. Candidates type "General Worker", "general
labour", "GW" and "helper" for one job; a table keyed on what people type
matches almost nothing and silently falls through to the default, which looks
like a working rule and is not one.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING

from app.db.mongo import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

JOBS_COLLECTION = "job_designations"
COUNTRIES_COLLECTION = "countries"
JOB_QUESTIONS_COLLECTION = "job_questions"

#: How many rows the bot may show for a list question.
#:
#: WhatsApp's own ceiling is ten, and it is a hard one — a list with eleven rows
#: is rejected by the API, not truncated by it. Nine jobs plus an "Other" row is
#: therefore the most a candidate can be *offered*; anything past that is
#: reachable by typing, which the interpreter maps back onto a job. Admins order
#: the list, so the nine shown are the nine that matter.
BOT_LIST_LIMIT = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slugify(text: str) -> str:
    """`General Worker / Helper` → `general_worker_helper`.

    Deliberately narrow: lowercase, underscores, nothing else. This value ends
    up in URLs, in JSON keys, in a WhatsApp payload and in a Mongo index, and
    the intersection of what all four handle without escaping is small.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return slug[:60] or "job"


# --------------------------------------------------------------------------- #
#  Jobs
# --------------------------------------------------------------------------- #
def jobs_collection():
    return get_db()[JOBS_COLLECTION]


def countries_collection():
    return get_db()[COUNTRIES_COLLECTION]


def job_questions_collection():
    return get_db()[JOB_QUESTIONS_COLLECTION]


def ensure_taxonomy_indexes() -> None:
    """Called from `ensure_indexes`. Safe to run repeatedly."""
    jobs = jobs_collection()
    jobs.create_index([("active", ASCENDING), ("bot_order", ASCENDING)], name="job_active_order_idx")
    jobs.create_index([("bot_visible", ASCENDING)], name="job_bot_visible_idx")

    countries = countries_collection()
    countries.create_index(
        [("active", ASCENDING), ("bot_order", ASCENDING)], name="country_active_order_idx"
    )

    questions = job_questions_collection()
    questions.create_index(
        [("job_id", ASCENDING), ("order", ASCENDING)], name="job_question_order_idx"
    )


def normalise_country(value: Optional[str]) -> str:
    """Countries arrive cased and spaced differently from every source."""
    return (value or "").strip().casefold()


def job_doc(
    *,
    job_id: str,
    title: str,
    cv_required_default: bool = True,
    cv_overrides: Optional[Dict[str, bool]] = None,
    bot_visible: bool = True,
    bot_order: int = 100,
    active: bool = True,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """One job, in the shape it is stored.

    `cv_overrides` is keyed on the *normalised* country name, because the
    lookup that reads it is given whatever the bot sends — "Malaysia",
    "malaysia", " Malaysia " — and a dictionary that only answers to one of
    those is a rule that silently does not apply.
    """
    now = utcnow()
    return {
        "_id": job_id,
        "id": job_id,
        "title": title.strip(),
        "active": active,
        # Whether candidates are offered it. A job can exist for filing and
        # reporting without being something the bot puts in front of people —
        # and with only nine rows to give away, that distinction earns its keep.
        "bot_visible": bot_visible,
        "bot_order": bot_order,
        # The rule when no country says otherwise.
        "cv_required_default": bool(cv_required_default),
        # `{"malaysia": False}` — the exceptions, per destination.
        "cv_overrides": {
            normalise_country(k): bool(v) for k, v in (cv_overrides or {}).items()
        },
        "created_at": now,
        "updated_at": now,
        "created_by": created_by,
    }


def list_jobs(*, active_only: bool = False, bot_only: bool = False) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if active_only or bot_only:
        query["active"] = True
    if bot_only:
        query["bot_visible"] = True
    rows = list(jobs_collection().find(query).sort([("bot_order", ASCENDING), ("title", ASCENDING)]))
    for row in rows:
        row.pop("_id", None)
    return rows


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    if not job_id:
        return None
    doc = jobs_collection().find_one({"_id": job_id})
    if doc:
        doc.pop("_id", None)
    return doc


def upsert_job(doc: Dict[str, Any]) -> Dict[str, Any]:
    stored = dict(doc)
    job_id = stored.pop("id", None) or stored.get("_id")
    stored["_id"] = job_id
    stored["id"] = job_id
    stored["updated_at"] = utcnow()
    jobs_collection().replace_one({"_id": job_id}, stored, upsert=True)
    log.info("Saved job designation %s (%s)", job_id, stored.get("title"))
    result = dict(stored)
    result.pop("_id", None)
    return result


def delete_job(job_id: str) -> bool:
    """Retire a job rather than erase it.

    Candidates are on file against this id and the CV decision recorded on them
    refers to it. Deleting the row would leave those records pointing at nothing
    and make "why was this candidate not asked for a CV?" unanswerable, so the
    job is deactivated and disappears from every list instead.
    """
    result = jobs_collection().update_one(
        {"_id": job_id},
        {"$set": {"active": False, "bot_visible": False, "updated_at": utcnow()}},
    )
    return bool(result.matched_count)


# --------------------------------------------------------------------------- #
#  Countries
# --------------------------------------------------------------------------- #
def country_doc(
    *,
    name: str,
    bot_visible: bool = True,
    bot_order: int = 100,
    active: bool = True,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    now = utcnow()
    return {
        "_id": slugify(name),
        "id": slugify(name),
        # The name as a person writes it, and as it is stored on candidates and
        # matched by the CV rules.
        "name": name.strip(),
        "active": active,
        "bot_visible": bot_visible,
        "bot_order": bot_order,
        "created_at": now,
        "updated_at": now,
        "created_by": created_by,
    }


def list_countries(*, active_only: bool = False, bot_only: bool = False) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if active_only or bot_only:
        query["active"] = True
    if bot_only:
        query["bot_visible"] = True
    rows = list(
        countries_collection().find(query).sort([("bot_order", ASCENDING), ("name", ASCENDING)])
    )
    for row in rows:
        row.pop("_id", None)
    return rows


def upsert_country(doc: Dict[str, Any]) -> Dict[str, Any]:
    stored = dict(doc)
    country_id = stored.pop("id", None) or stored.get("_id")
    stored["_id"] = country_id
    stored["id"] = country_id
    stored["updated_at"] = utcnow()
    countries_collection().replace_one({"_id": country_id}, stored, upsert=True)
    log.info("Saved country %s (%s)", country_id, stored.get("name"))
    result = dict(stored)
    result.pop("_id", None)
    return result


def delete_country(country_id: str) -> bool:
    result = countries_collection().update_one(
        {"_id": country_id},
        {"$set": {"active": False, "bot_visible": False, "updated_at": utcnow()}},
    )
    return bool(result.matched_count)


# --------------------------------------------------------------------------- #
#  Questions asked about a job
# --------------------------------------------------------------------------- #
def question_doc(
    *,
    job_id: str,
    text: str,
    kind: str = "text",
    choices: Optional[List[str]] = None,
    required: bool = False,
    order: int = 100,
    active: bool = True,
    question_id: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    now = utcnow()
    return {
        "_id": question_id or uuid.uuid4().hex,
        "id": question_id or uuid.uuid4().hex,
        "job_id": job_id,
        "text": text.strip(),
        # `text` for a typed answer, `choice` for a tap. Nothing else, because
        # every extra input type is another thing the bot has to render inside
        # WhatsApp's constraints.
        "kind": kind if kind in ("text", "choice") else "text",
        "choices": [c.strip() for c in (choices or []) if c and c.strip()][:9],
        "required": bool(required),
        "order": order,
        "active": active,
        "created_at": now,
        "updated_at": now,
        "created_by": created_by,
    }


def list_job_questions(job_id: Optional[str] = None, *, active_only: bool = False) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if job_id:
        query["job_id"] = job_id
    if active_only:
        query["active"] = True
    rows = list(
        job_questions_collection().find(query).sort([("order", ASCENDING), ("created_at", ASCENDING)])
    )
    for row in rows:
        row["id"] = row.pop("_id")
    return rows


def upsert_job_question(doc: Dict[str, Any]) -> Dict[str, Any]:
    stored = dict(doc)
    question_id = stored.pop("id", None) or stored.get("_id") or uuid.uuid4().hex
    stored["_id"] = question_id
    stored["updated_at"] = utcnow()
    job_questions_collection().replace_one({"_id": question_id}, stored, upsert=True)
    result = dict(stored)
    result["id"] = result.pop("_id")
    return result


def delete_job_question(question_id: str) -> bool:
    result = job_questions_collection().delete_one({"_id": question_id})
    return bool(result.deleted_count)


# --------------------------------------------------------------------------- #
#  Seeding
# --------------------------------------------------------------------------- #
#: The jobs and rules that were in force before any of this was editable.
#:
#: Seeded once, on first start, and this is not a convenience — it is the
#: promise that turning the policy into data changes nothing about what the
#: policy *says*. Every rule below is a transcription of the table that used to
#: live in `cv_policy.DEFAULT_POLICY`: low-skill roles in the South-East Asian
#: corridor are placed off a profile and an interview, and skilled or
#: certificated roles are placed against a client specification that the CV is
#: what satisfies.
#:
#: A candidate registering the day after this ships is asked for exactly what
#: they would have been asked for the day before.
SEED_JOBS: List[Dict[str, Any]] = [
    {"id": "general_worker", "title": "General Worker", "order": 1, "default": True,
     "overrides": {"Malaysia": False, "Singapore": False}},
    {"id": "factory_warehouse", "title": "Factory / Warehouse", "order": 2, "default": True,
     "overrides": {"Malaysia": False, "Singapore": False}},
    {"id": "packing", "title": "Packing", "order": 3, "default": True,
     "overrides": {"Malaysia": False, "Singapore": False}},
    {"id": "cleaning_housekeeping", "title": "Cleaning / Housekeeping", "order": 4, "default": True,
     "overrides": {"Malaysia": False, "Singapore": False}},
    {"id": "construction", "title": "Construction", "order": 5, "default": True,
     "overrides": {"Malaysia": False, "Singapore": False}},
    {"id": "hospitality", "title": "Hospitality", "order": 6, "default": True, "overrides": {}},
    {"id": "sales_retail", "title": "Sales / Retail", "order": 7, "default": True, "overrides": {}},
    # Skilled and certificated: a CV everywhere, which is what the old table's
    # wildcard rules said.
    {"id": "driver_operator", "title": "Driver / Operator", "order": 8, "default": True, "overrides": {}},
    {"id": "fabrication_welding", "title": "Welding / Fabrication", "order": 9, "default": True,
     "overrides": {}},
    {"id": "electrical_mechanical", "title": "Electrical / Mechanical", "order": 10, "default": True,
     "overrides": {}},
    {"id": "technician", "title": "Technician", "order": 11, "default": True, "overrides": {}},
    {"id": "other", "title": "Other", "order": 99, "default": True, "overrides": {}},
]

#: The destinations the bot already offered, as rows. `Singapore` and `Malaysia`
#: are separate because a rule about one cannot be applied to a record that says
#: "one of these two, we never asked".
SEED_COUNTRIES = [
    ("Singapore", 1),
    ("Malaysia", 2),
    ("Saudi Arabia", 3),
    ("United Arab Emirates", 4),
    ("Qatar", 5),
    ("Kuwait", 6),
    ("Oman", 7),
    ("Bahrain", 8),
]


def seed_taxonomy() -> None:
    """Write the built-in jobs and countries, once, if they are not there.

    Idempotent and non-destructive: an existing row is left exactly as it is,
    including its CV rules. A restart must never quietly reset a rule an admin
    changed — that is the failure that would make the whole feature untrustable.
    """
    jobs = jobs_collection()
    added = 0
    for seed in SEED_JOBS:
        if jobs.find_one({"_id": seed["id"]}):
            continue
        jobs.insert_one(
            job_doc(
                job_id=seed["id"],
                title=seed["title"],
                cv_required_default=seed["default"],
                cv_overrides=seed["overrides"],
                bot_visible=seed["id"] != "other",
                bot_order=seed["order"],
                created_by="seed",
            )
        )
        added += 1

    countries = countries_collection()
    added_countries = 0
    for name, order in SEED_COUNTRIES:
        if countries.find_one({"_id": slugify(name)}):
            continue
        countries.insert_one(country_doc(name=name, bot_order=order, created_by="seed"))
        added_countries += 1

    if added or added_countries:
        log.info("Seeded %d job designation(s) and %d country/countries", added, added_countries)


def taxonomy_version() -> str:
    """A stamp that changes whenever the table does.

    Recorded on every candidate as `cv_policy_version`, so a decision can be
    traced back to the rules that were in force when it was made. Derived from
    the row count and the latest edit rather than incremented by hand, because a
    version nobody remembers to bump is worse than none.
    """
    jobs = jobs_collection()
    count = jobs.count_documents({})
    latest = jobs.find_one(sort=[("updated_at", -1)], projection={"updated_at": 1})
    stamp = latest.get("updated_at") if latest else None
    return f"db-{count}-{int(stamp.timestamp()) if stamp else 0}"
