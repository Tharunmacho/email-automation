"""Draining a mailbox that has more in it than one batch.

Two things have to hold at once for a thousand résumés to get in:

* nothing is dropped — a message stays in the queue until it has actually been
  decided, however many polls that takes;
* nothing already decided is paid for twice — the inbox keeps every non-résumé
  email anyone was ever sent, and re-downloading those on each poll is what
  stopped this scaling with the mailbox rather than with the work.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.models import Attachment, EmailMessage
from app.ingestion.pipeline import ProcessResult
from app.ingestion.runner import IngestionRunner


class FakeLedger:
    def __init__(self, seen=()):
        self.seen = set(seen)
        self.asked: list[str] = []
        self.recorded: list[tuple] = []

    def message_seen(self, message_id):
        self.asked.append(message_id)
        return message_id in self.seen

    def is_message_suppressed(self, _message_id):
        return False

    def is_suppressed(self, _resume_hash):
        return False

    def find_by_hash(self, _resume_hash):
        return None

    def record(self, message_id, resume_hash, candidate_id, status, detail=""):
        self.recorded.append((message_id, resume_hash, status))


class StubPipeline:
    def __init__(self, ledger):
        self.ledger = ledger
        self.handled: list[str] = []

    def process_email(self, email, gmail=None):
        self.handled.append(email)
        return ProcessResult(email, "skipped", "stubbed")


def _runner(uids, ledger, monkeypatch, limit=25):
    monkeypatch.setattr("app.ingestion.runner.settings.gmail_max_results", limit)
    client = MagicMock()
    client.search_message_ids.return_value = list(uids)
    client.get_message.side_effect = lambda mid: mid
    monkeypatch.setattr("app.ingestion.runner.GmailClient", lambda: client)
    return IngestionRunner(gmail=client, pipeline=StubPipeline(ledger)), client


def test_a_message_already_decided_is_never_downloaded_again(monkeypatch):
    ledger = FakeLedger(seen={"2", "4"})
    runner, client = _runner(["1", "2", "3", "4", "5"], ledger, monkeypatch)

    summary = runner.run_once()

    assert sorted(runner.pipeline.handled) == ["1", "3", "5"]
    assert summary.fetched == 3
    fetched = [c.args[0] for c in client.get_message.call_args_list]
    assert "2" not in fetched and "4" not in fetched, "a settled message was re-downloaded"


def test_the_batch_is_bounded_but_the_queue_is_not_truncated(monkeypatch):
    """What does not fit is reported as backlog, not silently dropped."""
    ledger = FakeLedger()
    runner, _ = _runner([str(n) for n in range(1, 101)], ledger, monkeypatch, limit=25)

    summary = runner.run_once()

    assert summary.fetched == 25
    assert summary.backlog == 75


def test_the_batch_is_taken_from_the_front_of_the_queue(monkeypatch):
    """Oldest first, so the next poll continues where this one stopped rather
    than working the same newest few for ever.

    Asserted as a set: the batch is worked by a thread pool, so *which* five
    were chosen is the guarantee here and the order they happen to finish in is
    not.
    """
    ledger = FakeLedger()
    runner, _ = _runner([str(n) for n in range(1, 51)], ledger, monkeypatch, limit=5)

    runner.run_once()

    assert sorted(runner.pipeline.handled, key=int) == ["1", "2", "3", "4", "5"]


def test_a_drained_inbox_reports_no_backlog(monkeypatch):
    ledger = FakeLedger()
    runner, _ = _runner(["1", "2"], ledger, monkeypatch, limit=25)

    assert runner.run_once().backlog == 0


def test_a_broken_ledger_fetches_everything_rather_than_nothing(monkeypatch):
    """Bookkeeping being unavailable must cost money, never messages."""

    class Broken(FakeLedger):
        def message_seen(self, message_id):
            raise RuntimeError("mongo is down")

    runner, _ = _runner(["1", "2", "3"], Broken(), monkeypatch)

    assert runner.run_once().fetched == 3


# --------------------------------------------------------------------------- #
#  The row that makes the pre-filter possible
# --------------------------------------------------------------------------- #
def _plain_email(message_id: str) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        thread_id=f"t-{message_id}",
        from_addr="newsletter@example.com",
        subject="Our September newsletter",
        body_text="Nothing to do with hiring.",
        attachments=[
            Attachment(filename="flyer.png", mime_type="image/png", size=10,
                       attachment_id="a1", data=b"x")
        ],
    )


def test_an_email_that_is_not_a_resume_is_decided_once(monkeypatch):
    """Nothing labels a non-résumé email — it is somebody's ordinary mail and it
    stays where it is — so it comes back in every future search. The ledger row
    is what stops the poll re-downloading and re-detecting it for ever."""
    from app.ingestion.pipeline import IngestionPipeline
    from app.db.ledger import NOT_A_RESUME_SENTINEL

    ledger = FakeLedger()
    pipeline = IngestionPipeline(
        repository=MagicMock(find_by_message_id=lambda _m: None),
        storage=MagicMock(),
        parser=MagicMock(),
        ledger=ledger,
    )

    result = pipeline.process_email(_plain_email("77"))

    assert result.status == "skipped"
    assert ledger.recorded == [("77", NOT_A_RESUME_SENTINEL, "not_a_resume")]


def test_the_non_resume_sentinel_can_never_match_a_real_file():
    """It is keyed by message alone. A real CV arriving later with a real hash
    must not collide with the row that says an unrelated email had none."""
    from app.db.dedup import sha256_hex
    from app.db.ledger import DELETED_SENTINEL, NOT_A_RESUME_SENTINEL

    assert NOT_A_RESUME_SENTINEL != DELETED_SENTINEL
    assert NOT_A_RESUME_SENTINEL != sha256_hex(b"")
    assert not all(c in "0123456789abcdef" for c in NOT_A_RESUME_SENTINEL)
