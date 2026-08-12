"""Ingestion must allocate. A stored candidate nobody owns is a lost candidate.

The reported symptom was that résumés arrived and sat unassigned until an admin
placed them by hand. These tests pin the wiring itself: that a successful
ingestion calls the balancer, that the staff member is told, and — most
importantly — that neither of those failing can take the ingestion down with it.
"""
from __future__ import annotations

import pytest

from app.core.models import CandidateProfile, EmailMessage, ExtractedDocument
from app.ingestion.pipeline import IngestionPipeline
from tests.test_page_classifier import RESUME_PAGE
from tests.test_pipeline_redelivery import FakeLedger, FakeRepo, FakeStorage


class StubParser:
    def parse_file(self, data: bytes, filename: str):
        profile = CandidateProfile(
            is_resume=True, confidence=0.9, full_name="Rajesh Kumar",
            email="rajesh@example.com", phone="+91 98765 43210",
        )
        return profile, ExtractedDocument(
            text=data.decode("utf-8", "replace"), method="plain", char_count=len(data),
        )


def body_email(message_id: str = "msg-1") -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        from_addr="rajesh@example.com",
        from_name="Rajesh Kumar",
        subject="Application",
        body_text=RESUME_PAGE,
        attachments=[],
    )


@pytest.fixture
def pipeline():
    return IngestionPipeline(
        repository=FakeRepo(), storage=FakeStorage(), parser=StubParser(), ledger=FakeLedger(),
    )


class Placed:
    """Records the allocation the pipeline asked for."""

    def __init__(self, staff_id="staff-1", staff_name="Sarah Chen"):
        self.calls = []
        self._staff_id = staff_id
        self._staff_name = staff_name

    def __call__(self, candidate_id, profile=None, **kwargs):
        self.calls.append(candidate_id)
        from app.assignment.balancer import AssignmentResult

        return AssignmentResult(
            candidate_id=candidate_id,
            staff_id=self._staff_id,
            staff_name=self._staff_name,
            reason="workload",
        )


def test_an_ingested_resume_is_allocated_without_anyone_asking(pipeline, monkeypatch):
    """The whole point of the automation: no human step between arrival and owner."""
    placed = Placed()
    monkeypatch.setattr("app.assignment.assign_candidate", placed)

    result = pipeline.process_email(body_email())

    assert result.status == "processed"
    assert len(placed.calls) == 1, "ingestion completed without allocating the candidate"


def test_the_assigned_staff_member_is_notified(pipeline, monkeypatch):
    published = []
    monkeypatch.setattr("app.assignment.assign_candidate", Placed())
    monkeypatch.setattr("app.api.websocket.publish_event", lambda e: published.append(e) or True)

    pipeline.process_email(body_email())

    by_type = {event["type"]: event for event in published}
    event = by_type["candidate_assigned"]
    assert event["channel"] == "staff"
    assert event["staff_id"] == "staff-1"
    assert "Rajesh Kumar" in event["message"]


def test_the_admins_are_told_the_same_moment(pipeline, monkeypatch):
    """An admin who starts a sync used to watch it produce nothing visible.

    The only event ingestion emitted was addressed to the staff channel, so the
    person who pressed the button — the one most likely to be watching — was
    told nothing at all.
    """
    published = []
    monkeypatch.setattr("app.assignment.assign_candidate", Placed())
    monkeypatch.setattr("app.api.websocket.publish_event", lambda e: published.append(e) or True)

    pipeline.process_email(body_email())

    by_type = {event["type"]: event for event in published}
    assert set(by_type) == {"candidate_assigned", "candidate_ingested"}
    admin_event = by_type["candidate_ingested"]
    assert admin_event["channel"] == "admin"
    assert "Rajesh Kumar" in admin_event["message"]
    # Named, so the admin can see the allocation landed where it should.
    assert "Sarah Chen" in admin_event["message"]


def test_a_notification_store_that_is_down_does_not_stop_the_push(pipeline, monkeypatch):
    """Storage and delivery fail independently, and neither may fail ingestion.

    This test runs with no reachable Mongo (see tests/conftest.py), which is
    exactly the condition being asserted: the toast still goes out, and the
    résumé is still ingested.
    """
    published = []
    monkeypatch.setattr("app.assignment.assign_candidate", Placed())
    monkeypatch.setattr("app.api.websocket.publish_event", lambda e: published.append(e) or True)

    result = pipeline.process_email(body_email())

    assert result.status == "processed"
    assert len(published) == 2


def test_allocation_can_be_turned_off(pipeline, monkeypatch):
    from app.config import settings

    placed = Placed()
    monkeypatch.setattr("app.assignment.assign_candidate", placed)
    monkeypatch.setattr(settings, "auto_assign_enabled", False)

    result = pipeline.process_email(body_email())

    assert result.status == "processed"
    assert placed.calls == []


# --------------------------------------------------------------------------- #
#  Allocation must never cost us the ingestion
# --------------------------------------------------------------------------- #
def test_an_empty_roster_does_not_fail_the_ingestion(pipeline, monkeypatch):
    """The résumé is already extracted and stored by this point. Failing here
    would retry the whole thing — another OCR call and another LLM call — for a
    problem no retry can fix."""
    from app.assignment.balancer import AssignmentResult

    monkeypatch.setattr(
        "app.assignment.assign_candidate",
        lambda cid, profile=None, **kw: AssignmentResult(candidate_id=cid),
    )

    result = pipeline.process_email(body_email())

    assert result.status == "processed"
    assert result.attachments[0].status == "ingested"


def test_a_crashing_balancer_does_not_fail_the_ingestion(pipeline, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("Atlas went away mid-allocation")

    monkeypatch.setattr("app.assignment.assign_candidate", explode)

    result = pipeline.process_email(body_email())

    assert result.status == "processed"
    assert result.attachments[0].status == "ingested"


def test_an_unreachable_event_bus_does_not_fail_the_ingestion(pipeline, monkeypatch):
    """A toast nobody receives is a cosmetic loss; a re-ingested résumé is not."""
    def explode(_event):
        raise RuntimeError("Redis refused the connection")

    monkeypatch.setattr("app.assignment.assign_candidate", Placed())
    monkeypatch.setattr("app.api.websocket.publish_event", explode)

    result = pipeline.process_email(body_email())

    assert result.status == "processed"
    assert result.attachments[0].status == "ingested"
