"""The SLA sweep (`app.tasks.sla_checker`).

Measures arrival-to-verdict turnaround against ``settings.sla_threshold_hours``
— 24 hours by default, one working day. A profile is in breach when its clock
has been running for longer than the threshold and its owner has still not
opened it (`viewed_at` is null) or still not judged it (`evaluation_status` is
"pending").

The clock starts at `assigned_at`, falling back to `ingested_at` and then
`created_at` — see ``CandidateRepository.SLA_CLOCK_EXPR``, which the query uses
and ``_clock_started`` below mirrors. Allocation is what creates the obligation,
so it is the honest start; the fallbacks exist because records written by older
builds have no `assigned_at` at all, and matching on that field alone silently
excluded exactly the profiles that had been waiting longest.

The sweep runs on a beat timer, so it fires at whatever o'clock the interval
lands on — usually with nobody watching. That shapes the whole design:

  * **One alert per profile.** A breach already recorded as `active` is not
    re-recorded and not re-pushed. Re-firing the same modal every fifteen
    minutes is what gets an alert channel muted, and it would inflate the audit
    log into claiming a hundred separate things went wrong.
  * **Alerts resolve themselves.** A profile that drops out of the breach set —
    someone dealt with it — closes its alert on the next sweep.
  * **Written down as well as pushed.** The modal only reaches whoever is
    looking; the admins' feed is what is still there in the morning.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db.mongo import get_db
from app.db.repository import CandidateRepository
from app.logging_config import get_logger
from app.notifications import notify_sla_breaches
from app.tasks.celery_app import celery_app

log = get_logger(__name__)

ALERTS_COLLECTION = "sla_alerts"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_alerts_collection():
    return get_db()[ALERTS_COLLECTION]


def _threshold(threshold_hours: float | None) -> float:
    return float(
        threshold_hours if threshold_hours is not None else settings.sla_threshold_hours
    )


def _hours_since(moment: Optional[datetime], now: datetime) -> float:
    """Hours between `moment` and `now`, tolerating a naive timestamp.

    Documents written before the driver was configured tz-aware come back naive,
    and subtracting one of those from an aware `now` raises TypeError — which
    would take down the whole sweep over one old row.
    """
    if not isinstance(moment, datetime):
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (now - moment).total_seconds() / 3600.0)


def _clock_started(row: Dict[str, Any]) -> Optional[datetime]:
    """When this profile started waiting — the Python twin of `SLA_CLOCK_EXPR`.

    Both have to agree: the query decides *whether* a row is in breach and this
    decides *by how much*, and a row selected by one and measured at zero by the
    other would be reported as "0h overdue".
    """
    for field in ("assigned_at", "ingested_at", "created_at"):
        moment = row.get(field)
        if isinstance(moment, datetime):
            return moment
    return None


def _row_to_alert(row: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Turn a breaching candidate document into the alert the console renders."""
    profile = row.get("profile") or {}
    name = profile.get("full_name") or profile.get("email") or "Unnamed"
    started = _clock_started(row)
    return {
        "candidate_id": str(row.get("_id")),
        "full_name": name,
        "candidate_name": name,
        "assigned_staff_id": row.get("assigned_staff_id"),
        "assigned_staff_name": row.get("assigned_staff_name") or "Staff",
        "assigned_at": row.get("assigned_at"),
        "hours_overdue": _hours_since(started, now),
        # The admin's first question is always "did they even look at it?".
        "reason": "unviewed" if not row.get("viewed_at") else "unevaluated",
    }


def find_breaches(threshold_hours: float | None = None) -> List[Dict[str, Any]]:
    """Every profile currently in breach, across the whole roster."""
    hours = _threshold(threshold_hours)
    now = utcnow()
    cutoff = now - timedelta(hours=hours)

    repo = CandidateRepository()
    return [_row_to_alert(row, now) for row in repo.find_sla_breaches(cutoff)]


def list_alerts(status: str | None = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Recorded alerts, newest first. `status` of None means both open and closed."""
    coll = get_alerts_collection()
    query = {} if not status else {"status": status}
    return list(coll.find(query, {"_id": 0}).sort("created_at", -1).limit(limit))


def scan(threshold_hours: float | None = None) -> Dict[str, Any]:
    """Warn managers first, then escalate unresolved work to Yoosuf."""
    hours = _threshold(threshold_hours)
    escalation_hours = max(hours, float(settings.sla_super_admin_threshold_hours))
    now = utcnow()

    breaches = find_breaches(hours)
    alerts = get_alerts_collection()

    open_docs = list(alerts.find({"status": "active"}))
    already_open = {doc["candidate_id"] for doc in open_docs}
    fresh = [b for b in breaches if b["candidate_id"] not in already_open]

    eligible_for_escalation = {
        b["candidate_id"]: b for b in breaches if b["hours_overdue"] >= escalation_hours
    }
    previously_escalated = {
        doc["candidate_id"] for doc in open_docs if doc.get("super_admin_notified_at")
    }
    escalated = [
        breach
        for candidate_id, breach in eligible_for_escalation.items()
        if candidate_id not in previously_escalated
    ]

    if fresh:
        alerts.insert_many([{
            "candidate_id": b["candidate_id"],
            "candidate_name": b["full_name"],
            "assigned_staff_id": b["assigned_staff_id"],
            "assigned_staff_name": b["assigned_staff_name"],
            "hours_overdue": b["hours_overdue"],
            "reason": b["reason"],
            "threshold_hours": hours,
            "manager_notified_at": now,
            "super_admin_notified_at": (
                now if b["candidate_id"] in eligible_for_escalation else None
            ),
            "status": "active",
            "created_at": now,
            "resolved_at": None,
        } for b in fresh])

    existing_escalation_ids = [
        b["candidate_id"] for b in escalated if b["candidate_id"] in already_open
    ]
    if existing_escalation_ids:
        alerts.update_many(
            {"status": "active", "candidate_id": {"$in": existing_escalation_ids}},
            {"$set": {"super_admin_notified_at": now}},
        )

    still_breaching = [b["candidate_id"] for b in breaches]
    resolved = alerts.update_many(
        {"status": "active", "candidate_id": {"$nin": still_breaching}},
        {"$set": {"status": "resolved", "resolved_at": now}},
    ).modified_count

    if fresh:
        try:
            notify_sla_breaches(fresh, hours, recipient_stage="manager")
        except Exception as exc:  # noqa: BLE001 — a lost toast must not fail the sweep
            log.warning("Could not send manager SLA notifications: %s", exc)

    if escalated:
        try:
            notify_sla_breaches(
                escalated,
                escalation_hours,
                recipient_stage="super_admin",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not send super-admin SLA notifications: %s", exc)

    if fresh or escalated or resolved:
        log.info(
            "SLA sweep: %d in breach, %d manager alerts, "
            "%d super-admin escalations, %d resolved",
            len(breaches), len(fresh), len(escalated), resolved,
        )
    return {
        "in_breach": len(breaches),
        "new_alerts": len(fresh),
        "new_escalations": len(escalated),
        "resolved": resolved,
        "threshold_hours": hours,
        "escalation_threshold_hours": escalation_hours,
    }


@celery_app.task(name="app.tasks.sla_checker.scan_sla_breaches")
def scan_sla_breaches() -> Dict[str, Any]:
    """The beat task, run on `settings.sla_scan_interval_seconds`.

    It carries no lock, unlike the reconciler's. `scan` re-derives the breach
    set from the collection every time and skips anything already recorded as
    active, so two overlapping sweeps agree rather than race — and at hourly
    ticks over a two-day window there is nothing to overlap with anyway.
    """
    return scan()
