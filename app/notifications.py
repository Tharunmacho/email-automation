"""What happens when a candidate lands: who is told, and how.

Two deliveries, in this order, and the order is the point:

  1. **Write it down.** A row per recipient in the ``notifications`` collection.
     This is the one that survives the staff member being logged out, the
     browser being closed, Redis being down and the API restarting.
  2. **Push it.** A WebSocket event to whoever happens to be watching, for the
     toast. Best-effort by definition — it reaches open sockets and nothing
     else.
  3. **Send it to their phone.** The WhatsApp bot is asked to message the staff
     member on the number their account carries. Also best-effort, and last on
     purpose: it is the only one of the three that leaves this network, and the
     record it announces is already written by the time it runs.

Doing only (2) is what made the feature look broken: allocation happens on a
Gmail poll, the staff member it allocates to is usually not looking at the
screen that second, and the event went nowhere with no trace left behind.

Both the assigned staff member and the admins are told, because they need
different things from the same event — the staff member is being handed work,
the admin is watching the sync they just started actually produce something.
"""
from __future__ import annotations

from typing import List

from app.db import notifications as store
from app.db.notifications import NotificationRepository
from app.db.users import UserRepository
from app.logging_config import get_logger
from app.staff_whatsapp import relay_assignment, relay_sla_breach

log = get_logger(__name__)


def _admin_ids(users: UserRepository) -> List[str]:
    return [user.id for user in users.list_admins()]


def notify_candidate_assigned(
    staff_id: str,
    candidate: dict,
    *,
    staff_name: str | None = None,
    repo: NotificationRepository | None = None,
    users: UserRepository | None = None,
    relay_whatsapp: bool = True,
) -> int:
    """Record and push "a candidate is now yours" / "a candidate arrived".

    Never raises. A notification that cannot be stored or delivered must not
    fail the ingestion that produced it: the candidate is already in Mongo and
    already allocated, and losing the profile to protect the pop-up about it
    would be exactly the wrong trade.

    Returns how many people were notified, which is what the tests assert on.
    """
    from app.api import websocket as ws  # imported here: routes import both ways

    name = candidate.get("full_name") or candidate.get("email") or "Unnamed candidate"
    candidate_id = candidate.get("id")
    notified = 0

    try:
        repo = repo or NotificationRepository()
        users = users or UserRepository()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not open the notification store (%s); pushing only", exc)
        repo = None

    # ---- the staff member who now owns it -------------------------------- #
    if repo is not None and staff_id:
        try:
            repo.record(
                staff_id,
                type=store.CANDIDATE_ASSIGNED,
                title="New candidate assigned",
                message=f"{name} was allocated to you.",
                candidate_id=candidate_id,
                candidate_name=name,
            )
            notified += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not store the assignment notification: %s", exc)

    try:
        ws.publish_event(ws.candidate_assigned_event(staff_id, candidate))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not push the assignment event: %s", exc)

    # ---- and the admins watching the sync -------------------------------- #
    owner = staff_name or "a staff member"
    admin_message = f"{name} was ingested and allocated to {owner}."
    if repo is not None:
        try:
            for admin_id in _admin_ids(users):
                repo.record(
                    admin_id,
                    type=store.CANDIDATE_INGESTED,
                    title="Candidate ingested",
                    message=admin_message,
                    candidate_id=candidate_id,
                    candidate_name=name,
                )
                notified += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not store the ingest notification for admins: %s", exc)

    try:
        ws.publish_event(ws.candidate_ingested_event(candidate, staff_name=owner))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not push the ingest event: %s", exc)

    # ---- and their phone ------------------------------------------------- #
    # Last, and outside the count: `notified` is how many people this event was
    # *recorded* for, which the tests assert on, and a WhatsApp message that
    # went to somebody already counted is not a second person being told.
    #
    # `relay_assignment` swallows its own failures — see the module — so there
    # is deliberately no try/except around it here. Wrapping it would only
    # catch the import.
    if staff_id and relay_whatsapp:
        relay_assignment(candidate_id, staff_id)

    return notified


