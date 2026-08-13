"""Allocation against a real MongoDB implementation, not hand-written fakes.

The unit tests in test_assignment.py stub the repository, so they prove the
*choice* is right but say nothing about whether the queries behind it are. This
module runs the production `CandidateRepository` and `UserRepository` against
mongomock, so the aggregation in `workload_counts`, the projection in
`list_for_rebalance` and the writes in `assign` all execute for real.

That distinction matters: every failure this file is designed to catch —
a `$match` that silently drops documents, a projection missing a field the
balancer reads — is invisible to a fake that returns whatever the test wants.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from app.assignment.balancer import (
    allocate_unassigned,
    assign_candidate,
    rebalance_all,
    redistribute_from_staff,
    rehome_orphans,
)
from app.db.repository import CandidateRepository
from app.db.users import STAFF_ROLE, UserRepository


@pytest.fixture
def db(monkeypatch):
    """An in-memory database wired into every module that reaches for one."""
    client = mongomock.MongoClient(tz_aware=True)
    database = client["resume_ats_test"]

    from app.db import mongo, users as users_module

    monkeypatch.setattr(mongo, "get_db", lambda: database)
    monkeypatch.setattr(mongo, "get_candidates_collection", lambda: database["candidates"])
    monkeypatch.setattr(users_module, "get_users_collection", lambda: database["users"])
    return database


@pytest.fixture
def users(db):
    return UserRepository(collection=db["users"])


@pytest.fixture
def repo(db):
    return CandidateRepository(collection=db["candidates"])


def make_staff(users, name, email=None):
    return users.create(
        email=email or f"{name.lower().replace(' ', '')}@x.com",
        password="pw-123456",
        name=name,
        role=STAFF_ROLE,
    )


def insert_candidate(db, candidate_id, staff_id=None, staff_name=None, created_offset=0,
                     viewed=False, status="pending"):
    """A candidate document shaped the way the pipeline writes one."""
    db["candidates"].insert_one({
        "_id": candidate_id,
        "profile": {"full_name": candidate_id.upper(), "skills": ["python"]},
        "status": "ingested",
        "assigned_staff_id": staff_id,
        "assigned_staff_name": staff_name,
        "assigned_at": datetime.now(timezone.utc) if staff_id else None,
        "viewed_at": datetime.now(timezone.utc) if viewed else None,
        "evaluation_status": status,
        "created_at": datetime.now(timezone.utc) + timedelta(seconds=created_offset),
    })


def counts_by_name(db, users):
    """`{staff_name: assigned_count}` read straight back out of the database."""
    out = {}
    for member in users.list_staff():
        out[member.name] = db["candidates"].count_documents({"assigned_staff_id": member.id})
    return out


# --------------------------------------------------------------------------- #
#  The workload query itself
# --------------------------------------------------------------------------- #
def test_workload_counts_sees_every_assigned_candidate(db, users, repo):
    """If this aggregation under-reports, every allocation decision is wrong."""
    alice = make_staff(users, "Alice")
    for i in range(3):
        insert_candidate(db, f"c{i}", alice.id, alice.name)

    assert repo.workload_counts()[alice.id]["assigned"] == 3


def test_unassigned_candidates_are_not_counted_against_anyone(db, users, repo):
    make_staff(users, "Alice")
    insert_candidate(db, "orphan")            # assigned_staff_id = None
    assert repo.workload_counts() == {}


def test_a_brand_new_account_reports_zero_rather_than_being_absent(db, users, repo):
    """A staff member with no candidates has no row in the aggregation. If the
    balancer took that to mean "unknown" instead of "zero", the one person
    every incoming résumé should go to would be the one it never considered."""
    alice = make_staff(users, "Alice")
    for i in range(4):
        insert_candidate(db, f"c{i}", alice.id, alice.name)
    bob = make_staff(users, "Bob")

    assert bob.id not in repo.workload_counts()

    result = assign_candidate("new-1", None, repo=repo, users=users)
    assert result.staff_id == bob.id


# --------------------------------------------------------------------------- #
#  The specified ingestion sequence, end to end
# --------------------------------------------------------------------------- #
def test_specified_ingestion_sequence_equalises_at_five(db, users, repo):
    """Staff 1 = 5, Staff 2 = 4, Staff 3 = 3, then three résumés arrive.

    Run through the real repository: each `assign_candidate` re-reads the counts
    with the same aggregation the API uses, and writes with the same update.
    """
    s1 = make_staff(users, "Staff 1")
    s2 = make_staff(users, "Staff 2")
    s3 = make_staff(users, "Staff 3")

    for i in range(5):
        insert_candidate(db, f"s1-{i}", s1.id, s1.name)
    for i in range(4):
        insert_candidate(db, f"s2-{i}", s2.id, s2.name)
    for i in range(3):
        insert_candidate(db, f"s3-{i}", s3.id, s3.name)

    insert_candidate(db, "new-1")
    insert_candidate(db, "new-2")
    insert_candidate(db, "new-3")

    first = assign_candidate("new-1", None, repo=repo, users=users)
    assert first.staff_id == s3.id                       # lowest: 3
    assert counts_by_name(db, users) == {"Staff 1": 5, "Staff 2": 4, "Staff 3": 4}

    second = assign_candidate("new-2", None, repo=repo, users=users)
    assert second.staff_id in {s2.id, s3.id}             # both on 4
    assert sorted(counts_by_name(db, users).values()) == [4, 5, 5]

    third = assign_candidate("new-3", None, repo=repo, users=users)
    assert third.staff_id != second.staff_id
    assert counts_by_name(db, users) == {"Staff 1": 5, "Staff 2": 5, "Staff 3": 5}


# --------------------------------------------------------------------------- #
#  Rebalancing on staff creation — the reported symptom
# --------------------------------------------------------------------------- #
def test_creating_a_second_account_levels_an_existing_pile(db, users, repo):
    """The reported case: one staff member holding everything, a new hire at 0.

    Twelve unviewed profiles across two people must come out 6/6. Anything that
    leaves it at 12/0 — or 10/2 — is the bug.
    """
    tharun = make_staff(users, "tharun")
    for i in range(12):
        insert_candidate(db, f"c{i}", tharun.id, tharun.name, created_offset=i)

    evaluator = make_staff(users, "Staff Evaluator")
    result = rebalance_all(repo=repo, users=users)

    assert result["status"] == "ok"
    assert counts_by_name(db, users) == {"tharun": 6, "Staff Evaluator": 6}
    assert result["moved"] == 6
    assert db["candidates"].count_documents({"assigned_staff_id": evaluator.id}) == 6


def test_the_exact_reported_state_levels_to_six_six(db, users, repo):
    """The workload matrix as reported: 0/10 for tharun, 0/2 for Staff Evaluator.

    Staff Evaluator showed "1 unviewed · 2 pending", so one of its two has been
    opened and is pinned there. Eleven movable profiles plus that one pinned
    come out 6/6 — the pinned one counts toward its owner, so the levelling
    works around it rather than ignoring it.
    """
    tharun = make_staff(users, "tharun", "tharun@gmail.com")
    evaluator = make_staff(users, "Staff Evaluator", "staff@gmail.com")

    for i in range(10):
        insert_candidate(db, f"t-{i}", tharun.id, tharun.name, created_offset=i)
    insert_candidate(db, "se-seen", evaluator.id, evaluator.name, viewed=True, created_offset=20)
    insert_candidate(db, "se-open", evaluator.id, evaluator.name, created_offset=21)

    assert counts_by_name(db, users) == {"tharun": 10, "Staff Evaluator": 2}

    result = rebalance_all(repo=repo, users=users)

    assert result["locked"] == 1
    assert counts_by_name(db, users) == {"tharun": 6, "Staff Evaluator": 6}
    # The opened profile never moved, and kept the timestamp that locked it.
    assert db["candidates"].find_one({"_id": "se-seen"})["assigned_staff_id"] == evaluator.id
    assert db["candidates"].find_one({"_id": "se-seen"})["viewed_at"] is not None


def test_rebalance_leaves_reviewed_work_where_it_is(db, users, repo):
    """Only untouched profiles move; opened or judged ones stay put."""
    tharun = make_staff(users, "tharun")
    for i in range(4):
        insert_candidate(db, f"open-{i}", tharun.id, tharun.name, created_offset=i)
    insert_candidate(db, "seen-1", tharun.id, tharun.name, viewed=True, created_offset=10)
    insert_candidate(db, "judged-1", tharun.id, tharun.name, status="shortlisted", created_offset=11)

    make_staff(users, "Staff Evaluator")
    result = rebalance_all(repo=repo, users=users)

    assert result["locked"] == 2
    # The two locked profiles never left, and keep their evaluation state.
    assert db["candidates"].find_one({"_id": "seen-1"})["assigned_staff_id"] == tharun.id
    assert db["candidates"].find_one({"_id": "judged-1"})["evaluation_status"] == "shortlisted"
    # Six profiles, two pinned to tharun, so the four movable ones split 1/3.
    assert counts_by_name(db, users) == {"tharun": 3, "Staff Evaluator": 3}


def test_rebalance_does_not_wipe_evaluations_it_moves_around(db, users, repo):
    """A rebalance must never clear a verdict — that is the whole lock rule."""
    tharun = make_staff(users, "tharun")
    insert_candidate(db, "judged", tharun.id, tharun.name, viewed=True, status="interviewing")
    for i in range(5):
        insert_candidate(db, f"open-{i}", tharun.id, tharun.name, created_offset=i + 1)

    make_staff(users, "Staff Evaluator")
    rebalance_all(repo=repo, users=users)

    judged = db["candidates"].find_one({"_id": "judged"})
    assert judged["evaluation_status"] == "interviewing"
    assert judged["viewed_at"] is not None
    assert judged["assigned_staff_id"] == tharun.id


def test_rebalance_picks_up_candidates_nobody_owns(db, users, repo):
    """Profiles ingested while no staff existed are invisible to every
    dashboard until a rebalance re-homes them."""
    for i in range(4):
        insert_candidate(db, f"orphan-{i}", created_offset=i)

    alice = make_staff(users, "Alice")
    bob = make_staff(users, "Bob")
    rebalance_all(repo=repo, users=users)

    assert counts_by_name(db, users) == {"Alice": 2, "Bob": 2}
    assert db["candidates"].count_documents({"assigned_staff_id": None}) == 0


def test_a_deactivated_account_keeps_its_work_but_receives_none(db, users, repo):
    alice = make_staff(users, "Alice")
    bob = make_staff(users, "Bob")
    for i in range(4):
        insert_candidate(db, f"c{i}", alice.id, alice.name, created_offset=i)

    users.update_staff(bob.id, active=False)
    rebalance_all(repo=repo, users=users)

    assert db["candidates"].count_documents({"assigned_staff_id": bob.id}) == 0
    assert db["candidates"].count_documents({"assigned_staff_id": alice.id}) == 4


# --------------------------------------------------------------------------- #
#  Allocating what nobody owns, without disturbing what someone does
# --------------------------------------------------------------------------- #
def test_allocate_unassigned_places_only_the_unowned(db, users, repo):
    """The background pass: give owners to orphans, touch nothing else.

    This runs unprompted, so its blast radius is the whole point. A profile that
    already belongs to someone must come out of it belonging to the same person
    even when the roster is wildly uneven — otherwise reading a dashboard could
    move work out from under whoever was doing it.
    """
    alice = make_staff(users, "Alice")
    bob = make_staff(users, "Bob")
    for i in range(6):
        insert_candidate(db, f"a-{i}", alice.id, alice.name, created_offset=i)
    for i in range(2):
        insert_candidate(db, f"free-{i}", created_offset=10 + i)

    result = allocate_unassigned(repo=repo, users=users)

    assert result["allocated"] == 2
    # Both newcomers went to Bob, who was on zero; Alice's six never moved.
    assert counts_by_name(db, users) == {"Alice": 6, "Bob": 2}
    assert db["candidates"].count_documents({"assigned_staff_id": None}) == 0


def test_allocate_unassigned_does_not_relevel_a_lopsided_roster(db, users, repo):
    """12/0 with nothing unowned is left at 12/0. Levelling is the admin's call."""
    alice = make_staff(users, "Alice")
    make_staff(users, "Bob")
    for i in range(12):
        insert_candidate(db, f"a-{i}", alice.id, alice.name, created_offset=i)

    result = allocate_unassigned(repo=repo, users=users)

    assert result["allocated"] == 0
    assert counts_by_name(db, users) == {"Alice": 12, "Bob": 0}


