"""MongoDB connection + index bootstrap.

Sync PyMongo client (the pipeline and Celery workers are synchronous). The
FastAPI layer reuses the same repository via a threadpool. A cached client is
shared per-process.
"""
from __future__ import annotations

import re
from functools import lru_cache

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _redact_uri(uri: str) -> str:
    """Strip credentials before logging. The raw URI carries the DB password,
    and log files are far more widely readable than .env is."""
    return re.sub(r"://[^@/]+@", "://***:***@", uri)


def _setup_dns_resolver() -> None:
    try:
        import dns.resolver
        res = dns.resolver.Resolver(configure=True)
        res.nameservers.extend(["8.8.8.8", "1.1.1.1"])
        dns.resolver.default_resolver = res
    except Exception:
        pass



@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    _setup_dns_resolver()
    log.info("Connecting to MongoDB at %s", _redact_uri(settings.mongo_uri))
    return MongoClient(
        settings.mongo_uri,
        tz_aware=True,
        maxPoolSize=50,
        minPoolSize=1,
        maxIdleTimeMS=120000,
        connectTimeoutMS=20000,
        socketTimeoutMS=30000,
        serverSelectionTimeoutMS=20000,
        retryWrites=True,
        retryReads=True,
    )


def get_db() -> Database:
    return get_client()[settings.mongo_db]


def get_candidates_collection() -> Collection:
    return get_db()[settings.mongo_candidates_collection]


def ensure_index(collection: Collection, keys, name: str, **options) -> bool:
    """Create one index, tolerating a cluster that already has an equivalent.

    An index that exists under a *different* name is not an error to recover
    from — it is the same index, put there by an earlier version of this
    function or by hand in Atlas, and the queries it serves are already fast.
    Mongo disagrees and raises `IndexOptionsConflict`, which used to abort the
    whole of `ensure_indexes` at the first mismatch: everything declared after
    it, including the unique index on user emails, then silently never ran.

    So each index stands or falls on its own, and a failure is reported rather
    than being allowed to take the rest of the list with it.
    """
    from pymongo.errors import OperationFailure

    try:
        collection.create_index(keys, name=name, **options)
        return True
    except OperationFailure as exc:
        # 85 IndexOptionsConflict / 86 IndexKeySpecsConflict — same keys,
        # different name or options.
        if exc.code in (85, 86):
            log.debug("Index %s.%s already present in another form: %s", collection.name, name, exc)
            return True
        log.warning("Could not create index %s.%s: %s", collection.name, name, exc)
        return False
    except Exception as exc:  # noqa: BLE001 — never block startup on an index
        log.warning("Could not create index %s.%s: %s", collection.name, name, exc)
        return False


def _prepare_email_keys(collection: Collection) -> None:
    """Backfill email keys and flag legacy collisions before indexing.

    ``email_key`` is what "this is the same person" has always meant on the
    ingestion path — `find_by_email_or_phone` returns a duplicate on an email
    match alone — but nothing enforced it. The check was a read followed by an
    insert, and a read cannot win the race it exists for: one application
    delivered to two of the polled mailboxes is fetched as two messages, and
    `ingestion_max_workers` processes them at the same time. Both threads look
    for the address, both find nothing, and both insert. That is one candidate
    arriving twice with two ids, two allocations and two auto-replies.

    Backfilled from ``profile.email`` as well as the stored key, because records
    written before the key existed carry only the address, and a key that is
    absent cannot collide — which would let the very duplicates this is meant to
    stop keep their place.

    Unlike the passport routine, a colliding record is flagged but **not** given
    ``status: "duplicate"``. A passport number is a hard identity; an email
    address read off a degraded scan is not, and hiding a real candidate from
    the CRM on that evidence is the worse error of the two. The key is released
    so the index can build, the collision is recorded on both records for an
    operator to settle, and the row stays where its recruiter can see it. New
    duplicates never get this far — `CandidateRepository.insert` resolves them
    to the canonical record.
    """
    from app.core.models import utcnow
    from app.db.dedup import normalize_email

    rows = list(
        collection.find(
            {}, {"_id": 1, "created_at": 1, "email_key": 1, "profile.email": 1},
        ).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
    )
    order = {str(row["_id"]): position for position, row in enumerate(rows)}

    groups: dict[str, list[dict]] = {}
    for row in rows:
        profile = row.get("profile") or {}
        key = normalize_email(row.get("email_key") or profile.get("email"))
        if key:
            groups.setdefault(key, []).append(row)

    now = utcnow()
    for key, candidates in groups.items():
        candidates.sort(key=lambda row: order[str(row["_id"])])
        canonical = candidates[0]
        # Only when it would actually change something. This runs at every
        # startup over every candidate, and an unconditional write per distinct
        # address is thousands of round trips on a settled collection to set
        # each field to the value it already holds.
        if canonical.get("email_key") != key:
            collection.update_one({"_id": canonical["_id"]}, {"$set": {"email_key": key}})
        if len(candidates) == 1:
            continue

        canonical_id = str(canonical["_id"])
        candidate_ids = [str(row["_id"]) for row in candidates]
        review = {
            "reason": "duplicate_email",
            "email_key": key,
            "candidate_ids": candidate_ids,
            "flagged_at": now,
        }
        collection.update_one(
            {"_id": canonical["_id"]}, {"$set": {"identity_review": review}}
        )
        for duplicate in candidates[1:]:
            collection.update_one(
                {"_id": duplicate["_id"]},
                {
                    "$unset": {"email_key": ""},
                    "$set": {
                        "duplicate_of": canonical_id,
                        "identity_review": review,
                        "updated_at": now,
                    },
                },
            )
        log.warning(
            "Email %s is attached to %d candidates (%s); kept %s as canonical and "
            "flagged the rest for review",
            key, len(candidates), ", ".join(candidate_ids), canonical_id,
        )


