"""Deleting a candidate must prevent automated ingestion from restoring it.

The original emails and the content hash are both suppressed, so mailbox
retries and forwarded copies cannot recreate a permanently deleted candidate.
"""
from typing import Optional

import pytest

from app.core.models import Attachment, CandidateProfile, EmailMessage, ExtractedDocument
from app.db.dedup import sha256_hex
from app.ingestion.pipeline import IngestionPipeline

RESUME_BYTES = b"%PDF-1.4 Alice Smith alice@example.com +1 555-0100"
RESUME_HASH = sha256_hex(RESUME_BYTES)


class FakeRepo:
    def __init__(self):
        self.records = {}
        self.deleted_emails = set()
        self.deleted_phones = set()

    def find_by_message_id(self, message_id):
        return next((r for r in self.records.values()
                     if r.source_email and r.source_email.message_id == message_id), None)

    def find_by_resume_hash(self, resume_hash):
        return next((r for r in self.records.values() if r.resume_hash == resume_hash), None)

    def find_by_email_or_phone(self, email_key, phone_key):
        return next((r for r in self.records.values()
                     if (email_key and r.email_key == email_key)
                     or (phone_key and r.phone_key == phone_key)), None)

    def insert(self, record):
        self.records[record.id] = record
        return record.id

    def delete(self, candidate_id):
        record = self.records.pop(candidate_id, None)
        if record:
            self.deleted_emails.add(record.email_key)
            self.deleted_phones.add(record.phone_key)
        return record is not None

    def was_deleted(self, *, email_key=None, phone_key=None, **_signals):
        return email_key in self.deleted_emails or phone_key in self.deleted_phones

    def mark_auto_reply_sent(self, candidate_id):
        pass


class FakeLedger:
    def __init__(self):
        self.rows = []

    def message_seen(self, message_id):
        return any(r["message_id"] == message_id for r in self.rows)

    def is_suppressed(self, resume_hash):
        return any(r["resume_hash"] == resume_hash and r["suppressed"] for r in self.rows)

    def is_message_suppressed(self, message_id):
        return any(r["message_id"] == message_id and r["suppressed"] for r in self.rows)

    def find_by_hash(self, resume_hash):
        from app.db.ledger import LedgerEntry
        row = next((r for r in self.rows if r["resume_hash"] == resume_hash), None)
        return LedgerEntry(row["message_id"], row["resume_hash"], row["candidate_id"],
                           row["suppressed"]) if row else None

    def record(self, message_id, resume_hash, candidate_id, status, detail=""):
        self.rows.append({"message_id": message_id, "resume_hash": resume_hash,
                          "candidate_id": candidate_id, "status": status, "suppressed": False})

    def retire_candidate(self, candidate_id, message_ids, resume_hash=None):
        """Mirrors IngestLedger: hash-keyed rows go, message tombstones arrive.

        Deliberately has no `suppress_hash`. The fake used to carry one the real
        `IngestLedger` had not had for some time, so the whole suite went green
        while every real delete raised `AttributeError` and left the candidate
        removed with none of its emails retired. A stub may be simpler than the
        thing it stands for; it may not have methods the real one lacks.
        """
        self.rows = [r for r in self.rows
                     if r["candidate_id"] != candidate_id
                     and r["resume_hash"] != resume_hash
                     and r["message_id"] not in message_ids]
        for mid in message_ids:
            self.rows.append({"message_id": mid, "resume_hash": "__deleted__",
                              "candidate_id": candidate_id, "status": "deleted",
                              "suppressed": True})
        return len(message_ids)


class FakeStorage:
    name = "fake"

    def __init__(self):
        self.saved = {}

    def exists(self, key) -> bool:
        return key in self.saved

    def save(self, key, data, content_type: Optional[str] = None):
        self.saved[key] = data
        return key


class FakeParser:
    def parse_file(self, data, filename):
        profile = CandidateProfile(
            is_resume=True, confidence=0.9, full_name="Alice Smith",
            email="alice@example.com", phone="+1 555-0100",
        )
        return profile, ExtractedDocument(text="Alice Smith", method="pdf_text", char_count=11)


def _email(message_id: str) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        from_addr="alice@example.com",
        from_name="Alice Smith",
        subject="My resume",
        body_text="Please find my resume attached.",
        attachments=[Attachment(filename="alice_resume.pdf", mime_type="application/pdf",
                                size=len(RESUME_BYTES), attachment_id="att-1", data=RESUME_BYTES)],
    )


@pytest.fixture
def parts():
    repo, ledger, storage = FakeRepo(), FakeLedger(), FakeStorage()
    pipeline = IngestionPipeline(repository=repo, storage=storage,
                                 parser=FakeParser(), ledger=ledger)
    return pipeline, repo, ledger