# --------------------------------------------------------------------------- #
#  Deleting an account: the queue splits in two
# --------------------------------------------------------------------------- #
def test_deleting_an_account_reallocates_unviewed_and_orphans_the_rest(db, users, repo):
    """The two halves need opposite treatment, and this is why.

    Unviewed profiles carry no work, so they move immediately and land with
    whoever is holding fewest. Reviewed ones carry a verdict that reassignment
    would erase, so they are left pointing at the account that is gone — visible
    to nobody, counted as orphaned, and re-homed only when an admin says so.
    """
    leaver = make_staff(users, "Leaver")
    alice = make_staff(users, "Alice")

    for i in range(4):
        insert_candidate(db, f"open-{i}", leaver.id, leaver.name, created_offset=i)
    insert_candidate(db, "seen", leaver.id, leaver.name, viewed=True, created_offset=10)
    insert_candidate(db, "judged", leaver.id, leaver.name, viewed=True,
                     status="shortlisted", created_offset=11)

    users.delete_staff(leaver.id)
    result = redistribute_from_staff(leaver.id, repo=repo, users=users)

    assert result == {"status": "ok", "reallocated": 4, "orphaned": 2}
    assert db["candidates"].count_documents({"assigned_staff_id": alice.id}) == 4
    # The reviewed pair stayed put, verdict and first-open timestamp intact.
    judged = db["candidates"].find_one({"_id": "judged"})
    assert judged["assigned_staff_id"] == leaver.id
    assert judged["evaluation_status"] == "shortlisted"
    assert db["candidates"].find_one({"_id": "seen"})["viewed_at"] is not None
    assert repo.orphaned_count([m.id for m in users.list_staff()]) == 2