def _prepare_passport_keys(collection: Collection, passport_collection: Collection) -> None:
    """Backfill passport keys and quarantine legacy collisions before indexing.

    Older records kept the number only in ``profile`` or in the passport
    collection. If historical records already collide, the oldest remains the
    canonical owner. Later records keep all their data but are marked as
    duplicates and no longer claim the unique key.
    """
    from app.core.models import utcnow
    from app.db.dedup import normalize_passport

    rows = list(
        collection.find(
            {},
            {"_id": 1, "created_at": 1, "passport_key": 1, "profile.passport_number": 1},
        ).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
    )
    by_id = {str(row["_id"]): row for row in rows}
    order = {str(row["_id"]): position for position, row in enumerate(rows)}

    # Email bundles normally keep the number in the separate identity
    # collection. It is still the same person-level identity.
    try:
        identity_rows = passport_collection.find(
            {"candidate_id": {"$nin": [None, ""]}, "passport_number": {"$nin": [None, ""]}},
            {"candidate_id": 1, "passport_number": 1},
        )
        for identity in identity_rows:
            candidate_id = str(identity.get("candidate_id"))
            candidate = by_id.get(candidate_id)
            if candidate and not candidate.get("_identity_passport_number"):
                candidate["_identity_passport_number"] = identity.get("passport_number")
    except Exception as exc:  # noqa: BLE001 - candidate-side values still suffice
        log.warning("Could not include passport identity rows in key backfill: %s", exc)

    groups: dict[str, list[dict]] = {}
    for row in rows:
        profile = row.get("profile") or {}
        key = normalize_passport(
            row.get("passport_key")
            or profile.get("passport_number")
            or row.get("_identity_passport_number")
        )
        if key:
            groups.setdefault(key, []).append(row)

    now = utcnow()
    for key, candidates in groups.items():
        candidates.sort(key=lambda row: order[str(row["_id"])])
        canonical = candidates[0]
        canonical_id = str(canonical["_id"])
        collection.update_one(
            {"_id": canonical["_id"]},
            {"$set": {"passport_key": key}},
        )
        if len(candidates) == 1:
            continue

        candidate_ids = [str(row["_id"]) for row in candidates]
        review = {
            "reason": "duplicate_passport",
            "passport_key": key,
            "candidate_ids": candidate_ids,
            "flagged_at": now,
        }
        collection.update_one(
            {"_id": canonical["_id"]},
            {"$set": {"identity_review": review}},
        )
        for duplicate in candidates[1:]:
            collection.update_one(
                {"_id": duplicate["_id"]},
                {
                    "$unset": {"passport_key": "", "passport_key_source": ""},
                    "$set": {
                        "status": "duplicate",
                        "duplicate_of": canonical_id,
                        "identity_review": review,
                        "updated_at": now,
                    },
                },
            )
        log.warning(
            "Passport %s was attached to %d candidates; kept %s as canonical",
            key,
            len(candidates),
            canonical_id,
        )


