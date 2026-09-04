"""Every ingested candidate gets their reply — eventually, and exactly once.

Sending moved off the ingestion path because it was 2.89s of a 37.75s batch
spent holding the mail loop open on the one step whose failure the pipeline
deliberately swallows. That trade is only honest alongside something that
notices when a backgrounded send did not happen, so what is pinned here is not
the speed — it is the promise:

    a candidate in the database with status="ingested" gets an auto-reply,
    whatever went wrong the first time.

Three layers hold it up, and each is tested on its own because each covers a
failure the others do not: the sender retries a transient fault in place, a
shutdown drains what is queued, and the sweep finds anything still owed on the
next cycle.
"""
from __future__ import annotations

import threading

import pytest

from app.config import settings
from app.core.models import CandidateProfile, EmailMessage, SourceEmail
from app.ingestion import pipeline as pl


# --------------------------------------------------------------------------- #
#  Doubles
# --------------------------------------------------------------------------- #
class FakeRepo:
    """Just the two writes and the one query the sender and sweep use."""

    def __init__(self, owed=()):
        self.sent: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.attempts: dict[str, int] = {}
        self._owed = list(owed)
        self.lock = threading.Lock()

    def mark_auto_reply_sent(self, candidate_id):
        with self.lock:
            self.sent.append(candidate_id)

    def record_auto_reply_failure(self, candidate_id, error):
        with self.lock:
            self.failures.append((candidate_id, error))
            self.attempts[candidate_id] = self.attempts.get(candidate_id, 0) + 1
            return self.attempts[candidate_id]

    def find_awaiting_auto_reply(self, limit=50, max_attempts=5, grace_seconds=0):
        # The grace period is a Mongo-side filter on `updated_at`; what matters
        # here is that the sweep passes it through, which
        # `test_the_sweep_honours_the_cross_worker_grace_period` pins.
        self.asked_with = {
            "limit": limit, "max_attempts": max_attempts, "grace_seconds": grace_seconds,
        }
        return [r for r in self._owed if r.id not in self.sent][:limit]


class FakeMail:
    """A mail client that fails a chosen number of times before it works."""

    def __init__(self, fail_times=0, fail_forever=False):
        self.fail_times = fail_times
        self.fail_forever = fail_forever
        self.calls: list[str] = []

    def send_reply(self, message_id, thread_id, to_addr, subject, body_text):
        self.calls.append(to_addr)
        if self.fail_forever or len(self.calls) <= self.fail_times:
            raise RuntimeError("smtp is down")
        return {"status": "sent"}


class FakeRecord:
    def __init__(self, cid, addr="candidate@example.com"):
        self.id = cid
        self.profile = CandidateProfile(full_name="Rajesh Kumar")
        self.source_email = SourceEmail(
            message_id="m-1", thread_id="t-1", from_addr=addr,
            from_name="Rajesh", subject="Application",
        )