def test_deleting_the_last_account_orphans_everything(db, users, repo):
    """Nowhere to put the queue is not a failure — it is what the banner reports."""
    leaver = make_staff(users, "Leaver")
    for i in range(3):
        insert_candidate(db, f"c-{i}", leaver.id, leaver.name, created_offset=i)

    users.delete_staff(leaver.id)
    result = redistribute_from_staff(leaver.id, repo=repo, users=users)

    assert result["status"] == "no_staff"
    assert result["orphaned"] == 3
    assert repo.orphaned_count([]) == 3


# --------------------------------------------------------------------------- #
#  Re-homing orphans, which a rebalance cannot do
# --------------------------------------------------------------------------- #
def test_rehoming_orphans_keeps_the_verdict_a_rebalance_would_have_kept_stranded(db, users, repo):
    """The gap this closes.

    Every orphan is reviewed — that is *why* it was orphaned rather than
    reallocated — and `rebalance_all` refuses to move reviewed profiles. So a
    rebalance leaves the orphan count exactly where it was, and the console's
    warning would never clear. Re-homing moves them and keeps the evaluation.
    """
    alice = make_staff(users, "Alice")
    insert_candidate(db, "stranded", "deleted-account", "Ghost",
                     viewed=True, status="interviewing")

    # A rebalance sees a reviewed profile and declines, as designed.
    rebalance_all(repo=repo, users=users)
    assert repo.orphaned_count([alice.id]) == 1

    result = rehome_orphans(repo=repo, users=users)

    assert result == {"status": "ok", "rehomed": 1, "remaining": 0}
    stranded = db["candidates"].find_one({"_id": "stranded"})
    assert stranded["assigned_staff_id"] == alice.id
    assert stranded["assigned_staff_name"] == "Alice"
    assert stranded["evaluation_status"] == "interviewing"
    assert stranded["viewed_at"] is not None
    assert repo.orphaned_count([alice.id]) == 0


