"""The load balancer: lowest candidate count wins, and nothing else.

Everything here runs against in-memory fakes. The balancer takes its repository
and user store as arguments precisely so this is possible — see conftest.py for
why a test must never reach the real cluster.
"""
from __future__ import annotations

import pytest

from app.assignment.balancer import assign_candidate, rebalance_all
from app.core.models import utcnow
from app.db.users import User


# --------------------------------------------------------------------------- #
#  Fakes
# --------------------------------------------------------------------------- #
class FakeUsers:
    def __init__(self, staff):
        self._staff = list(staff)

    def list_assignable_staff(self):
        return [m for m in self._staff if m.active]


class FakeRepo:
    """Just enough of CandidateRepository for the balancer to run.

    `rows` are the raw projections `list_for_rebalance` returns, so the shape
    the balancer reads here is the shape Mongo actually gives it.
    """

    def __init__(self, workloads=None, rows=None):
        self._workloads = dict(workloads or {})
        self._rows = list(rows or [])
        self.assignments = []

    def workload_counts(self):
        return {sid: {"assigned": n, "evaluated": 0, "unviewed": n} for sid, n in self._workloads.items()}

    def list_for_rebalance(self):
        return self._rows

    def assign(self, candidate_id, staff_id, staff_name):
        self.assignments.append((candidate_id, staff_id))
        self._workloads[staff_id] = self._workloads.get(staff_id, 0) + 1
        for row in self._rows:
            if row["_id"] == candidate_id:
                row["assigned_staff_id"] = staff_id
        return True


def staff(sid, name, active=True):
    return User(id=sid, email=f"{sid}@x.com", name=name, role="staff", active=active)


def profile(skills=(), designation=None):
    return {"skills": list(skills), "current_designation": designation}


# --------------------------------------------------------------------------- #
#  Step-by-step minimum workload
# --------------------------------------------------------------------------- #
def test_candidate_goes_to_the_least_loaded_staff_member():
    """The worked example: A=10, B=7, C=6 sends the next profile to C."""
    users = FakeUsers([staff("a", "A"), staff("b", "B"), staff("c", "C")])
    repo = FakeRepo({"a": 10, "b": 7, "c": 6})

    result = assign_candidate("cand-1", profile(["python"]), repo=repo, users=users)

    assert result.staff_id == "c"
    assert result.reason == "workload"


def test_repeated_assignment_levels_the_roster_one_at_a_time():
    """A=10, B=7, C=6 → C, then B or C, and so on until the counts converge.

    This is the whole "step-by-step" rule: each placement re-reads the counts,
    so the sequence self-corrects rather than dealing a fixed round-robin.
    """
    users = FakeUsers([staff("a", "A"), staff("b", "B"), staff("c", "C")])
    repo = FakeRepo({"a": 10, "b": 7, "c": 6})

    picks = []
    for i in range(6):
        picks.append(assign_candidate(f"cand-{i}", profile(), repo=repo, users=users).staff_id)

    assert picks[0] == "c"          # 6 is the minimum
    assert picks[1] in {"b", "c"}   # both sit at 7
    # Nothing may be handed to the busiest member while anyone trails them.
    assert "a" not in picks
    # The two trailing members converge on the leader instead of overshooting.
    assert repo._workloads == {"a": 10, "b": 10, "c": 9}
    assert max(repo._workloads.values()) - min(repo._workloads.values()) <= 1


def test_ties_break_deterministically():
    """Equal counts must produce the same choice every run, or a rebalance is
    not reproducible and nothing about it can be asserted."""
    users = FakeUsers([staff("z", "Z"), staff("a", "A")])
    first = assign_candidate("c1", profile(), repo=FakeRepo({"a": 3, "z": 3}), users=users)
    second = assign_candidate("c1", profile(), repo=FakeRepo({"a": 3, "z": 3}), users=users)
    assert first.staff_id == second.staff_id == "a"


# --------------------------------------------------------------------------- #
#  Allocation ignores the candidate entirely
# --------------------------------------------------------------------------- #
def test_the_candidates_skills_do_not_influence_the_choice():
    """Distribution is a question about the team, not about the résumé.

    Two profiles with nothing in common must go the same way when the counts
    are the same — anything else means something other than workload is being
    consulted.
    """
    users = FakeUsers([staff("a", "A"), staff("b", "B")])

    welder = assign_candidate("c1", profile(["welding"], "Welder"), repo=FakeRepo({"a": 2, "b": 9}), users=users)
    developer = assign_candidate("c2", profile(["python"], "Engineer"), repo=FakeRepo({"a": 2, "b": 9}), users=users)

    assert welder.staff_id == developer.staff_id == "a"