def test_same_file_from_a_new_email_is_a_duplicate_while_the_candidate_lives(parts):
    pipeline, repo, _ = parts
    first = pipeline.process_email(_email("msg-original"))
    assert first.status == "processed"
    assert len(repo.records) == 1

    resent = pipeline.process_email(_email("msg-resent"))
    assert resent.attachments[0].status == "duplicate"
    assert len(repo.records) == 1


def test_same_file_ingests_as_new_after_the_candidate_is_deleted(parts):
    pipeline, repo, ledger = parts
    first = pipeline.process_email(_email("msg-original"))
    candidate_id = first.ingested_ids[0]

    # What DELETE /candidates/{id} does: drop the record, retire its emails.
    repo.delete(candidate_id)
    ledger.retire_candidate(candidate_id, ["msg-original"], resume_hash=RESUME_HASH)

    # A new email carrying the identical file ingests as a new candidate.
    again = pipeline.process_email(_email("msg-sent-again"))

    assert again.status == "processed", "re-sent resume must ingest as a new candidate"
    assert again.ingested_ids and again.ingested_ids[0] != candidate_id
    assert len(repo.records) == 1


def test_a_modified_resume_from_the_same_person_also_ingests_again(parts):
    """Same rule, and the case the removed gate was really aimed at.

    It matched on the candidate's own email and phone, so a *different* file
    from the same person was refused too. That is the deletion becoming
    permanent for the person rather than for the mail — exactly what must not
    happen when the deletion was a mistake.
    """
    pipeline, repo, ledger = parts
    first = pipeline.process_email(_email("msg-original"))
    candidate_id = first.ingested_ids[0]
    repo.delete(candidate_id)
    ledger.retire_candidate(candidate_id, ["msg-original"], resume_hash=RESUME_HASH)

    changed = _email("msg-modified")
    changed.attachments[0].data = RESUME_BYTES + b" updated"
    changed.attachments[0].size = len(changed.attachments[0].data)
    again = pipeline.process_email(changed)

    assert again.attachments[0].status == "ingested"
    assert len(repo.records) == 1


def test_the_deleted_candidates_own_email_is_never_ingested_again(parts):
    """Gmail's search index lags behind the `Resumes/Deleted` label by a minute
    or more, so a poll inside that window re-fetches the retired email. The
    ledger tombstone — not the label — is what has to stop it."""
    pipeline, repo, ledger = parts
    candidate_id = pipeline.process_email(_email("msg-original")).ingested_ids[0]

    repo.delete(candidate_id)
    ledger.retire_candidate(candidate_id, ["msg-original"], resume_hash=RESUME_HASH)

    refetched = pipeline.process_email(_email("msg-original"))

    assert refetched.status == "suppressed"
    assert repo.records == {}, "the deleted candidate must not come back"


def test_a_message_whose_attachment_errored_is_not_reported_as_skipped(parts):
    """The runner labels `skipped` messages as processed. A failure must not be
    laundered into a skip, or the email is retired without ever being ingested."""
    pipeline, _, _ = parts
    pipeline.parser = type("Boom", (), {
        "parse_file": lambda self, data, filename: (_ for _ in ()).throw(RuntimeError("boom"))
    })()

    result = pipeline.process_email(_email("msg-broken"))

    assert result.attachments[0].status == "error"
    assert result.status == "error"


def test_the_auto_reply_goes_to_the_candidate_not_the_forwarder(parts, monkeypatch):
    """A forwarded resume must still acknowledge the applicant. Replying to the
    sender sends it to whoever forwarded the mail — often your own inbox."""
    pipeline, _, _ = parts
    monkeypatch.setattr("app.ingestion.pipeline.settings.auto_reply_enabled", True)
    monkeypatch.setattr("app.ingestion.pipeline.generate_contextual_reply",
                        lambda profile, email: "Thanks for applying.")

    sent = {}

    class FakeGmail:
        def send_reply(self, message_id, thread_id, to_addr, subject, body_text):
            sent["to"] = to_addr

    forwarded = _email("msg-forwarded")
    forwarded.from_addr = "recruiter@agency.com"      # not the candidate

    result = pipeline.process_email(forwarded, gmail=FakeGmail())

    assert result.status == "processed"
    assert sent["to"] == "recruiter@agency.com", "must reply to the address the email arrived from"