def test_rehoming_spreads_orphans_over_the_least_loaded(db, users, repo):
    alice = make_staff(users, "Alice")
    bob = make_staff(users, "Bob")
    for i in range(3):
        insert_candidate(db, f"a-{i}", alice.id, alice.name, created_offset=i)
    for i in range(4):
        insert_candidate(db, f"lost-{i}", "deleted-account", "Ghost",
                         viewed=True, status="rejected", created_offset=10 + i)

    result = rehome_orphans(repo=repo, users=users)

    assert result["rehomed"] == 4
    # Bob was on zero, so he absorbs the backlog until the two are level — and
    # the seventh profile then breaks a 3-3 tie on staff *id*, which is a random
    # uuid. Asserting which of the two ends up with four would be asserting the
    # uuid ordering, so the invariant is the split, not who is on which side.
    counts = counts_by_name(db, users)
    assert sorted(counts.values()) == [3, 4]
    assert sum(counts.values()) == 7
    assert repo.orphaned_count([alice.id, bob.id]) == 0


def test_rehoming_without_an_active_account_reports_rather_than_drops(db, users, repo):
    insert_candidate(db, "stranded", "deleted-account", "Ghost", viewed=True, status="rejected")

    result = rehome_orphans(repo=repo, users=users)

    assert result["status"] == "error"
    assert result["remaining"] == 1
    assert db["candidates"].find_one({"_id": "stranded"})["assigned_staff_id"] == "deleted-account"


