"""Background ingestion: the poll lock, the worker probe, and summary shaping."""
from __future__ import annotations

import pytest

from app.ingestion.pipeline import AttachmentResult, ProcessResult
from app.ingestion.runner import BatchSummary
from app.tasks import health, locks
from app.tasks.locks import LockNotAcquired, claim_message, redis_lock


# --------------------------------------------------------------------------- #
#  A Redis stand-in covering just what the lock uses
# --------------------------------------------------------------------------- #
class FakeRedis:
    """SET NX EX plus the release script, with no expiry-clock simulation.

    The lock's correctness does not depend on time passing — it depends on NX
    refusing a second holder and on release only deleting one's own token.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        # Kept apart from `store` so the assertions above still read as
        # "which locks are held"; this is "for how long".
        self.expiries: dict[str, int | None] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.expiries[key] = ex
        return True

    def eval(self, _script, _numkeys, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0

    def ping(self):
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(locks, "get_redis", lambda: client)
    return client


# --------------------------------------------------------------------------- #
#  The lock
# --------------------------------------------------------------------------- #
def test_lock_is_released_when_the_block_exits(fake_redis):
    with redis_lock("poll", 60):
        assert "lock:poll" in fake_redis.store
    assert fake_redis.store == {}


def test_second_holder_is_refused_while_the_first_holds_it(fake_redis):
    with redis_lock("poll", 60):
        with pytest.raises(LockNotAcquired):
            with redis_lock("poll", 60):
                pytest.fail("the lock let two holders in at once")


def test_lock_is_released_even_when_the_body_raises(fake_redis):
    """A failed poll must not wedge every later poll behind a stuck lock."""
    with pytest.raises(ValueError):
        with redis_lock("poll", 60):
            raise ValueError("pipeline blew up")
    assert fake_redis.store == {}


def test_release_does_not_delete_a_lock_someone_else_now_holds(fake_redis):
    """The dangerous case: A's lock expires, B acquires, then A exits.

    A's release must be a no-op, or B loses a lock it is still working under.
    """
    with pytest.raises(RuntimeError):
        with redis_lock("poll", 60):
            # Simulate expiry-then-reacquisition by another worker.
            fake_redis.store["lock:poll"] = "worker-b-token"
            raise RuntimeError("worker A dies here")

    assert fake_redis.store["lock:poll"] == "worker-b-token"


def test_lock_is_reusable_after_release(fake_redis):
    for _ in range(3):
        with redis_lock("poll", 60):
            pass
    assert fake_redis.store == {}


def test_different_names_do_not_contend(fake_redis):
    with redis_lock("poll", 60):
        with redis_lock("other", 60):
            assert len(fake_redis.store) == 2


# --------------------------------------------------------------------------- #
#  The per-message claim
# --------------------------------------------------------------------------- #
def test_a_claimed_message_is_refused_to_everyone_else(fake_redis):
    with claim_message("msg-1") as first:
        assert first is True
        with claim_message("msg-1") as second:
            assert second is False, "two workers may not hold the same message"


def test_claims_on_different_messages_do_not_contend(fake_redis):
    with claim_message("msg-1") as a, claim_message("msg-2") as b:
        assert (a, b) == (True, True)


def test_a_claim_is_released_when_the_message_is_done(fake_redis):
    with claim_message("msg-1"):
        pass
    with claim_message("msg-1") as again:
        assert again is True, "a finished message must be claimable again"


def test_a_claim_is_released_even_when_processing_raises(fake_redis):
    """A failed resume must be retryable, not wedged behind its own claim."""
    with pytest.raises(ValueError):
        with claim_message("msg-1"):
            raise ValueError("OCR blew up")
    assert fake_redis.store == {}


def test_an_unreachable_redis_lets_the_message_through(monkeypatch):
    """The lock service being down must not stop resumes being ingested. The
    ledger and the resume-hash index still keep the duplicate record out; the
    worst case here is extraction paid for twice, against an inbox that never
    drains."""
    import redis as redis_module

    def no_redis():
        raise redis_module.ConnectionError("connection refused")

    monkeypatch.setattr(locks, "get_redis", no_redis)

    with claim_message("msg-1") as claimed:
        assert claimed is True


# --------------------------------------------------------------------------- #
#  Summary serialisation — the API and the task must agree on one shape
# --------------------------------------------------------------------------- #
def test_summary_to_dict_carries_counts_and_nested_attachments():
    from app.tasks.jobs import summary_to_dict

    summary = BatchSummary(
        fetched=2, processed=1, skipped=1, errors=0, ingested_candidates=1
    )
    summary.results.append(
        ProcessResult(
            message_id="msg-1",
            status="processed",
            attachments=[
                AttachmentResult(filename="cv.pdf", status="ingested", candidate_id="c-1"),
                AttachmentResult(filename="old.pdf", status="duplicate", detail="already seen"),
            ],
        )
    )

    payload = summary_to_dict(summary)

    assert payload["fetched"] == 2
    assert payload["ingested_candidates"] == 1
    assert payload["results"][0]["message_id"] == "msg-1"

    attachments = payload["results"][0]["attachments"]
    assert [a["filename"] for a in attachments] == ["cv.pdf", "old.pdf"]
    assert attachments[0]["candidate_id"] == "c-1"
    assert attachments[1]["detail"] == "already seen"


def test_summary_to_dict_is_json_serialisable():
    """It crosses the Celery result backend as JSON, so it must survive a round trip."""
    import json

    from app.tasks.jobs import summary_to_dict

    payload = summary_to_dict(BatchSummary())
    assert json.loads(json.dumps(payload)) == payload


# --------------------------------------------------------------------------- #
#  run_poll_cycle
# --------------------------------------------------------------------------- #
def test_poll_cycle_runs_the_batch_and_returns_its_summary(fake_redis, monkeypatch):
    from app.tasks import jobs

    summary = BatchSummary(fetched=1, processed=1, ingested_candidates=1)
    summary.results.append(
        ProcessResult(
            message_id="msg-1",
            status="processed",
            attachments=[AttachmentResult(filename="cv.pdf", status="ingested")],
        )
    )

    class StubRunner:
        def run_once(self, query=None):
            return summary

    monkeypatch.setattr(jobs, "IngestionRunner", lambda: StubRunner())

    payload = jobs.run_poll_cycle()

    assert payload["ingested_candidates"] == 1
    assert "skipped_reason" not in payload
    assert fake_redis.store == {}, "the lock must be released once the batch ends"


def test_poll_cycle_declines_when_another_cycle_holds_the_lock(fake_redis, monkeypatch):
    """A beat tick landing on a manual sync must no-op, not double-ingest."""
    from app.tasks import jobs

    def must_not_run():
        pytest.fail("the batch ran while another cycle held the lock")

    monkeypatch.setattr(jobs, "IngestionRunner", must_not_run)
    fake_redis.store["lock:" + locks.POLL_LOCK] = "the-other-cycle"

    payload = jobs.run_poll_cycle()

    assert payload["skipped_reason"]
    assert payload["fetched"] == 0
    assert payload["ingested_candidates"] == 0
    assert fake_redis.store["lock:" + locks.POLL_LOCK] == "the-other-cycle"


def test_poll_cycle_releases_the_lock_when_the_batch_raises(fake_redis, monkeypatch):
    from app.tasks import jobs

    class ExplodingRunner:
        def run_once(self, query=None):
            raise RuntimeError("Gmail is down")

    monkeypatch.setattr(jobs, "IngestionRunner", lambda: ExplodingRunner())

    with pytest.raises(RuntimeError):
        jobs.run_poll_cycle()

    assert fake_redis.store == {}, "a failed batch must not wedge the lock"


# --------------------------------------------------------------------------- #
#  poll_gmail — the fan-out poller beat runs
# --------------------------------------------------------------------------- #
class StubTask:
    """Stands in for `process_message`, recording what was queued."""

    def __init__(self):
        self.dispatched = []

    def delay(self, message_id):
        self.dispatched.append(message_id)


def _stub_mailbox(monkeypatch, message_ids):
    from app.tasks import jobs

    class StubClient:
        def search_message_ids(self, query=None):
            return list(message_ids)

    client = StubClient()
    monkeypatch.setattr(jobs, "get_email_client", lambda: client)
    monkeypatch.setattr(jobs, "get_all_email_clients", lambda: [client])
    queue = StubTask()
    monkeypatch.setattr(jobs, "process_message", queue)
    return queue


def test_poll_gmail_queues_one_task_per_message(fake_redis, monkeypatch):
    from app.tasks import jobs

    queue = _stub_mailbox(monkeypatch, ["msg-1", "msg-2", "msg-3"])

    payload = jobs.poll_gmail()

    assert queue.dispatched == ["msg-1", "msg-2", "msg-3"]
    assert payload["dispatched"] == 3


def test_poll_gmail_gives_the_lock_back_as_soon_as_it_has_dispatched(fake_redis, monkeypatch):
    """The whole point of the fan-out: the resumes are still being extracted
    when the next tick comes round, and it must find the lock free."""
    _stub_mailbox(monkeypatch, ["msg-1"])
    from app.tasks import jobs

    jobs.poll_gmail()

    assert fake_redis.store == {}


def test_poll_gmail_holds_the_lock_for_a_search_not_for_a_batch(fake_redis, monkeypatch):
    """A dispatch that took the batch TTL would keep every later tick out for
    half an hour if the poller ever died mid-search."""
    from app.config import settings
    from app.tasks import jobs

    held_for = {}

    class WatchingClient:
        def search_message_ids(self, query=None):
            held_for["ttl"] = fake_redis.expiries["lock:" + locks.POLL_LOCK]
            return []

    watching = WatchingClient()
    monkeypatch.setattr(jobs, "get_email_client", lambda: watching)
    monkeypatch.setattr(jobs, "get_all_email_clients", lambda: [watching])

    jobs.poll_gmail()

    assert held_for["ttl"] == settings.poll_dispatch_lock_ttl_seconds
    assert held_for["ttl"] < settings.poll_lock_ttl_seconds


def test_poll_gmail_declines_while_a_manual_sync_holds_the_lock(fake_redis, monkeypatch):
    from app.tasks import jobs

    queue = _stub_mailbox(monkeypatch, ["msg-1"])
    fake_redis.store["lock:" + locks.POLL_LOCK] = "the-manual-sync"

    payload = jobs.poll_gmail()

    assert payload["dispatched"] == 0
    assert payload["skipped_reason"]
    assert queue.dispatched == [], "the sync holding the lock is doing this work"


# --------------------------------------------------------------------------- #
#  process_message — one email, on its own
# --------------------------------------------------------------------------- #
class StubGmail:
    def __init__(self):
        self.read = []
        self.applied = []
        self.removed = []

    def get_message(self, message_id):
        return message_id

    def mark_read(self, message_id):
        self.read.append(message_id)

    def apply_label(self, message_id, label):
        self.applied.append((message_id, label))

    def remove_label(self, message_id, label):
        self.removed.append((message_id, label))


def _stub_pipeline(monkeypatch, result):
    from app.tasks import jobs

    gmail = StubGmail()
    monkeypatch.setattr(jobs, "get_email_client", lambda: gmail)
    monkeypatch.setattr(jobs, "get_all_email_clients", lambda: [gmail])
    monkeypatch.setattr(
        jobs, "IngestionPipeline",
        lambda: type("P", (), {"process_email": lambda self, email, gmail=None: result})(),
    )
    return gmail


def test_process_message_marks_an_ingested_email_done(fake_redis, monkeypatch):
    from app.tasks import jobs

    gmail = _stub_pipeline(
        monkeypatch,
        ProcessResult("msg-1", "processed", "", [
            AttachmentResult("cv.pdf", "ingested", "cand-1"),
        ]),
    )

    payload = jobs.process_message("msg-1")

    assert payload["status"] == "processed"
    assert payload["candidates"] == ["cand-1"]
    assert gmail.read == ["msg-1"]
    assert gmail.applied == [("msg-1", "Resumes/Processed")]


def test_process_message_re_asserts_deleted_on_a_suppressed_email(fake_redis, monkeypatch):
    """A retired email comes back until Gmail's search index catches up. It must
    never be stamped processed — that is how one ended up carrying both labels."""
    from app.tasks import jobs

    gmail = _stub_pipeline(
        monkeypatch, ProcessResult("msg-1", "suppressed", "candidate was deleted"),
    )

    jobs.process_message("msg-1")

    assert gmail.applied == [("msg-1", "Resumes/Deleted")]
    assert gmail.removed == [("msg-1", "Resumes/Processed")]
    assert gmail.read == []


def test_process_message_leaves_a_non_resume_email_alone(fake_redis, monkeypatch):
    from app.tasks import jobs

    gmail = _stub_pipeline(
        monkeypatch, ProcessResult("msg-1", "skipped", "not a resume email"),
    )

    jobs.process_message("msg-1")

    assert gmail.applied == []
    assert gmail.read == []


def test_process_message_drops_a_message_another_worker_already_has(fake_redis, monkeypatch):
    """Two beat ticks can queue the same unlabelled message. The second task
    must return, not extract the same resume a second time."""
    from app.tasks import jobs

    ran = []

    class Watching:
        def process_email(self, email, gmail=None):
            ran.append(email)
            return ProcessResult("msg-1", "processed")

    stub = StubGmail()
    monkeypatch.setattr(jobs, "get_email_client", lambda: stub)
    monkeypatch.setattr(jobs, "get_all_email_clients", lambda: [stub])
    monkeypatch.setattr(jobs, "IngestionPipeline", lambda: Watching())
    fake_redis.store[f"lock:{locks.MESSAGE_LOCK_PREFIX}:msg-1"] = "the-other-worker"

    payload = jobs.process_message("msg-1")

    assert payload["status"] == "skipped"
    assert ran == [], "the message was already being processed"


def test_process_message_releases_its_claim(fake_redis, monkeypatch):
    from app.tasks import jobs

    _stub_pipeline(monkeypatch, ProcessResult("msg-1", "processed"))

    jobs.process_message("msg-1")

    assert fake_redis.store == {}, "a finished message must be claimable again"


def test_a_labelling_failure_does_not_lose_the_ingested_candidate(fake_redis, monkeypatch):
    """The profile is already in Mongo. Raising here would retry the task and
    ingest it all over again."""
    from app.tasks import jobs

    gmail = _stub_pipeline(
        monkeypatch,
        ProcessResult("msg-1", "processed", "", [
            AttachmentResult("cv.pdf", "ingested", "cand-1"),
        ]),
    )
    gmail.apply_label = lambda *_: (_ for _ in ()).throw(RuntimeError("Gmail rate limit"))

    payload = jobs.process_message("msg-1")

    assert payload["candidates"] == ["cand-1"]


# --------------------------------------------------------------------------- #
#  Worker probe
# --------------------------------------------------------------------------- #
def test_probe_result_is_memoised(monkeypatch):
    health.reset_cache()
    calls = []

    def counting_probe():
        calls.append(1)
        return False

    monkeypatch.setattr(health, "_probe", counting_probe)

    assert health.workers_online() is False
    assert health.workers_online() is False
    assert health.workers_online() is False
    assert len(calls) == 1, "a cached answer should not re-probe a down broker"


def test_force_bypasses_the_memo(monkeypatch):
    health.reset_cache()
    calls = []
    monkeypatch.setattr(health, "_probe", lambda: (calls.append(1), True)[1])

    health.workers_online()
    health.workers_online(force=True)
    assert len(calls) == 2


def test_reset_cache_forces_the_next_probe(monkeypatch):
    health.reset_cache()
    calls = []
    monkeypatch.setattr(health, "_probe", lambda: (calls.append(1), False)[1])

    health.workers_online()
    health.reset_cache()
    health.workers_online()
    assert len(calls) == 2


def test_probe_reports_offline_rather_than_raising(monkeypatch):
    """Every failure mode collapses to False so the caller can run inline."""
    health.reset_cache()

    def exploding_redis():
        raise OSError("no route to host")

    monkeypatch.setattr(locks, "get_redis", exploding_redis)
    assert health.workers_online(force=True) is False