def ensure_indexes() -> None:
    """Create the indexes the pipeline relies on. Safe to call repeatedly."""
    db = get_db()
    coll = get_candidates_collection()
    # Add a public CRM id to records written before the field existed. The
    # generator is deterministic, so this is safe after interrupted startups
    # and produces the exact same value as a legacy detail read.
    from app.core.crm_ids import candidate_code

    for doc in coll.find(
        {"$or": [
            {"candidate_code": {"$exists": False}},
            {"candidate_code": None},
            {"candidate_code": ""},
        ]},
        {"_id": 1},
    ):
        coll.update_one(
            {"_id": doc["_id"]},
            {"$set": {"candidate_code": candidate_code(doc["_id"])}},
        )
    ensure_index(
        coll,
        [("candidate_code", ASCENDING)],
        "candidate_code_unique",
        unique=True,
        sparse=True,
    )
    # Exact-duplicate detection: one candidate per resume file hash.
    ensure_index(coll, [("resume_hash", ASCENDING)], "resume_hash_unique", unique=True, sparse=True)
    # Person-level dedup lookups.
    _prepare_passport_keys(coll, db[settings.mongo_passport_collection])
    ensure_index(
        coll,
        [("passport_key", ASCENDING)],
        "passport_key_unique",
        unique=True,
        sparse=True,
    )
    # One candidate per email address, enforced rather than hoped for.
    #
    # Sparse, and safe to be: `CandidateRecord.to_mongo` dumps with
    # `exclude_none=True` and `normalize_email` returns None (never "") for a
    # blank, so a candidate with no address carries no `email_key` field at all
    # and is not indexed. A sparse index skips a *missing* field, not a null
    # one, which is the trap this would otherwise fall into — every email-less
    # candidate colliding on `null` and only the first being allowed to exist.
    #
    # Phone deliberately stays non-unique. One number legitimately reaches more
    # than one candidate — an agent's mobile on a family's applications — and
    # `find_by_email_or_phone` already documents that. Making it unique would
    # merge people who are not the same person.
    _prepare_email_keys(coll)
    ensure_index(
        coll, [("email_key", ASCENDING)], "email_key_unique", unique=True, sparse=True
    )
    ensure_index(coll, [("phone_key", ASCENDING)], "phone_key_idx", sparse=True)
    # Idempotency: don't reprocess the same Gmail message.
    ensure_index(coll, [("source_email.message_id", ASCENDING)], "source_msg_idx")
    # Idempotency for API submissions, and the only thing that actually makes
    # them idempotent.
    #
    # The intake service looks the key up before inserting, but a lookup cannot
    # win the race it exists for: two retries of the same WhatsApp submission
    # arriving together both read an empty collection and both insert. The
    # unique index is what turns the second insert into a DuplicateKeyError,
    # which `CandidateRepository.insert` catches and resolves by returning the
    # candidate the winner created. Without this index that path is unreachable
    # and concurrent retries quietly create two people.
    #
    # Sparse because email candidates carry no key: they are deduplicated by
    # message id and résumé hash, and a unique index over a field they all lack
    # would let exactly one of them exist.
    ensure_index(
        coll,
        [("idempotency_key", ASCENDING)],
        "idempotency_key_unique",
        unique=True,
        sparse=True,
    )
    # Common future query paths (search/filter extension).
    ensure_index(coll, [("profile.skills", ASCENDING)], "skills_idx")
    ensure_index(coll, [("created_at", ASCENDING)], "created_at_idx")
    # Every staff-side read is scoped to one owner and sorted newest-first, and
    # the workload aggregation groups on the same key.
    ensure_index(
        coll,
        [("assigned_staff_id", ASCENDING), ("created_at", DESCENDING)],
        "assigned_staff_created_idx",
    )
    # The SLA sweep: assigned, past its deadline, still unopened or unjudged.
    ensure_index(coll, [("assigned_at", ASCENDING)], "assigned_at_idx", sparse=True)

    # Sourcing Clients & Job Orders Collections
    ensure_index(db["sourcing_clients"], [("id", ASCENDING)], "sourcing_client_id_idx", sparse=True)
    ensure_index(db["job_orders"], [("id", ASCENDING)], "job_order_id_idx", sparse=True)

    # Durable "already ingested / user deleted" ledger, the accounts, and the
    # notification feed — the last of which carries the TTL that stops the
    # collection growing without bound, so it is not optional.
    from app.db.b2b_enquiries import ensure_b2b_indexes
    from app.db.identity_records import ensure_identity_indexes
    from app.db.ingestion_state import ensure_ingestion_state_indexes
    from app.db.ledger import ensure_ledger_indexes
    from app.db.notifications import ensure_notification_indexes
    from app.db.repository import ensure_candidate_deletion_indexes
    from app.db.taxonomy import ensure_taxonomy_indexes, seed_taxonomy
    from app.db.users import ensure_user_deletion_indexes, ensure_user_indexes

    ensure_ledger_indexes()
    ensure_user_indexes()
    ensure_user_deletion_indexes()
    ensure_notification_indexes()
    ensure_candidate_deletion_indexes()
    # Manpower requirements the bot collects from agents. The unique index on
    # `idempotency_key` is the one that matters: without it a retried
    # submission becomes a second vacancy and the agency fills one job twice.
    ensure_b2b_indexes()
    # The jobs and countries an admin edits, and the CV rules hanging off them.
    # Seeded here rather than by a migration script so a fresh database answers
    # the same questions a long-running one does — and seeding is additive, so
    # a rule someone changed is never reset by a restart.
    ensure_taxonomy_indexes()
    seed_taxonomy()
    # The multipass state machine and the two identity collections it feeds.
    # The compound unique index is what stops a redelivered email queueing a
    # second Aadhaar job for a card that has already been read.
    ensure_ingestion_state_indexes()
    ensure_identity_indexes()

    log.info(
        "MongoDB indexes ensured on '%s', 'sourcing_clients', 'job_orders', "
        "'b2b_enquiries', 'ingest_ledger', 'users', 'notifications', "
        "'ingestion_state', '%s' and '%s'",
        coll.name,
        settings.mongo_aadhaar_collection,
        settings.mongo_passport_collection,
    )