# --------------------------------------------------------------------------- #
#  The SLA clock, run as a real query
# --------------------------------------------------------------------------- #
def sla_row(db, cid, **fields):
    """A candidate document carrying only the timestamps the sweep reads."""
    doc = {
        "_id": cid,
        "profile": {"full_name": cid.upper()},
        "assigned_staff_id": "s1",
        "assigned_staff_name": "Staff One",
        "viewed_at": None,
        "evaluation_status": "pending",
    }
    doc.update(fields)
    db["candidates"].insert_one(doc)


def test_the_sla_clock_falls_back_from_assigned_at_to_arrival(db, repo):
    """The regression this fallback exists for.

    The query used to match on `assigned_at` directly. A record allocated by an
    older build has no such field, so `{"assigned_at": {"$lte": cutoff}}` never
    matched it — and the sweep reported the profiles that had been waiting
    *longest* as perfectly fine, because it could not see when their clock had
    started. Each of the three sources has to select the row and produce the
    same overdue figure.
    """
    from app.tasks.sla_checker import _row_to_alert

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=48)

    sla_row(db, "by-assigned", assigned_at=old, created_at=old)
    sla_row(db, "by-ingested", ingested_at=old, created_at=old)   # no assigned_at
    sla_row(db, "by-created", created_at=old)                      # neither

    rows = repo.find_sla_breaches(now - timedelta(hours=24))

    assert sorted(r["_id"] for r in rows) == ["by-assigned", "by-created", "by-ingested"]
    # The query decides *whether*; `_row_to_alert` decides *by how much*. A row
    # selected by one and measured at zero by the other reads as "0h overdue".
    assert [round(_row_to_alert(r, now)["hours_overdue"]) for r in rows] == [48, 48, 48]


def test_a_profile_inside_the_window_or_already_dealt_with_is_not_a_breach(db, repo):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=48)

    sla_row(db, "fresh", assigned_at=now, created_at=old)
    sla_row(db, "judged", assigned_at=old, created_at=old, viewed_at=old,
            evaluation_status="shortlisted")
    # Nobody owns it, so there is no one to chase and no alert to address.
    sla_row(db, "unowned", assigned_staff_id=None, created_at=old)

    assert repo.find_sla_breaches(now - timedelta(hours=24)) == []


# --------------------------------------------------------------------------- #
#  Staff isolation, read back out of the database
# --------------------------------------------------------------------------- #
def test_each_staff_member_lists_only_their_own(db, users, repo):
    alice = make_staff(users, "Alice")
    bob = make_staff(users, "Bob")
    insert_candidate(db, "a1", alice.id, alice.name)
    insert_candidate(db, "b1", bob.id, bob.name)

    assert [r["id"] for r in repo.list_summaries(staff_id=alice.id)] == ["a1"]
    assert repo.count(staff_id=alice.id) == 1
    assert repo.count() == 2
