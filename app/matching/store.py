"""What matching decided, kept so it is not paid for twice.

Four collections, each with one job:

    candidate_matches         one evaluation of one candidate against one
                              version of one order. Never a copy of the
                              candidate — `candidate_id` and `job_order_id`
                              are references, and the evidence is a digest.
    candidate_match_reviews   the recruiter decision on a candidate for an
                              order. Separate on purpose: editing the order's
                              skills re-scores everyone, and that must not
                              throw away the "rejected — no Gulf licence" a
                              recruiter wrote last week.
    candidate_match_runs      one matching job: its progress, its cost, and
                              the lock that stops two of them running on the
                              same order at once.
    candidate_match_activity  when each order was last opened or edited, so
                              the background sweep spends only on orders
                              somebody is working on.

**The cache key** is everything that can change a score:

    {spec fingerprint}:{evidence fingerprint}:{scoring config}:{schema}:{model}

and `(job_order_id, candidate_id, cache_key)` is unique. Re-running a match
whose order, candidate, weights, questions and model are all unchanged finds
the stored result and spends nothing; change any one of them and it is a miss.

**Current versus history.** A pair can have several results — one per version
of the order or the profile. Exactly one is `is_current`, and it is only ever
replaced by a newer *successful* evaluation. A failed attempt is recorded
beside it and never displaces it: when Jev is down, the shortlist a recruiter
was working from stays on the screen, marked with the failure.

Original candidate records are never written to from here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.logging_config import get_logger

log = get_logger(__name__)

MATCHES = "candidate_matches"
REVIEWS = "candidate_match_reviews"
RUNS = "candidate_match_runs"
ACTIVITY = "candidate_match_activity"

#: `screened`: decided by the free check in `app.matching.screen`, never sent
#: to Jev. Stored under the same cache key a Jev result would use, so a later
#: "Check with Jev" simply overwrites it.
SCORED, FAILED, SCREENED = "scored", "failed", "screened"

RUN_QUEUED, RUN_RUNNING = "queued", "running"
RUN_COMPLETED, RUN_PARTIAL, RUN_FAILED, RUN_CANCELLED = "completed", "partial", "failed", "cancelled"
ACTIVE_RUN_STATES = (RUN_QUEUED, RUN_RUNNING)

REVIEW_STATES = ("pending", "shortlisted", "rejected", "manual_review")

#: A run that has not reported progress for this long is assumed dead — its
#: process restarted mid-run — and its lock is released so the order is not
#: stuck behind a run nobody is executing.
STALE_RUN_AFTER = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from app.db.mongo import get_db

    return get_db()


def ensure_indexes() -> None:
    """Called from `app.db.mongo.ensure_indexes`. Safe to run repeatedly."""
    from app.db.mongo import ensure_index

    db = _db()
    matches = db[MATCHES]
    ensure_index(
        matches,
        [("job_order_id", ASCENDING), ("candidate_id", ASCENDING), ("cache_key", ASCENDING)],
        "match_pair_version_unique", unique=True,
    )
    # The shortlist read: one order's current results.
    ensure_index(matches, [("job_order_id", ASCENDING), ("is_current", ASCENDING)],
                 "match_order_current_idx")
    # "What has this candidate been matched to" — the profile side.
    ensure_index(matches, [("candidate_id", ASCENDING), ("updated_at", DESCENDING)],
                 "match_candidate_idx")

    reviews = db[REVIEWS]
    ensure_index(reviews, [("job_order_id", ASCENDING), ("candidate_id", ASCENDING)],
                 "match_review_pair_unique", unique=True)

    runs = db[RUNS]
    ensure_index(runs, [("id", ASCENDING)], "match_run_id_unique", unique=True)
    ensure_index(runs, [("job_order_id", ASCENDING), ("created_at", DESCENDING)],
                 "match_run_order_idx")
    # The concurrency lock. Only an active run carries `active_lock`, and it
    # carries its order id there, so a second active run on the same order is
    # a duplicate key — enforced by the database, not by a check-then-insert
    # that two requests can both pass.
    ensure_index(runs, [("active_lock", ASCENDING)], "match_run_active_lock_unique",
                 unique=True, sparse=True)

    ensure_index(db[ACTIVITY], [("job_order_id", ASCENDING)], "match_activity_order_unique",
                 unique=True)


# --------------------------------------------------------------------------- #
#  Results
# --------------------------------------------------------------------------- #
def cache_key(spec_fp: str, evidence_fp: str, config_version: str, schema_version: str, model: str) -> str:
    return ":".join((spec_fp, evidence_fp, config_version, schema_version, model))


def find_scored(job_order_id: str, candidate_id: str, key: str) -> Optional[Dict[str, Any]]:
    return _db()[MATCHES].find_one(
        {"job_order_id": job_order_id, "candidate_id": candidate_id,
         "cache_key": key, "status": SCORED},
        {"_id": 0},
    )


def save_scored(doc: Dict[str, Any], status: str = SCORED) -> None:
    """Store a successful evaluation and make it the current one for its pair.

    `status=SCREENED` stores the free check's verdict the same way: it is the
    current result for its pair until the inputs change or Jev is asked.
    """
    coll = _db()[MATCHES]
    now = _now()
    key = {"job_order_id": doc["job_order_id"], "candidate_id": doc["candidate_id"],
           "cache_key": doc["cache_key"]}
    body = {**doc, "status": status, "is_current": True, "error": None,
            "last_failure": None, "updated_at": now}
    body.pop("created_at", None)
    coll.update_one(key, {"$set": body, "$setOnInsert": {"created_at": now}}, upsert=True)
    coll.update_many(
        {"job_order_id": doc["job_order_id"], "candidate_id": doc["candidate_id"],
         "cache_key": {"$ne": doc["cache_key"]}, "is_current": True},
        {"$set": {"is_current": False, "updated_at": now}},
    )


def mark_current(job_order_id: str, candidate_id: str, key: str) -> None:
    """A cache hit becomes the current result again, if a newer one displaced it."""
    coll = _db()[MATCHES]
    now = _now()
    coll.update_one({"job_order_id": job_order_id, "candidate_id": candidate_id, "cache_key": key},
                    {"$set": {"is_current": True, "updated_at": now}})
    coll.update_many(
        {"job_order_id": job_order_id, "candidate_id": candidate_id,
         "cache_key": {"$ne": key}, "is_current": True},
        {"$set": {"is_current": False, "updated_at": now}},
    )


def save_failed(doc: Dict[str, Any]) -> None:
    """Record a failed attempt without touching the current result.

    Two cases, because the unique key allows one document per version:

    * nothing scored under this key yet — the failure is its own document;
    * a scored result already holds the key (a forced re-evaluation that then
      failed) — the failure is noted on it as `last_failure`, leaving its
      score exactly as it was.

    Without the second case the upsert would collide with the scored document
    and the failure would vanish, which is the one thing a failure must not do.
    """
    coll = _db()[MATCHES]
    now = _now()
    key = {"job_order_id": doc["job_order_id"], "candidate_id": doc["candidate_id"],
           "cache_key": doc["cache_key"]}
    note = {"error": doc.get("error"), "run_id": doc.get("run_id"), "at": now}

    if coll.find_one({**key, "status": SCORED}, {"_id": 1}):
        coll.update_one({**key, "status": SCORED}, {"$set": {"last_failure": note}})
        return

    body = {**doc, "status": FAILED, "is_current": False, "updated_at": now}
    body.pop("created_at", None)
    try:
        coll.update_one({**key, "status": {"$ne": SCORED}},
                        {"$set": body, "$setOnInsert": {"created_at": now}}, upsert=True)
    except DuplicateKeyError:
        # A concurrent attempt stored a success under the same key between the
        # check above and this write. Note the failure on it rather than lose it.
        coll.update_one({**key, "status": SCORED}, {"$set": {"last_failure": note}})


#: What a list row or a count needs from a result. The explanation route reads
#: the whole document; everything else leaves out the per-question answers,
#: which are most of its size, so a shortlist of thousands opens quickly.
LIST_FIELDS = {"_id": 0, "evaluation": 0, "reasons": 0, "baseline": 0, "usage": 0}
COUNT_FIELDS = {"_id": 0, "candidate_id": 1, "category": 1, "evaluated_at": 1, "status": 1,
                "last_failure": 1, "error": 1, "updated_at": 1, "run_id": 1}


def current_keys(job_order_id: str) -> Dict[str, Dict[str, Any]]:
    """Each candidate's current result for an order: its cache key and age.

    One read for a whole run, so a candidate whose result is already current
    is recognised in memory instead of with a query and two writes apiece —
    which is what lets the background sweep look at every open order often.
    """
    return {
        doc["candidate_id"]: doc
        for doc in _db()[MATCHES].find(
            {"job_order_id": job_order_id, "is_current": True},
            {"_id": 0, "candidate_id": 1, "cache_key": 1, "evaluated_at": 1, "status": 1},
        )
    }


def failed_keys(job_order_id: str) -> Dict[tuple, Dict[str, Any]]:
    """Failed attempts for an order, by (candidate, cache key)."""
    out: Dict[tuple, Dict[str, Any]] = {}
    for doc in _db()[MATCHES].find(
        {"job_order_id": job_order_id, "status": FAILED},
        {"_id": 0, "candidate_id": 1, "cache_key": 1, "updated_at": 1, "error": 1},
    ):
        out[(doc["candidate_id"], doc.get("cache_key"))] = {
            "at": _aware(doc.get("updated_at")),
            "retryable": bool((doc.get("error") or {}).get("retryable")),
        }
    return out


def results_for_order(
    job_order_id: str,
    fields: Optional[Dict[str, int]] = None,
    candidate_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Every current result for an order, with the newest failure beside each.

    Read whole and grouped here rather than paginated in Mongo — the sort is
    category-then-score, which is not an index order. `fields` narrows what
    is read (`LIST_FIELDS`, `COUNT_FIELDS`); the default is everything.
    `candidate_id` reads one candidate's entry only.
    """
    coll = _db()[MATCHES]
    projection = fields or {"_id": 0}
    scope: Dict[str, Any] = {"job_order_id": job_order_id}
    if candidate_id is not None:
        scope["candidate_id"] = candidate_id
    current = {doc["candidate_id"]: doc
               for doc in coll.find({**scope, "is_current": True}, projection)}
    failures: Dict[str, Dict[str, Any]] = {}
    for doc in coll.find({**scope, "status": FAILED}, projection).sort("updated_at", ASCENDING):
        failures[doc["candidate_id"]] = doc

    for candidate_id, result in current.items():
        # A failed re-evaluation of the same version is noted on the result
        # itself (see `save_failed`); it is reported exactly like the others.
        noted = result.get("last_failure")
        if noted:
            previous = failures.get(candidate_id)
            if not previous or _aware(noted["at"]) > _aware(previous.get("updated_at")):
                failures[candidate_id] = {"error": noted.get("error"), "updated_at": noted["at"],
                                          "run_id": noted.get("run_id")}

    out: List[Dict[str, Any]] = []
    for candidate_id in set(current) | set(failures):
        result = current.get(candidate_id)
        failure = failures.get(candidate_id)
        if result and failure and failure.get("updated_at") and result.get("evaluated_at") \
                and _aware(failure["updated_at"]) <= _aware(result["evaluated_at"]):
            failure = None
        out.append({"candidate_id": candidate_id, "result": result, "failure": failure})
    return out


