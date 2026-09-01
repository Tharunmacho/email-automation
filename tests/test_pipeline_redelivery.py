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
        """Mirrors IngestLedger: message and hash tombstones both survive."""
        self.rows = [r for r in self.rows
                     if r["candidate_id"] != candidate_id
                     and r["resume_hash"] != resume_hash
                     and r["message_id"] not in message_ids]
        for mid in message_ids:
            self.rows.append({"message_id": mid, "resume_hash": "__deleted__",
                              "candidate_id": candidate_id, "status": "deleted",
                              "suppressed": True})
        if resume_hash:
            self.rows.append({"message_id": "__manual__", "resume_hash": resume_hash,
                              "candidate_id": candidate_id, "status": "deleted",
                              "suppressed": True})
        return len(message_ids) + int(bool(resume_hash))

    def suppress_hash(self, resume_hash, reason="deleted by user"):
        self.rows.append({"message_id": "__manual__", "resume_hash": resume_hash,
                          "candidate_id": None, "status": reason, "suppressed": True})


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


def test_same_file_from_a_new_email_stays_deleted(parts):
    pipeline, repo, ledger = parts
    first = pipeline.process_email(_email("msg-original"))
    candidate_id = first.ingested_ids[0]

    # What DELETE /candidates/{id} does: drop the record, retire its emails.
    repo.delete(candidate_id)
    ledger.retire_candidate(candidate_id, ["msg-original"], resume_hash=RESUME_HASH)

    # A new email carrying the identical file must not recreate the candidate.
    again = pipeline.process_email(_email("msg-sent-again"))

    assert again.status == "skipped"
    assert again.attachments[0].status == "suppressed"
    assert repo.records == {}


def test_modified_resume_with_same_contact_stays_deleted(parts):
    pipeline, repo, ledger = parts
    first = pipeline.process_email(_email("msg-original"))
    candidate_id = first.ingested_ids[0]
    repo.delete(candidate_id)
    ledger.retire_candidate(candidate_id, ["msg-original"], resume_hash=RESUME_HASH)

    changed = _email("msg-modified")
    changed.attachments[0].data = RESUME_BYTES + b" updated"
    changed.attachments[0].size = len(changed.attachments[0].data)
    again = pipeline.process_email(changed)

    assert again.attachments[0].status == "suppressed"
    assert repo.records == {}


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
