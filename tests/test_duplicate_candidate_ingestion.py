"""A duplicate resolved at insert must not allocate or auto-reply a second time.

One application delivered to two of the four polled mailboxes arrived as two
messages and became two candidates — and, because everything after the insert
acts on a *new* candidate, two allocations to two recruiters and two auto-replies
to one applicant.

The (4) `find_by_email_or_phone` check cannot stop that by itself: it is a read
followed by an insert, and `ingestion_max_workers` runs both messages at once,
so both threads pass the lookup before either has written anything. The unique
index on `email_key` settles the race and `CandidateRepository.insert` resolves
it to the candidate that already owns the address — but resolving it is only
half the fix. The pipeline has to notice it happened and stop.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.ingestion.pipeline import IngestionPipeline
from tests.test_ingestion_allocation import Placed, StubParser, body_email
from tests.test_pipeline_redelivery import FakeLedger, FakeRepo, FakeStorage


class CollidingRepo(FakeRepo):
    """A repo whose unique index fires: `insert` returns somebody else's id.

    Faithful to the real one — `CandidateRepository.insert` catches
    `DuplicateKeyError` and returns the existing candidate rather than raising,
    so the caller gets an id back that is not the one it minted.
    """

    def __init__(self, existing_id: str = "already-here"):
        super().__init__()
        self.existing_id = existing_id
        self.attempted: list[str] = []

    def insert(self, record):
        self.attempted.append(record.id)
        return self.existing_id


class Replier:
    """A Gmail stand-in that records every reply it is asked to send."""

    def __init__(self):
        self.sent = []

    def send_reply(self, **kwargs):
        self.sent.append(kwargs.get("to_addr"))


@pytest.fixture
def colliding():
    repo = CollidingRepo()
    return repo, IngestionPipeline(
        repository=repo, storage=FakeStorage(), parser=StubParser(), ledger=FakeLedger(),
    )


def test_the_second_copy_is_not_allocated_again(colliding, monkeypatch):
    """Two recruiters owning one applicant is the reported symptom."""
    repo, pipeline = colliding
    placed = Placed()
    monkeypatch.setattr("app.assignment.assign_candidate", placed)

    pipeline.process_email(body_email("second-mailbox-copy"))

    assert repo.attempted, "the insert was never attempted; the test proves nothing"
    assert placed.calls == [], (
        "the duplicate was allocated again — one applicant, two owners"
    )


def test_the_candidate_is_not_replied_to_twice(colliding, monkeypatch):
    """An auto-reply is irreversible and goes to a real person."""
    _repo, pipeline = colliding
    monkeypatch.setattr(settings, "auto_reply_enabled", True)
    monkeypatch.setattr("app.assignment.assign_candidate", Placed())
    replier = Replier()

    pipeline.process_email(body_email("second-mailbox-copy"), gmail=replier)

    assert replier.sent == [], f"a second auto-reply went to {replier.sent}"


def test_it_reports_the_candidate_it_folded_into(colliding, monkeypatch):
    """The existing candidate's id, so the ledger and the operator can follow it."""
    repo, pipeline = colliding
    monkeypatch.setattr("app.assignment.assign_candidate", Placed())

    result = pipeline.process_email(body_email("second-mailbox-copy"))

    attachment = result.attachments[0]
    assert attachment.status == "duplicate"
    assert attachment.candidate_id == repo.existing_id
    assert result.ingested_ids == [], "a folded duplicate must not count as ingested"


def test_a_genuinely_new_candidate_is_still_allocated_and_replied_to(monkeypatch):
    """The guard must not fire on the normal path."""
    monkeypatch.setattr(settings, "auto_reply_enabled", True)
    placed = Placed()
    monkeypatch.setattr("app.assignment.assign_candidate", placed)
    pipeline = IngestionPipeline(
        repository=FakeRepo(), storage=FakeStorage(), parser=StubParser(),
        ledger=FakeLedger(),
    )
    replier = Replier()

    result = pipeline.process_email(body_email("first-arrival"), gmail=replier)

    assert result.status == "processed"
    assert len(placed.calls) == 1, "a new candidate went unallocated"
    assert replier.sent, "a new candidate got no auto-reply"