# --------------------------------------------------------------------------- #
#  A refusal is a finished message, not an unfinished one
# --------------------------------------------------------------------------- #
def test_a_nationality_refusal_leaves_a_finished_message_the_runner_can_file(parts):
    """The contract `mark_message_done` relies on to stop re-reading the CV.

    A résumé refused on nationality is permanent, so the message must come back
    `skipped` *carrying its attachment verdict* — that pair is what tells the
    runner to label it. Reported as an error instead, it would be left in the
    inbox and OCR'd again on every poll for ever.
    """
    from app.core.exceptions import ForeignNationalityError

    pipeline, repo, _ = parts

    def refuse(data, filename):
        raise ForeignNationalityError(
            "rejected: other nationality (Pakistan) [confidence 0.99]"
        )

    pipeline.parser.parse_file = refuse

    result = pipeline.process_email(_email("msg-usman"))

    assert result.status == "skipped"
    assert [a.status for a in result.attachments] == ["rejected_nationality"]
    assert repo.records == {}, "a refused CV must not become a candidate"


# --------------------------------------------------------------------------- #
#  Against the real ledger, not a stand-in for it
# --------------------------------------------------------------------------- #
def test_retire_candidate_runs_on_the_real_ledger():
    """The whole suite was green while every delete raised `AttributeError`.

    `retire_candidate` called `self.suppress_hash(...)`, a method the real
    `IngestLedger` does not have — but the fake in this file did, so nothing
    here ever touched the real one. In production the candidate document is
    dropped *before* the ledger call, so the crash left the record deleted,
    its emails never retired, and the next poll free to ingest it all over
    again.

    This exercises the real class against an in-memory collection, so a method
    that exists only on the stub cannot pass again.
    """
    from app.db.ledger import DELETED_SENTINEL, IngestLedger

    class Coll:
        def __init__(self):
            self.docs: dict = {}

        def delete_many(self, query):
            before = len(self.docs)
            self.docs = {
                k: d for k, d in self.docs.items()
                if not any(all(d.get(f) == v for f, v in c.items()) for c in query["$or"])
            }
            class R:
                deleted_count = before - len(self.docs)
            return R()

        def update_one(self, flt, update, upsert=False):
            doc = self.docs.setdefault(flt["_id"], dict(update.get("$setOnInsert") or {}))
            doc.update(update["$set"])

    coll = Coll()
    ledger = IngestLedger(collection=coll)

    written = ledger.retire_candidate("cand-1", ["msg-a", "msg-b"], resume_hash=RESUME_HASH)

    assert written == 2, "one tombstone per email, and none for the file"
    tombstones = [d for d in coll.docs.values() if d.get("suppressed")]
    assert {d["message_id"] for d in tombstones} == {"msg-a", "msg-b"}
    assert all(d["resume_hash"] == DELETED_SENTINEL for d in tombstones), (
        "a hash-keyed tombstone would block the file for ever"
    )


def test_the_real_ledger_has_no_hash_suppression_to_call():
    """Named so the next person to add one has to delete this test first.

    `suppress_hash` blocked a résumé by file hash, which is the one thing
    deletion must not do: a candidate removed by mistake could never be
    re-sent, from any address, with any version of their CV.
    """
    from app.db.ledger import IngestLedger

    assert not hasattr(IngestLedger, "suppress_hash")
    assert not hasattr(IngestLedger, "suppress_candidate")


def test_seen_message_ids_answers_for_a_whole_inbox_at_once():
    """Against the real ledger, so the bulk form cannot drift from the single one."""
    from app.db.ledger import IngestLedger

    class Coll:
        def __init__(self, known):
            self.known = set(known)
            self.queries: list = []

        def distinct(self, field, query):
            self.queries.append(query)
            wanted = set(query["message_id"]["$in"])
            return sorted(self.known & wanted)

    coll = Coll({"msg-b", "msg-d"})
    ledger = IngestLedger(collection=coll)

    assert ledger.seen_message_ids(["msg-a", "msg-b", "msg-c", "msg-d"]) == {"msg-b", "msg-d"}
    assert len(coll.queries) == 1, "one round trip, not one per message"


def test_seen_message_ids_chunks_a_very_large_mailbox():
    """Chunked so the query document cannot grow without bound."""
    from app.db.ledger import IngestLedger, _SEEN_CHUNK

    class Coll:
        def __init__(self):
            self.sizes: list[int] = []

        def distinct(self, _field, query):
            self.sizes.append(len(query["message_id"]["$in"]))
            return []

    coll = Coll()
    IngestLedger(collection=coll).seen_message_ids([f"m{i}" for i in range(_SEEN_CHUNK * 2 + 7)])

    assert max(coll.sizes) <= _SEEN_CHUNK
    assert sum(coll.sizes) == _SEEN_CHUNK * 2 + 7, "every id must be asked about exactly once"


def test_an_empty_inbox_asks_nothing():
    from app.db.ledger import IngestLedger

    class Coll:
        def distinct(self, *_a, **_k):
            raise AssertionError("no query should be issued for an empty list")

    assert IngestLedger(collection=Coll()).seen_message_ids([]) == set()
