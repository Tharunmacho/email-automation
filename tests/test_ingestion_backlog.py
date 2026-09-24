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
from app.ingestion.runner import BatchSummary, IngestionRunner


class FakeLedger:
    def __init__(self, seen=()):
        self.seen = set(seen)
        self.asked: list[str] = []
        self.bulk_calls = 0
        self.recorded: list[tuple] = []

    def message_seen(self, message_id):
        self.asked.append(message_id)
        return message_id in self.seen

    def seen_message_ids(self, message_ids):
        """One call for the whole list, mirroring the real ledger."""
        self.bulk_calls += 1
        ids = list(message_ids)
        self.asked.extend(ids)
        return {m for m in ids if m in self.seen}

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
        def seen_message_ids(self, message_ids):
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


def test_the_whole_inbox_is_filtered_in_one_query(monkeypatch):
    """The regression that made a poll look hung.

    The filter asked the ledger about each message separately. At ~340ms a
    round trip against a remote Mongo, a 1,193-message mailbox spent seven and
    a half minutes on nothing but that — every poll, before the first résumé
    was fetched — and printed nothing while it did, because the line reporting
    the count came afterwards.
    """
    ledger = FakeLedger()
    runner, _ = _runner([str(n) for n in range(1, 1311)], ledger, monkeypatch, limit=25)

    runner.run_once()

    assert ledger.bulk_calls == 1, "the inbox must be filtered in a single query"


# --------------------------------------------------------------------------- #
#  The backlog has to survive being reported, not just be counted
# --------------------------------------------------------------------------- #
def test_the_backlog_survives_serialisation(monkeypatch):
    """`summary.backlog` is useless if the dict the callers read drops it.

    This is the bug this test exists for, and it was invisible from the runner's
    side: `run_once` counted the backlog correctly and
    `test_a_batch_is_bounded_and_the_rest_is_backlog` proved it. But every
    consumer reads the batch through `summary_to_dict`, which did not carry the
    field — so the inline poll's drain loop read `backlog=0`, broke after one
    batch, and left the rest of the mailbox sitting there until somebody pressed
    Sync again. One batch per click, for a queue that exists so that nobody has
    to click.
    """
    from app.tasks.jobs import summary_to_dict

    ledger = FakeLedger()
    runner, _ = _runner([str(n) for n in range(1, 101)], ledger, monkeypatch, limit=25)

    as_dict = summary_to_dict(runner.run_once())

    assert as_dict["backlog"] == 75, (
        "the backlog did not survive summary_to_dict; anything draining a "
        "mailbox by looping until it reaches zero will stop after one batch"
    )


class _ScriptedRunner:
    """A runner that returns a prepared batch each time it is asked."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    def run_once(self, query=None):
        self.calls += 1
        spec = self.batches[min(self.calls - 1, len(self.batches) - 1)]
        summary = BatchSummary()
        for key, value in spec.items():
            setattr(summary, key, value)
        return summary


def test_one_sync_drains_the_whole_mailbox():
    """A backlog is emptied by one Sync, not by one press per batch."""
    from app.tasks.jobs import drain_mailbox

    runner = _ScriptedRunner([
        {"fetched": 25, "processed": 25, "backlog": 50, "ingested_candidates": 25},
        {"fetched": 25, "processed": 25, "backlog": 25, "ingested_candidates": 25},
        {"fetched": 25, "processed": 25, "backlog": 0, "ingested_candidates": 25},
    ])

    combined = drain_mailbox(runner)

    assert runner.calls == 3, f"the drain stopped after {runner.calls} batch(es)"
    assert combined["processed"] == 75
    assert combined["ingested_candidates"] == 75
    assert combined["backlog"] == 0


def test_a_batch_that_decides_nothing_stops_the_drain():
    """The guard that keeps a retryable failure from becoming an infinite loop.

    A message that fails is left UNSEEN and unrecorded so it can be tried again
    — which means the next cycle is handed exactly the same ids and reports
    exactly the same backlog. Without this stop the drain re-runs the same
    failing batch for ever, paying for OCR on every pass.
    """
    from app.tasks.jobs import drain_mailbox

    runner = _ScriptedRunner([{"fetched": 25, "processed": 0, "errors": 25, "backlog": 50}])

    combined = drain_mailbox(runner)

    assert runner.calls == 1, (
        f"the drain ran {runner.calls} identical failing batches instead of stopping"
    )
    assert combined["backlog"] == 50, "what it gave up on must still be reported as queued"


def test_the_drain_is_capped_even_when_it_keeps_making_progress(monkeypatch):
    """A backstop, so no mailbox can hold one sync open indefinitely."""
    from app.config import settings
    from app.tasks.jobs import drain_mailbox

    monkeypatch.setattr(settings, "inline_poll_max_cycles", 5)
    # Always more to do, and always some progress: without the cap, for ever.
    runner = _ScriptedRunner([{"fetched": 25, "processed": 25, "backlog": 999}])

    drain_mailbox(runner)

    assert runner.calls == 5, f"the cap did not hold; ran {runner.calls} cycles"


def test_progress_is_reported_after_every_batch():
    """The UI has to move during a long drain, not jump at the end."""
    from app.tasks.jobs import drain_mailbox

    seen: list[int] = []
    runner = _ScriptedRunner([
        {"fetched": 25, "processed": 25, "backlog": 25},
        {"fetched": 25, "processed": 25, "backlog": 0},
    ])

    drain_mailbox(runner, on_progress=lambda running: seen.append(running["processed"]))

    assert seen == [25, 50], f"progress was reported as {seen}"