def notify_candidate_rejected(
    *,
    reason: str,
    filename: str = "",
    from_addr: str = "",
    country: str = "",
    repo: NotificationRepository | None = None,
    users: UserRepository | None = None,
) -> int:
    """Record and push "a CV arrived and was refused".

    The admins only. A rejection is not work being handed to anybody — it is the
    sync reporting what it declined, which is the admin's question.

    This notification carries more weight than the others in this module,
    because it is the *only* trace the refusal leaves anywhere a recruiter
    looks. An accepted candidate is a row in the CRM whether or not the pop-up
    arrives; a refused one is a log line on a server. If somebody sent a CV and
    is waiting to hear back, this is what tells the desk it happened.

    Never raises, for the same reason as its neighbours: the mail has already
    been dealt with and failing here would only take the batch down.
    """
    from app.api import websocket as ws  # imported here: routes import both ways

    who = from_addr or "an unknown sender"
    label = filename or "a CV"
    title = "Candidate rejected — other nationality" if country else "Candidate rejected"
    message = f"{label} from {who} was not ingested: {reason}"
    notified = 0

    try:
        repo = repo or NotificationRepository()
        users = users or UserRepository()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not open the notification store (%s); pushing only", exc)
        repo = None

    if repo is not None:
        try:
            for admin_id in _admin_ids(users):
                repo.record(
                    admin_id,
                    type=store.CANDIDATE_REJECTED,
                    title=title,
                    message=message,
                )
                notified += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not store the rejection notification: %s", exc)

    try:
        ws.publish_event(
            {
                "type": store.CANDIDATE_REJECTED,
                "title": title,
                "message": message,
                "filename": filename,
                "from_addr": from_addr,
                "country": country,
                "reason": reason,
            }
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not push the rejection event: %s", exc)

    return notified


def notify_sla_breaches(
    alerts: List[dict],
    threshold_hours: float,
    *,
    repo: NotificationRepository | None = None,
    users: UserRepository | None = None,
) -> int:
    """Record and push the sweep's newly-breached profiles.

    The sweep runs on a beat timer, which means it fires at whatever o'clock the
    interval lands on — frequently with no admin watching. The modal is the
    urgent path; this is the one that is still there in the morning.

    Only *newly* breaching profiles reach here (see app.tasks.sla_checker), so
    the feed does not re-report the same overdue profile every few minutes.
    """
    from app.api import websocket as ws

    notified = 0
    if alerts:
        try:
            repo = repo or NotificationRepository()
            users = users or UserRepository()
            count = len(alerts)
            hours = f"{threshold_hours:g}"
            if count == 1:
                alert = alerts[0]
                who = alert.get("assigned_staff_name") or "a staff member"
                what = alert.get("full_name") or alert.get("candidate_name") or "a profile"
                message = f"{what} has sat with {who} for over {hours} hours."
            else:
                message = f"{count} profiles have gone over the {hours}-hour review window."

            for admin_id in _admin_ids(users):
                repo.record(
                    admin_id,
                    type=store.SLA_ALERT,
                    title=f"{hours}-hour SLA breach",
                    message=message,
                    candidate_id=alerts[0].get("candidate_id") if count == 1 else None,
                    candidate_name=alerts[0].get("full_name") if count == 1 else None,
                )
                notified += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not store the SLA notification: %s", exc)

    try:
        ws.publish_event(ws.sla_alert_event(alerts, threshold_hours))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not push the SLA event: %s", exc)

    # ---- and the admins' phones ------------------------------------------ #
    # One call for the whole sweep, not one per profile. `alerts` is already
    # only what newly breached, so this does not re-announce anything.
    #
    # Outside `notified` for the same reason as the allocation relay: that
    # counts feed rows written, and an admin who was messaged as well was not
    # notified twice.
    relay_sla_breach(alerts, threshold_hours)

    return notified
