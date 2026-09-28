"""Leave cover: a colleague does the leave-taker's work for the day.

The flow, end to end:

1. A staff member asks for paid or unpaid leave and names a colleague as cover.
2. The colleague accepts or declines. A decline lets the requester name
   someone else; the manager still approves or rejects the leave itself.
3. On the leave day, once the leave is approved *and* the cover has accepted,
   the requester's unfinished candidates (nothing judged yet) are lent to the
   cover (`start_due_covers`).
4. When the day is over, `settle_finished_covers` ends it: whatever the cover
   completed stays completed with them; whatever is still pending goes back to
   the staff member who was on leave.

Both steps are idempotent and run from the beat schedule, and step 3 is also
tried straight away when the last condition falls into place, so a leave
approved on the morning of the day does not wait for the next tick.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.attendance.engine import AttendancePolicy, local_day
from app.attendance.repository import AttendanceRepository
from app.logging_config import get_logger

log = get_logger(__name__)


def _today(policy: AttendancePolicy | None = None, now: datetime | None = None):
    policy = policy or AttendancePolicy()
    return local_day(now or datetime.now(timezone.utc), policy.timezone_name)


def _notify(user_id: str | None, title: str, message: str) -> None:
    if not user_id:
        return
    try:
        from app.db.notifications import ATTENDANCE_REQUEST, NotificationRepository

        NotificationRepository().record(user_id, type=ATTENDANCE_REQUEST, title=title, message=message)
    except Exception as exc:  # noqa: BLE001 — the handover stands even if the alert fails
        log.warning("Leave-cover notification to %s failed: %s", user_id, exc)


def start_due_covers(
    attendance: AttendanceRepository | None = None,
    candidates=None,
    users=None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Lend today's leave-takers' pending work to their accepted covers."""
    attendance = attendance or AttendanceRepository()
    if candidates is None:
        from app.db.repository import CandidateRepository

        candidates = CandidateRepository()
    if users is None:
        from app.api.routes import users as users_repo

        users = users_repo

    started = []
    for permission in attendance.covers_due(_today(now=now)):
        cover = users.get(permission["cover_employee_id"])
        owner = users.get(permission["employee_id"])
        if cover is None or not getattr(cover, "active", True):
            attendance.claim_cover_handover(permission["id"], {
                "status": "skipped", "reason": "Cover account is no longer active",
                "started_at": datetime.now(timezone.utc),
            })
            continue
        if not attendance.claim_cover_handover(permission["id"], {
            "status": "active", "started_at": datetime.now(timezone.utc),
            "cover_employee_id": cover.id, "handed_over": 0,
        }):
            continue
        owner_name = getattr(owner, "name", None) or permission["employee_id"]
        moved = candidates.hand_over_for_cover(
            from_staff_id=permission["employee_id"],
            to_staff_id=cover.id,
            to_staff_name=cover.name,
            permission_id=permission["id"],
            remarks=f"Covering for {owner_name} on leave ({permission['attendance_date']}).",
        )
        attendance.update_cover_handover(permission["id"], {"handed_over": len(moved)})
        _notify(
            cover.id, "Leave cover started",
            f"{len(moved)} candidate(s) from {owner_name} are with you today. "
            "Anything not completed goes back to them tomorrow.",
        )
        log.info("Leave cover %s: %d candidate(s) lent to %s", permission["id"], len(moved), cover.id)
        started.append({"permission_id": permission["id"], "handed_over": len(moved)})
    return started


def settle_finished_covers(
    attendance: AttendanceRepository | None = None,
    candidates=None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """End covers whose day is over: completed stays, pending returns."""
    attendance = attendance or AttendanceRepository()
    if candidates is None:
        from app.db.repository import CandidateRepository

        candidates = CandidateRepository()

    settled = []
    for permission in attendance.covers_to_settle(_today(now=now)):
        result = candidates.settle_leave_cover(permission["id"])
        attendance.update_cover_handover(permission["id"], {
            "status": "settled",
            "settled_at": datetime.now(timezone.utc),
            "completed": result["completed"],
            "returned": result["returned"],
            "released": result["released"],
        })
        if result["returned"]:
            _notify(
                permission["employee_id"], "Leave cover ended",
                f"{result['completed']} candidate(s) were completed by your cover; "
                f"{result['returned']} pending candidate(s) are back in your queue.",
            )
        else:
            _notify(
                permission["employee_id"], "Leave cover ended",
                f"Your cover completed all {result['completed']} candidate(s) handed over.",
            )
        log.info("Leave cover %s settled: %s", permission["id"], result)
        settled.append({"permission_id": permission["id"], **result})
    return settled


def run_cover_sweep(**kwargs) -> dict:
    """Settle yesterday's covers first, then start today's."""
    settled = settle_finished_covers(
        kwargs.get("attendance"), kwargs.get("candidates"), now=kwargs.get("now"),
    )
    started = start_due_covers(
        kwargs.get("attendance"), kwargs.get("candidates"), kwargs.get("users"), now=kwargs.get("now"),
    )
    return {"settled": settled, "started": started}
