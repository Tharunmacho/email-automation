"""MongoDB connection + index bootstrap.

Sync PyMongo client (the pipeline and Celery workers are synchronous). The
FastAPI layer reuses the same repository via a threadpool. A cached client is
shared per-process.
"""
from __future__ import annotations

from functools import lru_cache

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


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
    log.info("Connecting to MongoDB at %s", settings.mongo_uri)
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


def ensure_indexes() -> None:
    """Create the indexes the pipeline relies on. Safe to call repeatedly."""
    db = get_db()
    coll = get_candidates_collection()
    # Exact-duplicate detection: one candidate per resume file hash.
    coll.create_index([("resume_hash", ASCENDING)], name="resume_hash_unique", unique=True, sparse=True)
    # Person-level dedup lookups.
    coll.create_index([("email_key", ASCENDING)], name="email_key_idx", sparse=True)
    coll.create_index([("phone_key", ASCENDING)], name="phone_key_idx", sparse=True)
    # Idempotency: don't reprocess the same Gmail message.
    coll.create_index([("source_email.message_id", ASCENDING)], name="source_msg_idx")
    # Common future query paths (search/filter extension).
    coll.create_index([("profile.skills", ASCENDING)], name="skills_idx")
    coll.create_index([("created_at", ASCENDING)], name="created_at_idx")

    # Sourcing Clients & Job Orders Collections
    db["sourcing_clients"].create_index([("id", ASCENDING)], name="sourcing_client_id_idx", sparse=True)
    db["job_orders"].create_index([("id", ASCENDING)], name="job_order_id_idx", sparse=True)

    log.info("MongoDB indexes ensured on '%s', 'sourcing_clients', and 'job_orders'", coll.name)
