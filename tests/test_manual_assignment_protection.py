"""Manual assignments must never be undone by an automatic pass.

The reported bug: staff reassigned candidates by hand and the system later put
them back with the previous owner. The cause was the post-poll step running a
full `rebalance_all`. These tests run the production repository against
mongomock and pin down every automatic path that can write an owner:

  * the post-poll step in `IngestionRunner.run_once`
  * `allocate_unassigned`, `rebalance_all`
  * ingestion / intake allocation of a new or re-sent candidate
  * the compare-and-set guards that stop a stale snapshot overwriting a
    manual change made in the meantime

and confirm the deliberate paths (new-candidate allocation, the admin
auto-assign control, staff deletion, orphan re-homing) still work.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from app.assignment import balancer
from app.assignment.balancer import (
    _is_pinned,
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
    client = mongomock.MongoClient(tz_aware=True)
    database = client["manual_assignment_test"]

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


def make_staff(users, name):
    return users.create(
        email=f"{name.lower()}@x.com", password="pw-123456", name=name, role=STAFF_ROLE,
    )


def insert(db, cid, owner=None, *, offset=0, manual=None, history=None, viewed=False):
    doc = {
        "_id": cid,
        "profile": {"full_name": cid.upper(), "destination_country": None},
        "status": "ingested",
        "assigned_staff_id": owner.id if owner else None,
        "assigned_staff_name": owner.name if owner else None,
        "viewed_at": datetime.now(timezone.utc) if viewed else None,
        "evaluation_status": "pending",
        "created_at": datetime.now(timezone.utc) + timedelta(seconds=offset),
    }
    if manual is not None:
        doc["manually_assigned"] = manual
    if history is not None:
        doc["assignment_history"] = history
    db["candidates"].insert_one(doc)


def owner_of(db, cid):
    return db["candidates"].find_one({"_id": cid})["assigned_staff_id"]


# --------------------------------------------------------------------------- #
#  Mail polling
# --------------------------------------------------------------------------- #
class _PipelineWithRepo:
    def __init__(self, repo):
        self.repo = repo


def test_mail_poll_only_fills_unowned_candidates_and_never_rebalances(db, users, repo, monkeypatch):
    from app.ingestion.runner import IngestionRunner

    a, b = make_staff(users, "Alice"), make_staff(users, "Bob")
    # Lopsided on purpose: a rebalance would move some of Alice's to Bob.
    for i in range(4):
        insert(db, f"auto-{i}", a, offset=i, manual=False)
    insert(db, "hand-placed", a, offset=5, manual=True)
    insert(db, "new", offset=6)

    def forbidden(**_kwargs):
        raise AssertionError("mail polling must never rebalance")

    monkeypatch.setattr(balancer, "rebalance_all", forbidden)
    monkeypatch.setattr("app.assignment.rebalance_all", forbidden)

    IngestionRunner(clients=[], pipeline=_PipelineWithRepo(repo)).run_once()

    assert owner_of(db, "new") == b.id  # the unowned one got the least-loaded owner
    assert owner_of(db, "hand-placed") == a.id
    assert all(owner_of(db, f"auto-{i}") == a.id for i in range(4))


# --------------------------------------------------------------------------- #
#  Rebalance and pinning
# --------------------------------------------------------------------------- #
def test_rebalance_skips_flagged_and_legacy_manual_moves(db, users, repo):
    a, b = make_staff(users, "Alice"), make_staff(users, "Bob")
    insert(db, "flagged", a, manual=True)
    insert(db, "legacy", a, history=[{"reason": "manual_reassignment", "to_staff_id": a.id}])
    insert(db, "legacy-route", a, history=[{"type": "reassignment", "to_staff_id": a.id}])
    for i in range(3):
        insert(db, f"auto-{i}", a, offset=10 + i, manual=False)

    result = rebalance_all(repo=repo, users=users)

    assert {owner_of(db, c) for c in ("flagged", "legacy", "legacy-route")} == {a.id}
    assert result["locked"] == 3
    assert b.id in {owner_of(db, f"auto-{i}") for i in range(3)}


def test_stale_manual_history_does_not_pin_an_automatic_owner(db, users):
    """Moved by hand to Alice, then (by the old bug) silently put back with Bob
    with no history entry. The entry names Alice, so Bob's ownership is automatic."""
    a, b = make_staff(users, "Alice"), make_staff(users, "Bob")
    insert(db, "reverted", b, history=[{"reason": "manual_reassignment", "to_staff_id": a.id}])
    assert _is_pinned(db["candidates"].find_one({"_id": "reverted"})) is False


def test_flag_false_overrides_old_history(db, users):
    a = make_staff(users, "Alice")
    insert(db, "c", a, manual=False,
           history=[{"reason": "manual_reassignment", "to_staff_id": a.id}])
    assert _is_pinned(db["candidates"].find_one({"_id": "c"})) is False


