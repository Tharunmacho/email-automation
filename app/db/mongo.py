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


def ensure_indexes() -> None:
    """Create the indexes the pipeline relies on. Safe to call repeatedly."""
    db = get_db()
    coll = get_candidates_collection()
    # Exact-duplicate detection: one candidate per resume file hash.
    ensure_index(coll, [("resume_hash", ASCENDING)], "resume_hash_unique", unique=True, sparse=True)
    # Person-level dedup lookups.
    ensure_index(coll, [("email_key", ASCENDING)], "email_key_idx", sparse=True)
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
    from app.db.taxonomy import ensure_taxonomy_indexes, seed_taxonomy
    from app.db.users import ensure_user_indexes

    ensure_ledger_indexes()
    ensure_user_indexes()
    ensure_notification_indexes()
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