def result_for(job_order_id: str, candidate_id: str) -> Optional[Dict[str, Any]]:
    return _db()[MATCHES].find_one(
        {"job_order_id": job_order_id, "candidate_id": candidate_id, "is_current": True},
        {"_id": 0},
    )


def _aware(value: Any) -> datetime:
    """Mongo hands datetimes back naive; everything here is UTC."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# --------------------------------------------------------------------------- #
#  Recruiter decisions
# --------------------------------------------------------------------------- #
def reviews_for_order(job_order_id: str) -> Dict[str, Dict[str, Any]]:
    return {doc["candidate_id"]: doc
            for doc in _db()[REVIEWS].find({"job_order_id": job_order_id}, {"_id": 0})}


def save_review(job_order_id: str, candidate_id: str, status: str, note: str, user: Dict[str, Any]) -> Dict[str, Any]:
    if status not in REVIEW_STATES:
        raise ValueError(f"Unknown review status {status!r}")
    now = _now()
    entry = {
        "status": status, "note": note, "at": now,
        "by_id": user.get("id"), "by_name": user.get("name") or user.get("email"),
    }
    _db()[REVIEWS].update_one(
        {"job_order_id": job_order_id, "candidate_id": candidate_id},
        {"$set": {**entry, "updated_at": now},
         "$setOnInsert": {"created_at": now},
         # The trail, because "who rejected this and when" is the question a
         # client asks after the fact.
         "$push": {"history": {"$each": [entry], "$slice": -20}}},
        upsert=True,
    )
    return _db()[REVIEWS].find_one({"job_order_id": job_order_id, "candidate_id": candidate_id}, {"_id": 0})


# --------------------------------------------------------------------------- #
#  Activity: which orders somebody is working on
# --------------------------------------------------------------------------- #
VIEWED, EDITED = "viewed_at", "edited_at"


def touch(job_order_id: str, what: str) -> None:
    """Record that an order was just opened (`VIEWED`) or saved (`EDITED`)."""
    now = _now()
    _db()[ACTIVITY].update_one(
        {"job_order_id": job_order_id},
        {"$set": {what: now, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


def activity(job_order_ids: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if job_order_ids is not None:
        query["job_order_id"] = {"$in": list(job_order_ids)}
    return {doc["job_order_id"]: {key: _aware(value) for key, value in doc.items()}
            for doc in _db()[ACTIVITY].find(query, {"_id": 0})}


# --------------------------------------------------------------------------- #
#  Runs
# --------------------------------------------------------------------------- #
class RunConflict(RuntimeError):
    """Another run is already active on this order."""

    def __init__(self, run: Optional[Dict[str, Any]]):
        super().__init__("A matching run is already in progress for this job order")
        self.run = run


def _release_stale(job_order_id: str) -> None:
    coll = _db()[RUNS]
    cutoff = _now() - STALE_RUN_AFTER
    for run in coll.find({"active_lock": job_order_id}):
        heartbeat = _aware(run.get("updated_at") or run.get("created_at"))
        if heartbeat and heartbeat < cutoff:
            log.warning("Releasing abandoned matching run %s on %s", run.get("id"), job_order_id)
            coll.update_one(
                {"id": run["id"]},
                {"$set": {"status": RUN_FAILED, "error": "Abandoned: no progress reported",
                          "finished_at": _now(), "updated_at": _now()},
                 "$unset": {"active_lock": ""}},
            )


def create_run(job_order_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a queued run, or raise `RunConflict` if one is already active."""
    coll = _db()[RUNS]
    _release_stale(job_order_id)
    now = _now()
    run = {
        "id": f"MR-{uuid.uuid4().hex[:12]}",
        "job_order_id": job_order_id,
        "active_lock": job_order_id,
        "status": RUN_QUEUED,
        "retrieved": 0, "considered": 0, "skipped": 0, "deferred": 0, "screened": 0,
        "processed": 0, "scored": 0, "cached": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        "cancel_requested": False, "error": None,
        "created_at": now, "updated_at": now, "started_at": None, "finished_at": None,
        **fields,
    }
    # Belt and braces for a store without the unique index (mongomock honours
    # it; an Atlas cluster whose index build failed would not).
    existing = coll.find_one({"active_lock": job_order_id}, {"_id": 0})
    if existing:
        raise RunConflict(existing)
    try:
        coll.insert_one(dict(run))
    except DuplicateKeyError:
        raise RunConflict(coll.find_one({"active_lock": job_order_id}, {"_id": 0}))
    return run


