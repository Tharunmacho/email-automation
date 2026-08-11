"""The batch summary must describe what actually reached MongoDB.

A poll that wrote a candidate profile once reported `Ingested Candidates=0`,
because Gmail post-processing ran inside the same `try` as the ingestion: one
failed `apply_label` discarded the whole result and counted it as an error.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.ingestion.pipeline import AttachmentResult, ProcessResult
from app.ingestion.runner import IngestionRunner


@pytest.fixture(autouse=True)
def claims_granted(monkeypatch):
    """Every message is claimable unless a test says otherwise.

    Stubbed rather than left to the real lock so these tests describe the
    runner's reporting and never depend on a Redis being up.
    """

    @contextmanager
    def granted(_message_id, _ttl=None):
        yield True

    monkeypatch.setattr("app.ingestion.runner.claim_message", granted)


class StubPipeline:
    """Always ingests, so anything missing from the summary was lost by the runner."""

    def __init__(self):
        self.seen = []

    def process_email(self, email, gmail=None):
        self.seen.append(email)
        return ProcessResult(
            email, "processed", "stubbed",
            [AttachmentResult("resume.pdf", "ingested", "candidate-new")],
        )


@pytest.fixture
def gmail():
    client = MagicMock()
    client.search_message_ids.return_value = ["msg-1"]
    client.get_message.side_effect = lambda mid: mid
    return client


def _runner(gmail, monkeypatch):
    # The runner builds a fresh client per message for thread-safety.
    monkeypatch.setattr("app.ingestion.runner.GmailClient", lambda: gmail)
    return IngestionRunner(gmail=gmail, pipeline=StubPipeline())


def test_summary_counts_an_ingested_candidate(gmail, monkeypatch):
    summary = _runner(gmail, monkeypatch).run_once()

    assert summary.fetched == 1
    assert summary.processed == 1
    assert summary.ingested_candidates == 1
    assert summary.errors == 0


def test_a_labelling_failure_does_not_erase_the_ingested_candidate(gmail, monkeypatch):
    """The profile is already in Mongo; Gmail failing to mark the mail done is a
    warning, not a lost ingestion."""
    gmail.apply_label.side_effect = RuntimeError("Gmail rate limit")

    summary = _runner(gmail, monkeypatch).run_once()

    assert gmail.apply_label.called, "the failing call must actually have run"
    assert summary.processed == 1, "an ingested candidate must still be reported"
    assert summary.ingested_candidates == 1
    assert summary.errors == 0


def test_a_suppressed_email_is_re_labelled_deleted_and_never_marked_processed(gmail, monkeypatch):
    """Gmail's search index lags the delete, so retired emails come back for a
    while. They must be re-asserted as deleted, not stamped processed — that is
    how one email ended up carrying both labels."""
    runner = _runner(gmail, monkeypatch)
    runner.pipeline = type("Suppressing", (), {
        "process_email": lambda self, email, gmail=None: ProcessResult(
            email, "suppressed", "candidate was deleted by a user"),
    })()

    summary = runner.run_once()

    gmail.apply_label.assert_called_once_with("msg-1", "Resumes/Deleted")
    gmail.remove_label.assert_called_once_with("msg-1", "Resumes/Processed")
    assert summary.suppressed == 1
    assert summary.errors == 0
    assert summary.ingested_candidates == 0


def test_a_message_another_worker_holds_is_left_to_that_worker(gmail, monkeypatch):
    """A manual sync re-fetches messages a beat tick has already fanned out to
    the workers — they are unlabelled until their resumes are stored. Without
    the claim both would run the same OCR and the same LLM call."""

    @contextmanager
    def refused(_message_id, _ttl=None):
        yield False

    monkeypatch.setattr("app.ingestion.runner.claim_message", refused)
    runner = _runner(gmail, monkeypatch)

    summary = runner.run_once()

    assert runner.pipeline.seen == [], "the other worker is already processing it"
    assert summary.skipped == 1
    assert summary.errors == 0
    assert not gmail.apply_label.called, "the worker that has it will label it"


def test_a_failure_before_ingestion_is_still_counted_as_an_error(gmail, monkeypatch):
    gmail.get_message.side_effect = RuntimeError("Gmail unreachable")

    summary = _runner(gmail, monkeypatch).run_once()

    assert summary.errors == 1
    assert summary.processed == 0
    assert summary.ingested_candidates == 0