def an_email(addr="candidate@example.com") -> EmailMessage:
    return EmailMessage(
        message_id="m-1", thread_id="t-1", from_addr=addr,
        from_name="Rajesh", subject="Application",
    )


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """The backoff is real seconds; the tests must not spend them."""
    monkeypatch.setattr(settings, "auto_reply_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "auto_reply_enabled", True)


# --------------------------------------------------------------------------- #
#  Layer 1: the sender retries in place
# --------------------------------------------------------------------------- #
def test_a_transient_smtp_failure_is_retried_not_dropped(monkeypatch):
    monkeypatch.setattr(settings, "auto_reply_send_attempts", 3)
    repo, mail = FakeRepo(), FakeMail(fail_times=2)

    ok = pl._send_auto_reply(
        repo, mail, "cand-1", CandidateProfile(), an_email(), "candidate@example.com"
    )

    assert ok is True
    assert len(mail.calls) == 3, "the sender gave up before its attempts were spent"
    assert repo.sent == ["cand-1"]


def test_the_flag_is_written_only_after_a_send_returns():
    """`auto_reply_sent` is the contract the sweep reads. It must not lie."""
    repo, mail = FakeRepo(), FakeMail(fail_forever=True)

    ok = pl._send_auto_reply(
        repo, mail, "cand-1", CandidateProfile(), an_email(), "candidate@example.com"
    )

    assert ok is False
    assert repo.sent == [], (
        "a candidate whose reply never sent is marked as replied — the sweep "
        "will now skip them for ever"
    )
    assert repo.failures and repo.failures[0][0] == "cand-1"


def test_a_permanently_failing_send_never_raises(monkeypatch):
    """The résumé is already stored. A reply can fail; an ingest cannot."""
    monkeypatch.setattr(settings, "auto_reply_send_attempts", 2)
    repo, mail = FakeRepo(), FakeMail(fail_forever=True)

    assert pl._send_auto_reply(
        repo, mail, "c", CandidateProfile(), an_email(), "x@example.com"
    ) is False


# --------------------------------------------------------------------------- #
#  Layer 3: the sweep — the actual guarantee
# --------------------------------------------------------------------------- #
def test_the_sweep_sends_what_the_background_send_never_did():
    """The production shape: the process died holding a queued reply."""
    repo = FakeRepo(owed=[FakeRecord("cand-1"), FakeRecord("cand-2")])
    mail = FakeMail()

    report = pl.flush_pending_auto_replies(repo=repo, gmail=mail)

    assert report["sent"] == 2, report
    assert sorted(repo.sent) == ["cand-1", "cand-2"]


def test_the_sweep_does_not_resend_to_a_candidate_already_replied_to():
    """Idempotent, because the flag is the only thing it goes on."""
    repo = FakeRepo(owed=[FakeRecord("cand-1")])
    mail = FakeMail()

    pl.flush_pending_auto_replies(repo=repo, gmail=mail)
    pl.flush_pending_auto_replies(repo=repo, gmail=mail)

    assert len(mail.calls) == 1, (
        f"{len(mail.calls)} sends for one candidate — the sweep is re-mailing "
        "people who already heard from us"
    )


def test_the_sweep_threads_the_reply_onto_the_original_conversation():
    """A reply that starts a new thread reads as spam to the candidate."""
    seen = {}

    class Recording(FakeMail):
        def send_reply(self, message_id, thread_id, to_addr, subject, body_text):
            seen.update(message_id=message_id, thread_id=thread_id, subject=subject)
            return {}

    pl.flush_pending_auto_replies(repo=FakeRepo(owed=[FakeRecord("c")]), gmail=Recording())

    assert seen["message_id"] == "m-1"
    assert seen["thread_id"] == "t-1"
    assert seen["subject"] == "Application"


def test_a_dead_mail_client_leaves_everyone_in_the_queue():
    """No client is a reason to try later, never a reason to mark them done."""
    repo = FakeRepo(owed=[FakeRecord("cand-1")])

    def no_client():
        raise RuntimeError("no SMTP configured")

    import app.email_client as ec
    original = ec.get_email_client
    ec.get_email_client = no_client
    try:
        report = pl.flush_pending_auto_replies(repo=repo)
    finally:
        ec.get_email_client = original

    assert repo.sent == []
    assert report["pending"] == 1


def test_the_sweep_is_off_when_auto_reply_is_off(monkeypatch):
    """The feature flag still governs. Nothing reaches a stranger without it."""
    monkeypatch.setattr(settings, "auto_reply_enabled", False)
    repo, mail = FakeRepo(owed=[FakeRecord("cand-1")]), FakeMail()

    pl.flush_pending_auto_replies(repo=repo, gmail=mail)

    assert mail.calls == []


def test_a_broken_lookup_does_not_take_down_the_poll_cycle():
    """The sweep runs at the end of a batch. It may not be able to fail it."""
    class Exploding(FakeRepo):
        def find_awaiting_auto_reply(self, limit=50, max_attempts=5, grace_seconds=0):
            raise RuntimeError("mongo is unreachable")

    report = pl.flush_pending_auto_replies(repo=Exploding(), gmail=FakeMail())

    assert report["sent"] == 0
    assert "error" in report


# --------------------------------------------------------------------------- #
#  Queueing, and what it promises the caller
# --------------------------------------------------------------------------- #
def test_queueing_returns_false_when_there_is_nothing_to_send_with():
    """No client means the sweep's problem, not a silent success."""
    assert pl.queue_auto_reply(
        FakeRepo(), None, "c", CandidateProfile(), an_email(), "x@example.com"
    ) is False


def test_a_queued_reply_is_actually_sent():
    """The fast path still has to work, not merely return quickly."""
    repo, mail = FakeRepo(), FakeMail()

    assert pl.queue_auto_reply(
        repo, mail, "cand-1", CandidateProfile(), an_email(), "candidate@example.com"
    ) is True
    pl._drain_replies()  # what shutdown does: finish what is queued

    assert repo.sent == ["cand-1"], "the queued reply never went out"


def test_shutdown_drains_the_queue_rather_than_discarding_it(monkeypatch):
    monkeypatch.setattr(settings, "auto_reply_drain_seconds", 10.0)
    repo, mail = FakeRepo(), FakeMail()
    for n in range(5):
        pl.queue_auto_reply(
            repo, mail, f"cand-{n}", CandidateProfile(), an_email(), "c@example.com"
        )

    pl._drain_replies()

    assert len(repo.sent) == 5, f"only {len(repo.sent)} of 5 queued replies were sent"


# --------------------------------------------------------------------------- #
#  The race between the two layers
# --------------------------------------------------------------------------- #
def test_the_sweep_does_not_double_send_a_reply_still_in_flight(monkeypatch):
    """The layers must not each mail the same candidate.

    This is not an edge case, it is the ordinary path: the inline poll runs the
    sweep immediately after the batch, which is exactly when the replies that
    batch queued are still working through a one-worker pool at a few seconds
    each. Every one of them still reads `auto_reply_sent=False`, because the
    flag is written *after* the send returns — so an unguarded sweep picks up a
    reply already in flight and the candidate gets two copies.
    """
    import time as _time

    monkeypatch.setattr(settings, "auto_reply_drain_seconds", 10.0)

    class Slow(FakeMail):
        def send_reply(self, message_id, thread_id, to_addr, subject, body_text):
            _time.sleep(0.3)  # long enough that the sweep starts mid-send
            return super().send_reply(message_id, thread_id, to_addr, subject, body_text)

    record = FakeRecord("cand-1")
    repo = FakeRepo(owed=[record])
    mail = Slow()

    pl.queue_auto_reply(
        repo, mail, "cand-1", record.profile, an_email(), "candidate@example.com"
    )
    # Straight into the sweep, with the send still running — the real ordering.
    pl.flush_pending_auto_replies(repo=repo, gmail=mail)

    assert len(mail.calls) == 1, (
        f"{len(mail.calls)} sends for one candidate: the sweep mailed someone "
        "whose reply was already in flight"
    )
    assert repo.sent == ["cand-1"]


def test_the_sweep_honours_the_cross_worker_grace_period(monkeypatch):
    """Draining closes the double-send window in one process, not across two.

    With more than one worker, the process that ingested a candidate holds the
    queued reply while a beat sweep can land on a different worker, drain an
    empty pool of its own, and read the same `auto_reply_sent=False`. Both then
    send. The grace period is what separates "in flight somewhere" from
    "genuinely failed": a send takes seconds, and this is minutes.
    """
    monkeypatch.setattr(settings, "auto_reply_grace_seconds", 120)
    repo = FakeRepo(owed=[FakeRecord("cand-1")])

    pl.flush_pending_auto_replies(repo=repo, gmail=FakeMail())

    assert repo.asked_with["grace_seconds"] == 120, (
        "the sweep asked for every unreplied candidate regardless of how "
        "recently it was touched — a reply in flight on another worker will "
        "be sent twice"
    )