def update_run(run_id: str, *, inc: Optional[Dict[str, Any]] = None, **fields: Any) -> None:
    update: Dict[str, Any] = {"$set": {**fields, "updated_at": _now()}}
    if inc:
        update["$inc"] = inc
    _db()[RUNS].update_one({"id": run_id}, update)


def finish_run(run_id: str, status: str, error: Optional[str] = None) -> None:
    _db()[RUNS].update_one(
        {"id": run_id},
        {"$set": {"status": status, "error": error, "finished_at": _now(), "updated_at": _now()},
         "$unset": {"active_lock": ""}},
    )


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    return _db()[RUNS].find_one({"id": run_id}, {"_id": 0, "active_lock": 0})


def latest_run(job_order_id: str) -> Optional[Dict[str, Any]]:
    rows = list(_db()[RUNS].find({"job_order_id": job_order_id}, {"_id": 0, "active_lock": 0})
                .sort("created_at", DESCENDING).limit(1))
    return rows[0] if rows else None


def latest_full_run(job_order_id: str) -> Optional[Dict[str, Any]]:
    """The newest run over the whole pool — not a one-candidate refresh.

    Coverage ("Jev checked X of Y") has to come from a run that looked at the
    pool; a refresh retrieves one candidate and would report a pool of one.
    """
    rows = list(_db()[RUNS].find({"job_order_id": job_order_id, "candidate_ids": None},
                                 {"_id": 0, "active_lock": 0})
                .sort("created_at", DESCENDING).limit(1))
    return rows[0] if rows else None


def cancel_requested(run_id: str) -> bool:
    run = _db()[RUNS].find_one({"id": run_id}, {"cancel_requested": 1})
    return bool(run and run.get("cancel_requested"))


def request_cancel(run_id: str) -> bool:
    result = _db()[RUNS].update_one(
        {"id": run_id, "status": {"$in": list(ACTIVE_RUN_STATES)}},
        {"$set": {"cancel_requested": True, "updated_at": _now()}},
    )
    return bool(result.modified_count)


def candidate_ids(rows: Iterable[Dict[str, Any]]) -> List[str]:
    return [str(row["candidate_id"]) for row in rows if row.get("candidate_id")]
