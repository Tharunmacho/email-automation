"""Candidate → staff allocation by country desk, then workload.

Two entry points, both synchronous because both callers are — the ingestion
pipeline (in a Celery worker) and the admin API.

`assign_candidate` places one new profile. `rebalance_all` re-levels the whole
collection after the staff roster changes.

Singapore and Malaysia candidates go to their dedicated desk. Every other
destination goes to the rest of the roster. Within the responsible desk, the
profile goes to whichever active staff member currently holds the fewest.

Ties break on staff id rather than arbitrarily, so the same roster and the same
counts always produce the same choice. Without that a rebalance would not be
reproducible and nothing about it could be asserted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from app.db.repository import CandidateRepository
from app.db.users import User, UserRepository
from app.logging_config import get_logger

log = get_logger(__name__)


# Existing CRM accounts assigned to the Singapore/Malaysia desk. Email is used
# instead of the generated staff id so the rule survives account recreation and
# database migration.
SINGAPORE_MALAYSIA_DESK_EMAILS = frozenset({
    "sreya.adira@gmail.com",
    "bakkimamal.adira@gmail.com",
    "dsharmila.adira@gmail.com",
    "hajirabegum.adira@gmail.com",
    "jemalisahi.adira@gmail.com",
    "noorul.adira@gmail.com",
})
SINGAPORE_MALAYSIA_COUNTRIES = frozenset({"singapore", "malaysia"})


def _destination_country(profile: object) -> str:
    """Read the placement country from a model, dict, or rebalance row."""
    if isinstance(profile, dict):
        nested = profile.get("profile")
        if isinstance(nested, dict):
            profile = nested
        value = profile.get("destination_country") if isinstance(profile, dict) else None
    else:
        value = getattr(profile, "destination_country", None)
    return str(value or "").strip().casefold()


def is_staff_eligible(member: object, profile: object) -> bool:
    """Whether ``member`` belongs to the desk responsible for this candidate."""
    is_sg_my_candidate = _destination_country(profile) in SINGAPORE_MALAYSIA_COUNTRIES
    email = member.get("email", "") if isinstance(member, dict) else getattr(member, "email", "")
    is_sg_my_staff = str(email).strip().casefold() in SINGAPORE_MALAYSIA_DESK_EMAILS
    return is_sg_my_candidate == is_sg_my_staff


def _eligible_staff(staff: Sequence[User], profile: object) -> List[User]:
    return [member for member in staff if is_staff_eligible(member, profile)]


@dataclass(frozen=True)
class AssignmentResult:
    """The outcome of placing one candidate."""

    candidate_id: str
    staff_id: Optional[str] = None
    staff_name: Optional[str] = None
    # "workload" — placed with the least-loaded staff member
    # "no_staff"  — there is no active staff member to assign to
    reason: str = "no_staff"

    @property
    def assigned(self) -> bool:
        return self.staff_id is not None

    def to_public(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "assigned_staff_id": self.staff_id,
            "assigned_staff_name": self.staff_name,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
#  Placement
# --------------------------------------------------------------------------- #
def _least_loaded(staff: Sequence[User], workloads: Dict[str, int]) -> User:
    """The staff member holding the fewest profiles, ties broken by id."""
    return min(staff, key=lambda member: (workloads.get(member.id, 0), member.id))


def _current_workloads(staff: Sequence[User], repo: CandidateRepository) -> Dict[str, int]:
    """`{staff_id: assigned_count}` for the active roster, zero-filled.

    Zero-filled on purpose: a staff member created a moment ago has no
    candidates and so no row in the aggregation, and omitting them would hide
    the one person every incoming profile should be going to.
    """
    workloads = {member.id: 0 for member in staff}
    for staff_id, counts in repo.workload_counts().items():
        if staff_id in workloads:
            workloads[staff_id] = counts.get("assigned", 0)
    return workloads


def assign_candidate(
    candidate_id: str,
    profile: object = None,
    *,
    repo: Optional[CandidateRepository] = None,
    users: Optional[UserRepository] = None,
) -> AssignmentResult:
    """Place one candidate with the least-loaded staff member.

    Called once per ingested résumé. The destination selects a desk and the
    workload comparison selects one person inside that desk.

    Returns a `no_staff` result rather than raising when the roster is empty: a
    missing staff account must not fail an ingestion that has already extracted
    and stored the profile.
    """
    repo = repo or CandidateRepository()
    users = users or UserRepository()

    staff = _eligible_staff(users.list_assignable_staff(), profile)
    if not staff:
        log.warning(
            "Candidate %s left unassigned: no active staff member for destination %s",
            candidate_id,
            _destination_country(profile) or "unspecified",
        )
        return AssignmentResult(candidate_id=candidate_id)

    workloads = _current_workloads(staff, repo)
    chosen = _least_loaded(staff, workloads)

    repo.assign(candidate_id, chosen.id, chosen.name)
    log.info(
        "Assigned candidate %s to %s (%s), who was holding %d",
        candidate_id, chosen.name, chosen.id, workloads.get(chosen.id, 0),
    )
    return AssignmentResult(
        candidate_id=candidate_id,
        staff_id=chosen.id,
        staff_name=chosen.name,
        reason="workload",
    )


# --------------------------------------------------------------------------- #
#  Rebalancing
# --------------------------------------------------------------------------- #
def _is_locked(row: dict) -> bool:
    """True when moving this profile would destroy work someone already did.

    A profile that has been opened or judged stays where it is. Reassignment
    clears `viewed_at` and the verdict (see `CandidateRepository.assign`), so
    re-levelling a fully-evaluated collection would otherwise wipe the whole
    team's output and reset every SLA clock to now.

    Locked profiles still count toward their owner's load, so the levelling
    works around them instead of pretending they are not there.
    """
    if not row.get("assigned_staff_id"):
        return False
    status = row.get("evaluation_status") or "pending"
    return bool(row.get("viewed_at")) or status != "pending"


def rebalance_all(
    *,
    repo: Optional[CandidateRepository] = None,
    users: Optional[UserRepository] = None,
) -> dict:
    """Level the collection across the active roster. Admin-triggered only.

    Creating a staff account no longer runs this. It used to, on the reasoning
    that a new hire starts at zero while everyone else is deep — but that made
    adding a colleague silently reshuffle a queue somebody was part-way through,
    and the admin who pressed "create" was not asking for that. Re-levelling is
    now a deliberate act: this function runs from the Rebalance control and from
    nowhere else.

    Only *unviewed* profiles move. Anything opened or judged is pinned to its
    current owner by `_is_locked`, still counted against their load so the
    levelling works around it.

    The movable ones are dealt out one at a time, oldest first, each to whoever
    is holding the fewest at that moment. That is the same minimum-workload rule
    applied repeatedly, and it converges on an even split without needing to
    compute the target size up front. A profile whose least-loaded destination
    is the staff member already holding it is left alone, so a rebalance that
    changes nothing costs no writes and disturbs no SLA clocks.
    """
    repo = repo or CandidateRepository()
    users = users or UserRepository()

    staff = users.list_assignable_staff()
    if not staff:
        return {
            "status": "error",
            "detail": "No active staff members to rebalance across.",
            "moved": 0, "locked": 0, "unchanged": 0, "staff_counts": {},
        }

    rows = repo.list_for_rebalance()
    workloads = {member.id: 0 for member in staff}

    movable: List[dict] = []
    locked_count = 0
    for row in rows:
        if _is_locked(row):
            locked_count += 1
            owner = row.get("assigned_staff_id")
            # Work owned by a deactivated or deleted account keeps its
            # evaluation but no longer occupies a slot in the pool being
            # levelled.
            if owner in workloads:
                workloads[owner] += 1
        else:
            movable.append(row)

    moved = 0
    unchanged = 0
    for row in movable:
        eligible = _eligible_staff(staff, row)
        if not eligible:
            unchanged += 1
            continue
        chosen = _least_loaded(eligible, workloads)
        workloads[chosen.id] += 1

        if row.get("assigned_staff_id") == chosen.id:
            unchanged += 1
            continue
        repo.assign(row["_id"], chosen.id, chosen.name)
        moved += 1

    log.info(
        "Rebalance across %d active staff: [%d profiles moved, %d locked/reviewed left in place] "
        "(%d already with the right owner, %d total)",
        len(staff), moved, locked_count, unchanged, len(rows),
    )
    return {
        "status": "ok",
        "moved": moved,
        "unchanged": unchanged,
        "locked": locked_count,
        "total": len(rows),
        "staff_counts": {
            member.id: {"name": member.name, "assigned": workloads.get(member.id, 0)}
            for member in staff
        },
    }


# --------------------------------------------------------------------------- #
#  Targeted allocation
# --------------------------------------------------------------------------- #
def _deal(
    rows: Sequence[dict],
    staff: Sequence[User],
    workloads: Dict[str, int],
    place,
) -> int:
    """Deal `rows` out one at a time to whoever is holding the fewest.

    `place(candidate_id, staff_id, staff_name)` does the write, which is the
    only thing that differs between the callers: fresh work is placed with
    `assign` (verdict cleared), already-started work with `reassign` (verdict
    kept). Returns how many were actually written.
    """
    placed = 0
    for row in rows:
        eligible = _eligible_staff(staff, row)
        if not eligible:
            continue
        chosen = _least_loaded(eligible, workloads)
        workloads[chosen.id] += 1
        if row.get("assigned_staff_id") == chosen.id:
            continue
        place(row["_id"], chosen.id, chosen.name)
        placed += 1
    return placed


def allocate_unassigned(
    *,
    repo: Optional[CandidateRepository] = None,
    users: Optional[UserRepository] = None,
) -> dict:
    """Give an owner to every profile that has none. Touches nothing else.

    This is what runs on its own, without anybody asking — a résumé ingested
    while the roster was empty has been waiting ever since, and it is invisible
    to every staff dashboard until someone owns it.

    Deliberately *not* `rebalance_all`: that would also shuffle profiles which
    already have an owner, and a background refresh must never move work out
    from under the person doing it.
    """
    repo = repo or CandidateRepository()
    users = users or UserRepository()

    staff = users.list_assignable_staff()
    if not staff:
        return {"status": "error", "detail": "No active staff members.", "allocated": 0}

    rows = repo.list_unassigned()
    if not rows:
        return {"status": "ok", "allocated": 0}

    workloads = _current_workloads(staff, repo)
    allocated = _deal(rows, staff, workloads, repo.assign)

    log.info("Allocated %d previously unowned profile(s) across %d staff", allocated, len(staff))
    return {"status": "ok", "allocated": allocated}


def redistribute_from_staff(
    staff_id: str,
    *,
    repo: Optional[CandidateRepository] = None,
    users: Optional[UserRepository] = None,
) -> dict:
    """Deal with one staff member's queue after their account is removed.

    The two halves of that queue need opposite treatment, and conflating them is
    what a plain rebalance got wrong here:

      * **Unviewed** — nobody has read these, so nothing is lost by moving them.
        They go to whoever is holding the fewest, immediately and without asking.
      * **Viewed or judged** — someone did the work and the verdict is on the
        record. Moving one with `assign` would erase it, so these are left
        pointing at the id that no longer exists. That makes them *orphaned*:
        counted by `CandidateRepository.orphaned_count`, reported on the admin
        console as profiles nobody can see, and re-homed by `rehome_orphans`
        once an admin decides who should get them.

    Called after the account row is deleted, so `staff_id` is already gone from
    the roster and the remaining staff are exactly the active pool.
    """
    repo = repo or CandidateRepository()
    users = users or UserRepository()

    rows = repo.list_owned_by(staff_id)
    if not rows:
        return {"status": "ok", "reallocated": 0, "orphaned": 0}

    movable = [row for row in rows if not _is_locked(row)]
    orphaned = len(rows) - len(movable)

    staff = users.list_assignable_staff()
    if not staff:
        # Nowhere to put them. Everything the account held is orphaned, which is
        # exactly what the console's banner is for.
        log.warning(
            "Staff %s deleted with no active staff remaining: %d profile(s) orphaned",
            staff_id, len(rows),
        )
        return {"status": "no_staff", "reallocated": 0, "orphaned": len(rows)}

    workloads = _current_workloads(staff, repo)
    reallocated = _deal(movable, staff, workloads, repo.assign)
    # A country desk may temporarily have no active member. Those profiles are
    # still orphaned; reporting them as reallocated would hide work from the
    # admin console even though no write occurred.
    orphaned += len(movable) - reallocated

    log.info(
        "Staff %s deleted: [%d unviewed profile(s) reallocated, %d reviewed left orphaned]",
        staff_id, reallocated, orphaned,
    )
    return {"status": "ok", "reallocated": reallocated, "orphaned": orphaned}


def rehome_orphans(
    *,
    repo: Optional[CandidateRepository] = None,
    users: Optional[UserRepository] = None,
) -> dict:
    """Hand every profile stranded on a deleted account to somebody who exists.

    Uses `reassign`, not `assign`: an orphan is orphaned precisely *because* it
    had been reviewed, and the evaluation on it is the reason it was not moved
    automatically when the account went. Re-homing preserves the verdict, the
    score, the notes and `viewed_at` — the profile changes hands, not state.

    Clears the console banner by construction: the banner counts orphans, and
    after this there are none left to count.
    """
    repo = repo or CandidateRepository()
    users = users or UserRepository()

    everyone = users.list_staff(include_inactive=True)
    rows = repo.list_orphaned([member.id for member in everyone])
    if not rows:
        return {"status": "ok", "rehomed": 0, "remaining": 0}

    staff = users.list_assignable_staff()
    if not staff:
        return {
            "status": "error",
            "detail": "No active staff members to re-home these profiles to.",
            "rehomed": 0,
            "remaining": len(rows),
        }

    workloads = _current_workloads(staff, repo)
    rehomed = _deal(rows, staff, workloads, repo.reassign)
    remaining = len(rows) - rehomed

    log.info("Re-homed %d orphaned profile(s) across %d active staff", rehomed, len(staff))
    return {"status": "ok", "rehomed": rehomed, "remaining": remaining}
