"""Candidate → staff allocation by workload alone.

Two entry points, both synchronous because both callers are — the ingestion
pipeline (in a Celery worker) and the admin API.

`assign_candidate` places one new profile. `rebalance_all` re-levels the whole
collection after the staff roster changes.

The rule is the same in both, and it is deliberately the whole rule: the
profile goes to whichever active staff member is currently holding the fewest.
Nothing about the candidate is inspected — not their skills, not their job
title. Distribution is a question about the team's capacity, not about the
résumé, and answering it purely on counts is what makes the outcome
predictable: any two people's queues differ by at most one, always.

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

    Called once per ingested résumé. `profile` is accepted and ignored — the
    pipeline has it to hand and the parameter keeps the call site stable, but
    allocation does not look at the candidate.

    Returns a `no_staff` result rather than raising when the roster is empty: a
    missing staff account must not fail an ingestion that has already extracted
    and stored the profile.
    """
    repo = repo or CandidateRepository()
    users = users or UserRepository()

    staff = users.list_assignable_staff()
    if not staff:
        log.warning("Candidate %s left unassigned: no active staff members", candidate_id)
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
    """Level the collection across the active roster.

    Runs when a staff account is created — a new hire starts at zero while
    everyone else is deep — and on demand from the admin console.

    Untouched profiles are dealt out one at a time, oldest first, each to
    whoever is holding the fewest at that moment. That is the same
    minimum-workload rule applied repeatedly, and it converges on an even split
    without needing to compute the target size up front. A profile whose
    least-loaded destination is the staff member already holding it is left
    alone, so a rebalance that changes nothing costs no writes and disturbs no
    SLA clocks.
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
        chosen = _least_loaded(staff, workloads)
        workloads[chosen.id] += 1

        if row.get("assigned_staff_id") == chosen.id:
            unchanged += 1
            continue
        repo.assign(row["_id"], chosen.id, chosen.name)
        moved += 1

    log.info(
        "Rebalanced across %d staff: %d moved, %d already correct, %d locked by prior work",
        len(staff), moved, unchanged, locked_count,
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