def test_rebalance_does_not_overwrite_a_manual_move_made_after_its_snapshot(db, users, repo, monkeypatch):
    """The race: rebalance reads, a person reassigns, rebalance writes."""
    a, b = make_staff(users, "Alice"), make_staff(users, "Bob")
    c = make_staff(users, "Carol")
    for i in range(4):
        insert(db, f"auto-{i}", a, offset=i, manual=False)
    real_list = repo.list_for_rebalance

    def snapshot_then_manual_move():
        rows = real_list()
        for i in range(4):
            repo.assign(f"auto-{i}", c.id, c.name, manual=True)
        return rows

    monkeypatch.setattr(repo, "list_for_rebalance", snapshot_then_manual_move)
    rebalance_all(repo=repo, users=users)

    assert all(owner_of(db, f"auto-{i}") == c.id for i in range(4))


def test_allocate_unassigned_does_not_overwrite_a_manual_assignment_in_flight(db, users, repo, monkeypatch):
    a, _b = make_staff(users, "Alice"), make_staff(users, "Bob")
    insert(db, "new")
    real_list = repo.list_unassigned

    def snapshot_then_manual_assign():
        rows = real_list()
        repo.assign("new", a.id, a.name, manual=True)
        return rows

    monkeypatch.setattr(repo, "list_unassigned", snapshot_then_manual_assign)
    result = allocate_unassigned(repo=repo, users=users)

    assert owner_of(db, "new") == a.id
    assert db["candidates"].find_one({"_id": "new"})["manually_assigned"] is True
    assert result["allocated"] == 0


def test_rebalance_does_not_wipe_a_profile_opened_after_its_snapshot(db, users, repo, monkeypatch):
    a, _b = make_staff(users, "Alice"), make_staff(users, "Bob")
    for i in range(4):
        insert(db, f"auto-{i}", a, offset=i, manual=False)
    real_list = repo.list_for_rebalance

    def snapshot_then_open():
        rows = real_list()
        for i in range(4):
            repo.mark_viewed(f"auto-{i}")
        return rows

    monkeypatch.setattr(repo, "list_for_rebalance", snapshot_then_open)
    rebalance_all(repo=repo, users=users)

    for i in range(4):
        doc = db["candidates"].find_one({"_id": f"auto-{i}"})
        assert doc["assigned_staff_id"] == a.id
        assert doc["viewed_at"] is not None


# --------------------------------------------------------------------------- #
#  New candidates and re-ingestion
# --------------------------------------------------------------------------- #
def test_new_candidate_is_auto_assigned(db, users, repo):
    a, b = make_staff(users, "Alice"), make_staff(users, "Bob")
    insert(db, "held", a, manual=False)
    insert(db, "fresh")

    result = assign_candidate("fresh", None, repo=repo, users=users, only_if_unassigned=True)

    assert result.assigned and result.staff_id == b.id
    assert db["candidates"].find_one({"_id": "fresh"})["manually_assigned"] is False


def test_automatic_assignment_never_replaces_an_existing_owner(db, users, repo):
    """What ingestion/intake use. A re-sent résumé for an owned candidate must
    leave the owner alone even if allocation is asked for again."""
    a, _b = make_staff(users, "Alice"), make_staff(users, "Bob")
    for i in range(3):
        insert(db, f"load-{i}", a, manual=False)
    insert(db, "owned", a, manual=True)

    result = assign_candidate("owned", None, repo=repo, users=users, only_if_unassigned=True)

    assert not result.assigned and result.reason == "already_assigned"
    assert owner_of(db, "owned") == a.id


def test_admin_auto_assign_still_overrides_on_purpose(db, users, repo):
    a, b = make_staff(users, "Alice"), make_staff(users, "Bob")
    insert(db, "load", a, manual=False)
    insert(db, "owned", a, manual=True)

    result = assign_candidate("owned", None, repo=repo, users=users)

    assert result.assigned and result.staff_id == b.id


def test_whatsapp_restatement_only_allocates_an_unowned_candidate():
    import inspect

    from app.services import candidate_intake

    assert "not existing.assigned_staff_id" in inspect.getsource(candidate_intake._refresh_existing)


def test_email_reingestion_returns_before_allocation():
    """The pipeline allocates only after a fresh insert; a duplicate returns first."""
    import inspect

    from app.ingestion import pipeline

    source = inspect.getsource(pipeline.IngestionPipeline)
    assert source.index("no second allocation or auto-reply") < source.index(
        "if not self._allocate(candidate_id, profile)"
    )


# --------------------------------------------------------------------------- #
#  Staff deletion and orphans still move work
# --------------------------------------------------------------------------- #
def test_deleting_staff_moves_their_unviewed_work_including_hand_placed(db, users, repo):
    a, b = make_staff(users, "Alice"), make_staff(users, "Bob")
    insert(db, "auto", a, manual=False)
    insert(db, "hand", a, manual=True)
    insert(db, "read", a, manual=False, viewed=True)
    users.delete_staff(a.id)

    result = redistribute_from_staff(a.id, repo=repo, users=users)

    assert owner_of(db, "auto") == b.id
    assert owner_of(db, "hand") == b.id
    assert owner_of(db, "read") == a.id  # orphaned, verdict kept
    assert result == {"status": "ok", "reallocated": 2, "orphaned": 1}

    rehomed = rehome_orphans(repo=repo, users=users)
    assert rehomed["rehomed"] == 1
    assert owner_of(db, "read") == b.id