def test_a_profile_the_parser_returned_nothing_for_is_still_placed():
    """An unassigned profile is one nobody is accountable for, and the SLA
    scanner cannot report it — so an empty profile must not block allocation."""
    users = FakeUsers([staff("a", "A")])

    for empty in (None, {}, profile()):
        result = assign_candidate("c1", empty, repo=FakeRepo({"a": 4}), users=users)
        assert result.staff_id == "a"


# --------------------------------------------------------------------------- #
#  Degenerate rosters
# --------------------------------------------------------------------------- #
def test_empty_roster_reports_rather_than_raises():
    """Ingestion has already stored the profile by this point; a missing roster
    must not turn that into a failed, retried extraction."""
    result = assign_candidate("c1", profile(), repo=FakeRepo(), users=FakeUsers([]))
    assert result.assigned is False
    assert result.reason == "no_staff"


def test_deactivated_staff_receive_nothing():
    users = FakeUsers([staff("a", "A"), staff("gone", "Gone", active=False)])
    repo = FakeRepo({"a": 50, "gone": 0})
    assert assign_candidate("c1", profile(), repo=repo, users=users).staff_id == "a"


# --------------------------------------------------------------------------- #
#  Rebalancing
# --------------------------------------------------------------------------- #
def row(cid, staff_id=None, viewed=False, status="pending", skills=()):
    return {
        "_id": cid,
        "assigned_staff_id": staff_id,
        "viewed_at": utcnow() if viewed else None,
        "evaluation_status": status,
        "profile": {"skills": list(skills)},
    }


def test_rebalance_levels_an_uneven_split():
    users = FakeUsers([staff("a", "A"), staff("b", "B"), staff("c", "C")])
    repo = FakeRepo(rows=[row(f"c{i}", "a") for i in range(9)])

    result = rebalance_all(repo=repo, users=users)

    assert result["status"] == "ok"
    counts = {sid: info["assigned"] for sid, info in result["staff_counts"].items()}
    assert counts == {"a": 3, "b": 3, "c": 3}


def test_rebalance_does_not_move_work_already_done():
    """Reassignment clears viewed_at and the verdict, so re-levelling an
    evaluated collection would wipe the team's output. Those rows stay put and
    still count toward their owner's load."""
    users = FakeUsers([staff("a", "A"), staff("b", "B")])
    repo = FakeRepo(rows=[
        row("done-1", "a", viewed=True, status="shortlisted"),
        row("done-2", "a", viewed=True, status="rejected"),
        row("fresh-1", "a"),
        row("fresh-2", "a"),
    ])

    result = rebalance_all(repo=repo, users=users)

    assert result["locked"] == 2
    moved_ids = {cid for cid, _ in repo.assignments}
    assert "done-1" not in moved_ids and "done-2" not in moved_ids
    # A already holds two locked profiles, so both movable ones go to B.
    assert result["staff_counts"]["b"]["assigned"] == 2


def test_rebalance_writes_nothing_when_already_level():
    """A no-op rebalance must not restart every SLA clock in the collection."""
    users = FakeUsers([staff("a", "A"), staff("b", "B")])
    repo = FakeRepo(rows=[row("c1", "a"), row("c2", "b")])

    result = rebalance_all(repo=repo, users=users)

    assert repo.assignments == []
    assert result["moved"] == 0
    assert result["unchanged"] == 2


def test_rebalance_rehomes_orphans_from_a_deleted_account():
    """Profiles pointing at an account that no longer exists are invisible to
    every staff dashboard until something re-homes them."""
    users = FakeUsers([staff("a", "A")])
    repo = FakeRepo(rows=[row("c1", "deleted-staff"), row("c2", "deleted-staff")])

    result = rebalance_all(repo=repo, users=users)

    assert result["moved"] == 2
    assert {sid for _, sid in repo.assignments} == {"a"}


def test_rebalance_without_staff_reports_an_error():
    result = rebalance_all(repo=FakeRepo(rows=[row("c1")]), users=FakeUsers([]))
    assert result["status"] == "error"
    assert result["moved"] == 0


