"""A résumé pasted into the email body must ingest like any other.

Plenty of candidates — especially from a phone — type or paste the CV straight
into the message and attach nothing. The mail then has no attachment at all,
which used to end the pipeline at stage 1 with `no resume-type attachment`.

The body is handed on as a synthetic `email_body.txt`, so nothing downstream
needs a special case: it is hashed, deduplicated, stored and parsed on exactly
the path a PDF takes. These tests pin that it really does go all the way
through, and that a covering note does not.
"""
from __future__ import annotations

import pytest

from app.core.models import CandidateProfile, EmailMessage, ExtractedDocument
from app.ingestion.pipeline import IngestionPipeline
from tests.test_page_classifier import RESUME_PAGE
from tests.test_pipeline_redelivery import FakeLedger, FakeRepo, FakeStorage


class RecordingParser:
    """Stands in for the LLM, and remembers what it was asked to read."""

    def __init__(self):
        self.seen: list[tuple[bytes, str]] = []

    def parse_file(self, data: bytes, filename: str):
        self.seen.append((data, filename))
        profile = CandidateProfile(
            is_resume=True, confidence=0.9, full_name="Rajesh Kumar",
            email="rajesh@example.com", phone="+91 98765 43210",
        )
        return profile, ExtractedDocument(
            text=data.decode("utf-8", "replace"), method="plain", char_count=len(data),
        )


def body_email(message_id: str, body: str) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        from_addr="rajesh@example.com",
        from_name="Rajesh Kumar",
        subject="Fwd:",            # deliberately signal-free
        body_text=body,
        attachments=[],            # the whole point: nothing attached
    )


@pytest.fixture
def parts():
    repo, ledger, storage, parser = FakeRepo(), FakeLedger(), FakeStorage(), RecordingParser()
    pipeline = IngestionPipeline(
        repository=repo, storage=storage, parser=parser, ledger=ledger,
    )
    return pipeline, repo, storage, parser


def test_a_pasted_resume_becomes_a_candidate(parts):
    pipeline, repo, _storage, parser = parts

    result = pipeline.process_email(body_email("msg-body", RESUME_PAGE))

    assert result.status == "processed"
    assert len(repo.records) == 1
    # The parser was handed the body text, under the synthetic filename.
    data, filename = parser.seen[0]
    assert filename == "email_body.txt"
    assert b"EOT Crane Operator" in data


def test_the_body_is_stored_like_any_other_resume(parts):
    """A recruiter can still download what was ingested."""
    pipeline, repo, storage, _parser = parts

    pipeline.process_email(body_email("msg-body", RESUME_PAGE))

    record = next(iter(repo.records.values()))
    assert record.resume.original_filename == "email_body.txt"
    assert record.resume.storage_key in storage.saved
    assert b"EOT Crane Operator" in storage.saved[record.resume.storage_key]


def test_the_same_pasted_resume_twice_is_deduplicated(parts):
    """Body text hashes like a file, so redelivery is caught the same way."""
    pipeline, repo, _storage, _parser = parts

    first = pipeline.process_email(body_email("msg-1", RESUME_PAGE))
    second = pipeline.process_email(body_email("msg-2", RESUME_PAGE))

    assert first.status == "processed"
    assert second.attachments[0].status == "duplicate"
    assert len(repo.records) == 1


def test_a_covering_note_is_skipped_without_calling_the_parser(parts):
    """"Please find my resume attached" is not a resume — and costs nothing."""
    pipeline, repo, _storage, parser = parts

    result = pipeline.process_email(
        body_email("msg-note", "Hi, please find my resume attached. Regards, Rajesh"),
    )

    assert result.status == "skipped"
    assert repo.records == {}
    assert parser.seen == []


def test_an_empty_email_is_skipped(parts):
    pipeline, _repo, _storage, parser = parts

    result = pipeline.process_email(body_email("msg-empty", ""))

    assert result.status == "skipped"
    assert parser.seen == []