# --------------------------------------------------------------------------- #
#  The end-to-end story the platform is specified around
# --------------------------------------------------------------------------- #
def test_spec_walkthrough_five_four_three_equalises_at_five():
    """The specified worked example, step by step.

    Roster starts at 5 / 4 / 3. Three candidates arrive one at a time and the
    roster has to finish level at 5 / 5 / 5 — each placement re-reads the counts,
    so the third candidate lands on whoever the first two left behind.
    """
    users = FakeUsers([staff("s1", "Staff 1"), staff("s2", "Staff 2"), staff("s3", "Staff 3")])
    repo = FakeRepo({"s1": 5, "s2": 4, "s3": 3})

    first = assign_candidate("cand-1", profile(), repo=repo, users=users)
    assert first.staff_id == "s3"                      # lowest count: 3
    assert repo._workloads == {"s1": 5, "s2": 4, "s3": 4}

    second = assign_candidate("cand-2", profile(), repo=repo, users=users)
    assert second.staff_id in {"s2", "s3"}             # both now sit at 4
    assert sorted(repo._workloads.values()) == [4, 5, 5]

    third = assign_candidate("cand-3", profile(), repo=repo, users=users)
    assert third.staff_id != second.staff_id           # the one still on 4
    assert repo._workloads == {"s1": 5, "s2": 5, "s3": 5}


def test_new_hire_takes_a_fair_share_of_an_existing_backlog():
    """One staff member holds all 5 résumés; a second account is created.

    The new hire starts at zero while the first is deep, so creating the
    account has to level the pile rather than wait for the next intake to
    trickle candidates across. 5 across 2 people is 3/2 — an even split to
    within one, which is as equal as an odd number gets.
    """
    users = FakeUsers([staff("a", "Anita"), staff("b", "Bala")])
    repo = FakeRepo(rows=[row(f"cand-{i}", "a") for i in range(5)])

    result = rebalance_all(repo=repo, users=users)

    counts = {sid: info["assigned"] for sid, info in result["staff_counts"].items()}
    assert sorted(counts.values()) == [2, 3]
    assert sum(counts.values()) == 5
    assert result["moved"] == 2


def test_third_hire_levels_the_same_backlog_again():
    """Adding a third account re-levels to 2/2/1 rather than leaving them at 3/2."""
    users = FakeUsers([staff("a", "Anita"), staff("b", "Bala"), staff("c", "Chitra")])
    repo = FakeRepo(rows=[row("c0", "a"), row("c1", "a"), row("c2", "a"), row("c3", "b"), row("c4", "b")])

    result = rebalance_all(repo=repo, users=users)

    counts = sorted(info["assigned"] for info in result["staff_counts"].values())
    assert counts == [1, 2, 2]


def test_a_new_hire_cannot_be_handed_work_someone_has_already_judged():
    """The reason a rebalance is safe to run on a live system.

    Anita has evaluated three of the five. Those carry her verdict and her
    timestamps; moving them would clear both. Only the untouched two are
    available to the new hire, so Bala takes them and the evaluated three stay.
    """
    users = FakeUsers([staff("a", "Anita"), staff("b", "Bala")])
    repo = FakeRepo(rows=[
        row("done-1", "a", viewed=True, status="shortlisted"),
        row("done-2", "a", viewed=True, status="rejected"),
        row("done-3", "a", viewed=True, status="interviewing"),
        row("open-1", "a"),
        row("open-2", "a"),
    ])

    result = rebalance_all(repo=repo, users=users)

    assert result["locked"] == 3
    assert {cid for cid, _ in repo.assignments} == {"open-1", "open-2"}
    assert result["staff_counts"]["b"]["assigned"] == 2


def test_a_new_hire_absorbs_intake_until_they_catch_up():
    """A staff member created at zero takes every arrival until level.

    This is the rule that makes creating an account self-correcting without any
    special case for it: the minimum-workload check already sends everything to
    whoever is furthest behind, and stops the moment they are not.
    """
    users = FakeUsers([staff("old", "Existing"), staff("new", "New Hire")])
    repo = FakeRepo({"old": 4, "new": 0})

    picks = [
        assign_candidate(f"cand-{i}", profile(), repo=repo, users=users).staff_id
        for i in range(4)
    ]

    # Every one of the first four goes to the new hire, who is behind throughout.
    assert picks == ["new", "new", "new", "new"]
    assert repo._workloads == {"old": 4, "new": 4}

    # Caught up: the next one is no longer theirs by default.
    assign_candidate("cand-4", profile(), repo=repo, users=users)
    assert sorted(repo._workloads.values()) == [4, 5]


def test_ingesting_one_at_a_time_keeps_the_team_level():
    """The steady state: résumés arrive one by one and nobody pulls ahead.

    This is the automation path — no rebalance involved, just the per-candidate
    rule applied on ingestion — and it has to converge on its own.
    """
    users = FakeUsers([staff("a", "Anita"), staff("b", "Bala"), staff("c", "Chitra")])
    repo = FakeRepo({"a": 0, "b": 0, "c": 0})

    for i in range(9):
        assign_candidate(f"cand-{i}", profile(), repo=repo, users=users)

    assert repo._workloads == {"a": 3, "b": 3, "c": 3}
