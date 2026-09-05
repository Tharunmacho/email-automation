"""The ingestion pipeline — orchestrates one email end to end.

    Gmail message
        → detect (stage 1)
        → for each resume attachment:
              download → extract text (OCR fallback) → AI structure (stage 2)
              → dedup (hash / email / phone) → store file + insert Mongo record

Each stage is a small, independently-testable unit imported from its own module;
this class only wires them together and owns error handling + status reporting.
"""
from __future__ import annotations

import atexit
import concurrent.futures
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from app.ai.resume_parser import ResumeParser
from app.core.exceptions import (
    AIParseError,
    ForeignNationalityError,
    NotAResumeError,
    PipelineError,
    TextExtractionError,
    UnsupportedFileTypeError,
)
from app.core.models import (
    Attachment,
    CandidateProfile,
    CandidateRecord,
    EmailMessage,
    SourceEmail,
    StoredResume,
    utcnow,
)
from app.ai.reply_generator import generate_contextual_reply
from app.config import settings
from app.db.dedup import normalize_email, normalize_phone, sha256_hex
from app.db.ledger import NOT_A_RESUME_SENTINEL, IngestLedger
from app.db.repository import CandidateRepository
from app.extraction.jobs import JobContext, use_job_context
from app.ingestion.detector import detect
from app.ingestion.job_recorder import IngestionStateRecorder
from app.logging_config import get_logger
from app.assignment import assign_candidate
from app.notifications import notify_candidate_assigned, notify_candidate_rejected
from app.storage.base import StorageBackend
from app.storage.factory import get_storage_backend
from app.extraction.text_extractor import extract_text

log = get_logger(__name__)

# Below this confidence we keep the record but flag it for human review.
_MIN_CONFIDENCE = 0.55


@dataclass
class AttachmentResult:
    filename: str
    status: str                       # ingested | duplicate | suppressed | not_resume
                                      # | rejected_nationality | error
    candidate_id: Optional[str] = None
    detail: str = ""
    #: A reply was handed to the background sender — not that it has gone out.
    #: Sending is off the ingestion path, so when this result is returned the
    #: SMTP conversation has usually not started. The durable answer to "did
    #: this candidate get their reply" is `auto_reply_sent` on the candidate
    #: record, written only by a send that returned, and
    #: `flush_pending_auto_replies` is what makes sure it eventually becomes
    #: true.
    reply_queued: bool = False
    # What the Aadhaar / passport passes over the same bundle did, e.g.
    # "aadhaar p54=succeeded; passport p55=pending". Never affects `status`:
    # an unreadable passport does not make an ingested resume a failure.
    identity: str = ""


# The policy itself lives with the detector that decides it, so the parser can
# reach it without importing the pipeline. Re-exported under the old name
# because this is where the pipeline enforces it.
from app.extraction.resume_nationality import (  # noqa: E402
    refuse_foreign_candidate as _refuse_foreign_candidate,
)


# --------------------------------------------------------------------------- #
#  Auto-replies, off the ingestion path
# --------------------------------------------------------------------------- #
# Composing a reply is free — `generate_contextual_reply` is string templating,
# not a model call — but *sending* one is a full SMTP conversation: connect,
# STARTTLS, login, send, quit, each stage against a 15s timeout. Measured on one
# batch that is 2.89s, spent with the mail loop held open behind it, on the one
# step whose failure this pipeline deliberately swallows. Nothing downstream
# waits for it: the résumé is stored, the candidate assigned, the ledger
# written. So it belongs behind the return, not in front of it.
#
# What makes that safe rather than merely faster
# ----------------------------------------------
# Moving work to the background is a promise to notice when it does not happen.
# Three things keep that promise, and none of them is sufficient alone:
#
# 1. the sender retries a transient failure in place, with backoff;
# 2. a shutdown drains what is queued rather than dropping it;
# 3. `flush_pending_auto_replies` sweeps every ingested candidate still showing
#    `auto_reply_sent=False` and sends what is owed — which is what covers the
#    redeploy mid-send, the SMTP outage, and the process that died holding a
#    queue.
#
# The flag is the contract. It is written only by a send that returned, so
# "ingested in the database" and "has had their reply" can be reconciled at any
# time by anyone, without reference to what this process happens to remember.
#
# One worker, and not a knob.
# ---------------------------
# The instinct is a pool of four. It would be wrong: the Gmail client sends
# through a `googleapiclient` Resource built on httplib2, which is not
# thread-safe, and two concurrent sends through one client is corruption rather
# than throughput. The SMTP client opens a fresh connection per call and would
# be fine, but the pipeline cannot see which of the two it was handed. Serial is
# also enough — taking the send off the critical path is the whole win, and one
# worker keeps replies in the order they were earned. A setting whose only
# non-default value is unsafe should not exist, so this is a constant.
_REPLY_SENDERS = 1

_reply_pool: "concurrent.futures.ThreadPoolExecutor | None" = None
_reply_pool_lock = threading.Lock()


def _sender() -> "concurrent.futures.ThreadPoolExecutor":
    """The background sender, built on first use and shared process-wide."""
    global _reply_pool
    with _reply_pool_lock:
        if _reply_pool is None:
            _reply_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_REPLY_SENDERS, thread_name_prefix="auto-reply",
            )
        return _reply_pool


def _drain_replies() -> None:
    """At shutdown, finish what is queued — within reason.

    Bounded rather than unlimited, and that is safe for one reason: a reply this
    drops keeps `auto_reply_sent=False` on its candidate, and the sweep sends it
    on the next cycle. Waiting for an unbounded backlog instead would hold a
    container stop open until the deploy timed out and killed it at a worse
    moment than this one.
    """
    global _reply_pool
    with _reply_pool_lock:
        pool, _reply_pool = _reply_pool, None
    if pool is None:
        return
    budget = max(0.0, float(getattr(settings, "auto_reply_drain_seconds", 0) or 0))
    finished = threading.Event()

    def _shutdown() -> None:
        try:
            pool.shutdown(wait=True)
        finally:
            finished.set()

    threading.Thread(target=_shutdown, name="auto-reply-drain", daemon=True).start()
    if not finished.wait(budget):
        log.warning(
            "Auto-reply sender still had work after %.0fs; the rest keeps "
            "auto_reply_sent=False and goes out on the next sweep", budget,
        )


# Registered once, at import, rather than each time a pool is built. The sweep
# drains the pool and drops it — see `flush_pending_auto_replies` — so a pool is
# built and discarded on every cycle, and registering from inside `_sender`
# accumulated a duplicate handler each time.
atexit.register(_drain_replies)


def _reply_email(source, fallback_subject: str = "") -> EmailMessage:
    """The message a reply is threaded onto, rebuilt from what was stored.

    The sweep holds a candidate record rather than the mail it arrived on, and
    `send_reply` needs the message and thread ids to keep the reply inside the
    same conversation instead of opening a new one in the candidate's inbox.
    """
    return EmailMessage(
        message_id=source.message_id,
        thread_id=source.thread_id,
        from_addr=source.from_addr,
        from_name=source.from_name,
        subject=source.subject or fallback_subject,
        date=source.received_date,
    )


def _send_auto_reply(
    repo: CandidateRepository, gmail, candidate_id: str,
    profile: CandidateProfile, email: EmailMessage, reply_to: str,
) -> bool:
    """Compose and send one reply, retrying a transient failure. Never raises.

    Runs on the sender thread, so it touches only what it was handed plus the
    repository, whose driver is thread-safe. `profile` and `email` are read and
    never mutated here, and the pipeline does not write to them after handing
    them over.

    A retry can in principle double-send: if the server accepted the message and
    the connection then broke, the exception looks identical to one from a mail
    that never landed. That is the right way round to be wrong — the ask is that
    an ingested candidate is replied to, and a duplicate courtesy mail is a
    smaller failure than silence.
    """
    attempts = max(1, int(getattr(settings, "auto_reply_send_attempts", 1) or 1))
    backoff = max(0.0, float(getattr(settings, "auto_reply_retry_backoff_seconds", 0) or 0))
    last = ""

    for attempt in range(1, attempts + 1):
        try:
            body = generate_contextual_reply(profile, email)
            gmail.send_reply(
                message_id=email.message_id,
                thread_id=email.thread_id,
                to_addr=reply_to,
                subject=email.subject,
                body_text=body,
            )
            # Only here, and only now. This flag is what the sweep reads to
            # decide who is still owed one, so it has to mean "a send returned",
            # never "a send was attempted".
            repo.mark_auto_reply_sent(candidate_id)
            log.info(
                "Auto-reply sent to candidate %s at %s%s",
                candidate_id, reply_to,
                " (attempt %d)" % attempt if attempt > 1 else "",
            )
            return True
        except Exception as exc:  # noqa: BLE001 — a failed reply is not a failed ingest
            last = "%s: %s" % (type(exc).__name__, exc)
            if attempt < attempts:
                pause = backoff * (2 ** (attempt - 1))
                log.warning(
                    "Auto-reply to %s for candidate %s failed (%s); retrying in %.1fs",
                    reply_to, candidate_id, last, pause,
                )
                time.sleep(pause)

    # Recorded rather than lost. The count is what eventually stops the sweep
    # coming back to an address that cannot receive mail at all.
    try:
        count = repo.record_auto_reply_failure(candidate_id, last)
    except Exception as exc:  # noqa: BLE001 — never let bookkeeping raise here
        log.warning("Could not record the failed auto-reply for %s: %s", candidate_id, exc)
        count = 0
    log.warning(
        "Auto-reply to %s for candidate %s failed after %d attempt(s) (%s); "
        "%d failure(s) recorded, the sweep will try again",
        reply_to, candidate_id, attempts, last, count,
    )
    return False


def queue_auto_reply(
    repo: CandidateRepository, gmail, candidate_id: str,
    profile: CandidateProfile, email: EmailMessage, reply_to: str,
) -> bool:
    """Hand one reply to the background sender. Returns whether it was queued."""
    if not (gmail and hasattr(gmail, "send_reply") and reply_to):
        log.info(
            "Auto-reply for candidate %s not queued: no send-capable mail client "
            "or no address to reply to; the sweep will send it once there is one",
            candidate_id,
        )
        return False
    _sender().submit(
        _send_auto_reply, repo, gmail, candidate_id, profile, email, reply_to,
    )
    return True


def flush_pending_auto_replies(
    repo: "CandidateRepository | None" = None, gmail=None, limit: "int | None" = None,
) -> dict:
    """Send every reply an ingested candidate is still owed.

    This is the guarantee. Everything above it is an optimisation on top: the
    background sender makes the common case fast, and this makes the promise
    true regardless of what the background sender managed. A candidate in the
    database with `status="ingested"` and `auto_reply_sent=False` is work still
    outstanding, whatever went wrong — a redeploy mid-send, SMTP refusing
    connections for an hour, a process killed holding a queue.

    Runs sequentially and in place rather than through the pool: every caller is
    already a background thread (the inline poll, or a beat task) and has
    nothing to gain from handing the work on. Never raises — a sweep that cannot
    run is a log line, not a failed poll cycle.
    """
    if not settings.auto_reply_enabled:
        return {"sent": 0, "failed": 0, "pending": 0, "reason": "auto-reply disabled"}

    from app.email_client import get_email_client

    # Finish what this process already has in hand before asking who is owed.
    #
    # Without this the sweep double-sends, and not rarely — systematically. The
    # inline poll calls it immediately after the batch, which is exactly when
    # the replies that batch queued are still working through a single-worker
    # pool at a few seconds each. Every one of those still reads
    # `auto_reply_sent=False`, because the flag is written *after* the send
    # returns, so the sweep would pick up a reply already in flight and mail the
    # candidate twice.
    #
    # Draining first collapses the window: every locally-queued send has either
    # written its flag or recorded its failure by the time the query runs, so
    # what comes back is genuinely outstanding rather than merely unfinished.
    _drain_replies()

    try:
        repo = repo or CandidateRepository()
        owed = repo.find_awaiting_auto_reply(
            limit=int(limit or settings.auto_reply_sweep_limit),
            max_attempts=int(settings.auto_reply_max_attempts),
            grace_seconds=int(getattr(settings, "auto_reply_grace_seconds", 0) or 0),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not look up candidates awaiting an auto-reply: %s", exc)
        return {"sent": 0, "failed": 0, "pending": 0, "error": str(exc)}

    if not owed:
        return {"sent": 0, "failed": 0, "pending": 0}

    try:
        gmail = gmail or get_email_client()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "%d candidate(s) are owed an auto-reply but no mail client could be "
            "built (%s); they keep auto_reply_sent=False and stay in the queue",
            len(owed), exc,
        )
        return {"sent": 0, "failed": 0, "pending": len(owed), "error": str(exc)}

    log.info("Auto-reply sweep: %d ingested candidate(s) still owed a reply", len(owed))
    sent = 0
    failed = 0
    for record in owed:
        source = record.source_email
        if not (source and source.from_addr):
            continue  # the query asked for one; a record without it is not ours
        if _send_auto_reply(
            repo, gmail, record.id, record.profile,
            _reply_email(source), source.from_addr,
        ):
            sent += 1
        else:
            failed += 1

    log.info("Auto-reply sweep finished: sent=%d failed=%d", sent, failed)
    return {"sent": sent, "failed": failed, "pending": max(0, len(owed) - sent - failed)}


@dataclass
class ProcessResult:
    message_id: str
    status: str                       # processed | skipped | error
    reason: str = ""
    attachments: List[AttachmentResult] = field(default_factory=list)

    @property
    def ingested_ids(self) -> List[str]:
        return [a.candidate_id for a in self.attachments if a.status == "ingested" and a.candidate_id]


class IngestionPipeline:
    def __init__(
        self,
        repository: Optional[CandidateRepository] = None,
        storage: Optional[StorageBackend] = None,
        parser: Optional[ResumeParser] = None,
        ledger: Optional[IngestLedger] = None,
    ):
        self.repo = repository or CandidateRepository()
        self.storage = storage or get_storage_backend()
        self.parser = parser or ResumeParser()
        self.ledger = ledger or IngestLedger()

    # ---------------------------------------------------------------- #
    def process_email(self, email: EmailMessage, gmail=None) -> ProcessResult:
        """Process a fully-populated EmailMessage. ``gmail`` (optional) is used to
        lazily download attachment bytes if they aren't already present."""
        # The user deleted the candidate this email produced. The Gmail label
        # that hides it needs a minute to reach Gmail's search index, so this
        # check — not the label — is what stops a re-ingest in the meantime.
        if self.ledger.is_message_suppressed(email.message_id):
            log.info("Message %s belongs to a deleted candidate — not re-ingesting", email.message_id)
            return ProcessResult(
                email.message_id, "suppressed", "candidate was deleted by a user",
            )

        # Idempotency. The ledger is checked first because it outlives the
        # candidate record: deleting a candidate used to erase the only proof
        # that this message had been handled, so the next poll re-ingested it.
        if self.ledger.message_seen(email.message_id):
            # Said out loud, because this skip can outlive the record it is
            # protecting. The ledger deliberately survives a deleted candidate,
            # so a migration that moves the candidates and leaves the ledger
            # behind produces a message that is skipped for ever with nothing
            # in the CRM to show for it. Silent, that reads as "the poll found
            # nothing"; named, it points at `scripts/clean_ledger_and_verify.py`.
            log.info(
                "Message %s skipped: the ledger already records it as handled. "
                "If no candidate exists for it, the ledger entry is orphaned.",
                email.message_id,
            )
            return ProcessResult(email.message_id, "skipped", "already processed (ledger)")

        existing = self.repo.find_by_message_id(email.message_id)
        if existing:
            log.info(
                "Message %s skipped: candidate %s was already created from it",
                email.message_id, existing.id,
            )
            return ProcessResult(email.message_id, "skipped", "already processed")

        detection = detect(email)
        if not detection.is_candidate:
            # Written down so the answer is reached once. Nothing labels a
            # non-résumé email — it is somebody's ordinary mail and we leave it
            # where it is — so it stays in the inbox and comes back in every
            # future search. Without a row here the poll re-downloads and
            # re-detects the whole accumulated inbox on every pass, which is
            # what stops the ingestion scaling with the mailbox.
            #
            # Keyed by a sentinel rather than a file hash: there is no file, and
            # the row must never match one that arrives later on a real CV.
            try:
                self.ledger.record(
                    email.message_id, NOT_A_RESUME_SENTINEL, None,
                    "not_a_resume", detection.reason,
                )
            except Exception as err:  # noqa: BLE001 — bookkeeping must not fail a poll
                log.warning("Could not record the non-résumé verdict for %s: %s",
                            email.message_id, err)
            return ProcessResult(email.message_id, "skipped", f"not a resume email: {detection.reason}")

        results: List[AttachmentResult] = []
        for att in detection.resume_attachments:
            results.append(self._process_attachment(email, att, gmail))

        if any(r.status == "ingested" for r in results):
            overall = "processed"
        elif any(r.status == "error" for r in results):
            # Not "skipped": the runner labels skipped messages as processed, and
            # a message that only failed must stay unlabelled so the next poll
            # retries it once the failure is fixed.
            overall = "error"
        else:
            overall = "skipped"
        return ProcessResult(email.message_id, overall, detection.reason, results)

    # ---------------------------------------------------------------- #
    def _process_attachment(self, email: EmailMessage, att: Attachment, gmail) -> AttachmentResult:
        # The moment this résumé entered the system. Taken before any of the
        # expensive work rather than after it, because OCR on a scanned bundle
        # can run for minutes and the SLA clock is measuring how long a
        # candidate has been waiting, not how long the parser took.
        arrived_at = utcnow()
        try:
            data = att.data
            if data is None:
                if gmail is None:
                    raise PipelineError("Attachment bytes not loaded and no Gmail client supplied.")
                data = gmail.download_attachment(email.message_id, att)

            resume_hash = sha256_hex(data)

            # (0) The user deleted a candidate that came from this exact file.
            #     Never bring it back, however many times the mail is re-fetched.
            if self.ledger.is_suppressed(resume_hash):
                return AttachmentResult(
                    att.filename, "suppressed",
                    detail="previously deleted by a user — not re-ingested",
                )

            # (1) Exact-duplicate short-circuit before any expensive work.
            dup = self.repo.find_by_resume_hash(resume_hash)
            if dup:
                self.ledger.record(email.message_id, resume_hash, dup.id, "duplicate")
                return AttachmentResult(att.filename, "duplicate", dup.id, "identical file already ingested")

            # Same file already seen under a different message id.
            seen = self.ledger.find_by_hash(resume_hash)
            if seen and seen.candidate_id:
                return AttachmentResult(
                    att.filename, "duplicate", seen.candidate_id,
                    "identical file already ingested (ledger)",
                )

            # (2) Extract text and AI structure.
            #     Under a job context, so that the résumé OCR — which happens
            #     several calls down inside the extractor — is submitted with an
            #     idempotency key derived from *this* mail, and lands on an
            #     ingestion row the reconciler can find if it does not finish.
            recorder = IngestionStateRecorder(
                email.message_id,
                att.attachment_id,
                filename=att.filename,
                sha256=resume_hash,
            )
            context = JobContext(
                account_id=recorder.account_id,
                message_id=email.message_id,
                attachment_id=att.attachment_id,
                recorder=recorder,
            )
            with use_job_context(context):
                if hasattr(self.parser, "parse_file"):
                    profile, extracted = self.parser.parse_file(data, att.filename)
                else:
                    extracted = extract_text(data, att.filename)
                    if extracted.is_resume is False:
                        raise NotAResumeError(
                            f"Attachment '{att.filename}' is not a resume: "
                            f"{extracted.classification_reason}"
                        )
                    # Before the AI structuring below, not after it: a candidate
                    # this desk cannot place should cost neither the résumé
                    # endpoint (already declined inside the extractor) nor a
                    # model call here.
                    _refuse_foreign_candidate(att.filename, extracted)
                    # (3) AI structuring — résumé pages only, so a 30-page
                    #     bundle costs the two pages that hold the CV, not all
                    #     thirty.
                    hint = f"Subject: {email.subject}; From: {email.from_name or email.from_addr}"
                    profile = self.parser.parse(extracted.resume_text, hint=hint)

            # The parser-supplied branch above extracts and structures in one
            # call, so its refusal lands here. Re-asking a decision already made
            # and carried on `extracted` — never recomputed, so the answer
            # cannot drift between the two places it is enforced.
            _refuse_foreign_candidate(att.filename, extracted)

            if not profile.is_resume:
                reason = (profile.additional_info or {}).get("rejection_reason") \
                    or getattr(extracted, "classification_reason", "") \
                    or "content is not a resume"
                raise NotAResumeError(f"Attachment '{att.filename}' is not a resume: {reason}")

            if not profile.email and not profile.phone:
                raise NotAResumeError(
                    f"Attachment '{att.filename}' is not a valid candidate resume (missing candidate email & phone in resume)"
                )

            # A contact detail alone is not a resume — every letterhead has one.
            # When OCR fails and the heuristic parser scrapes a phone number off
            # a hall ticket, this is what stops it becoming a candidate.
            if profile.confidence < settings.min_ingest_confidence:
                raise NotAResumeError(
                    f"Attachment '{att.filename}' does not look like a resume "
                    f"(confidence {profile.confidence:.2f} < {settings.min_ingest_confidence:.2f}; "
                    f"extraction may have failed)"
                )

            # (4) Deduplication strictly using Candidate Email & Phone extracted from the resume.
            email_key = normalize_email(profile.email)
            phone_key = normalize_phone(profile.phone)

            # A `was_deleted` gate used to sit here, refusing any résumé whose
            # email, phone or hash matched a hard-deleted candidate — and
            # calling `suppress_hash`, which no longer exists, so every delete
            # raised before it could tombstone anything.
            #
            # It is gone because it enforced the opposite of the rule this desk
            # runs on: a résumé re-sent after a deletion must ingest as a new
            # candidate. Deleting from the CRM is how a mistake is corrected,
            # and a gate keyed on the person's own email and phone made that
            # correction permanent — the candidate could never apply again, from
            # any address, with any version of their CV.
            #
            # What still holds the line is `retire_candidate`: the emails the
            # deleted candidate came from are tombstoned by message id, so the
            # poll cannot bring the same mail back while Gmail's index catches
            # up. The file is deliberately freed.
            #
            # The removal is deliberately limited to this path. `repo.was_deleted`
            # still gates WhatsApp intake and manual upload, and is meant to:
            # both of those are a person acting deliberately at a keyboard, who
            # can be told "this candidate was deleted" and do something about
            # it. A résumé arriving by mail has nobody to tell — refusing it
            # silently is how a deletion turns into a ban. Same helper, three
            # entry points, one of which now answers differently on purpose.
            person_dup = self.repo.find_by_email_or_phone(email_key, phone_key)
            if person_dup:
                self.ledger.record(email.message_id, resume_hash, person_dup.id, "duplicate")
                return AttachmentResult(
                    att.filename, "duplicate", person_dup.id,
                    "same candidate (email/phone in resume) already exists",
                )

            # (5) Store original file + insert record.
            record = self._build_record(
                email, att, data, resume_hash, extracted, profile, email_key, phone_key,
                ingested_at=arrived_at,
            )
            if profile.confidence < _MIN_CONFIDENCE:
                record.status = "needs_review"

            self._store_file(record, data, att)
            candidate_id = self.repo.insert(record)

            # The insert may have landed on somebody who is already here.
            #
            # `record.id` is a uuid minted moments ago in `_build_record`, so a
            # different id back means a unique index fired and `insert` resolved
            # it to the candidate that owns the address, passport or file. The
            # (4) check above cannot prevent that on its own: one application
            # delivered to two of the polled mailboxes is two messages, and
            # `ingestion_max_workers` runs them together, so both pass the
            # lookup before either inserts.
            #
            # Returning here rather than carrying on is the point. Everything
            # below acts on a *new* candidate — an allocation to a recruiter and
            # an auto-reply to a real person — and doing that a second time for
            # one applicant is exactly what the duplicate was reported as.
            if candidate_id != record.id:
                # The file was stored immediately before the atomic insert
                # decision. This request lost that race, so its object is not
                # referenced by the existing candidate and must be removed.
                try:
                    self.storage.delete(record.resume.storage_key)
                except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                    log.warning(
                        "Could not remove duplicate resume object %s: %s",
                        record.resume.storage_key,
                        exc,
                    )
                self.ledger.record(email.message_id, resume_hash, candidate_id, "duplicate")
                log.info(
                    "Attachment '%s' resolved to existing candidate %s on insert; "
                    "no second allocation or auto-reply",
                    att.filename, candidate_id,
                )
                return AttachmentResult(
                    att.filename, "duplicate", candidate_id,
                    "same candidate already exists (resolved at insert)",
                )

            self.ledger.record(email.message_id, resume_hash, candidate_id, "ingested")
            recorder.storage_key = record.resume.storage_key
            recorder.link_candidate(candidate_id)

            # (5.2) The other documents in the same bundle. The résumé is the
            #       only thing a candidate record needs, so this runs *after*
            #       the insert and can never cost one: an Aadhaar page that
            #       will not read is logged, not raised.
            identity = self._extract_identity_documents(
                email, att, data, extracted, resume_hash,
                record.resume.storage_key, candidate_id,
            )

            # (5.5) Auto-assign candidate to active staff & trigger push notifications
            if not self._allocate(candidate_id, profile):
                self._announce(candidate_id, profile)

            # (6) Contextual auto-reply, queued rather than sent.
            #
            # The candidate is already stored, already assigned, already in the
            # ledger; nothing below depends on the reply, and the SMTP round
            # trip it costs was 2.89s of a 37.75s batch spent with the mail loop
            # held open. So it is handed to the background sender and the batch
            # moves on. See the module header for what makes that safe: the
            # sender retries, shutdown drains, and `flush_pending_auto_replies`
            # sweeps up anything still owed.
            reply_queued = False
            # Never on a record we are unsure about. A reply is irreversible and
            # goes to a real person; a profile heading for human review has not
            # earned one. The sweep applies the same rule — it asks only for
            # candidates whose status is "ingested" — so a record that is later
            # promoted out of review is not silently mailed by the catch-up.
            if settings.auto_reply_enabled and record.status != "needs_review":
                # Reply to the address the mail actually arrived from.
                reply_to = email.from_addr or email_key
                reply_queued = queue_auto_reply(
                    self.repo, gmail, candidate_id, profile, email, reply_to,
                )

            return AttachmentResult(
                att.filename,
                "ingested",
                candidate_id,
                f"confidence={profile.confidence:.2f}",
                reply_queued=reply_queued,
                identity=identity,
            )

        except ForeignNationalityError as exc:
            # A permanent, deliberate refusal — the document read perfectly well
            # and belongs to somebody this desk does not recruit. Nothing was
            # uploaded and nothing is stored; the mail is still labelled by the
            # caller so it is not fetched again on every poll for ever.
            log.info("Rejected on nationality: %s", exc)
            self._announce_rejection(email, att, exc)
            return AttachmentResult(
                att.filename, "rejected_nationality", detail=str(exc),
            )
        except (NotAResumeError,) as exc:
            log.info("Skipping attachment: %s", exc)
            return AttachmentResult(att.filename, "not_resume", detail=str(exc))
        except UnsupportedFileTypeError as exc:
            # Permanent, not retryable, and the distinction decides whether the
            # mail ever gets labelled done. A file type we have no reader for
            # will not become readable on the next poll, so reporting it as an
            # error left the message unlabelled and re-fetched forever. Stage 1
            # now admits attachments on their MIME type alone, which is what
            # makes an unreadable type reachable here at all.
            log.info("Attachment '%s' is of a type we cannot read: %s", att.filename, exc)
            return AttachmentResult(att.filename, "not_resume", detail=str(exc))
        except (TextExtractionError, AIParseError) as exc:
            log.warning("Attachment failed (%s): %s", att.filename, exc)
            return AttachmentResult(att.filename, "error", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — never let one attachment kill the batch
            log.exception("Unexpected error on attachment %s", att.filename)
            return AttachmentResult(att.filename, "error", detail=str(exc))

    def _extract_identity_documents(
        self,
        email: EmailMessage,
        att: Attachment,
        data: bytes,
        extracted,
        resume_hash: str,
        storage_key: str,
        candidate_id: str,
    ) -> str:
        """Read the Aadhaar and passport out of the same bundle, if they are there.

        Returns a one-line summary for the attachment result. Everything here is
        best-effort by design: the candidate is already in Mongo, and no failure
        of a supporting document is allowed to undo that.
        """
        if not settings.multipass_extraction_enabled:
            return ""
        if not settings.veris_ocr_api_key:
            # Aadhaar and passport extraction have no local fallback — there is
            # no Tesseract equivalent for an MRZ — so without a key there is
            # nothing to attempt and no row worth opening.
            return ""

        page_texts = [p.text for p in getattr(extracted, "pages", []) or []]
        if not page_texts:
            return ""

        try:
            from app.ingestion.multipass import MultipassExtractor

            result = MultipassExtractor().run(
                page_texts,
                data,
                message_id=email.message_id,
                attachment_id=att.attachment_id,
                filename=att.filename,
                sha256=resume_hash,
                storage_key=storage_key,
                candidate_id=candidate_id,
            )
            if not result.passes:
                return ""
            summary = result.summary()
            log.info("Identity documents for candidate %s: %s", candidate_id, summary)
            return summary
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Identity extraction failed for candidate %s (%s): %s",
                candidate_id, att.filename, exc,
            )
            return f"identity extraction failed: {exc}"

    def _allocate(self, candidate_id: str, profile: CandidateProfile) -> bool:
        """Assign the new candidate. Returns whether anyone was told about it."""
        if not settings.auto_assign_enabled:
            return False
        try:
            import app.assignment
            from app.notifications import notify_candidate_assigned

            result = app.assignment.assign_candidate(candidate_id, profile, repo=self.repo)
            if getattr(result, "assigned", False):
                notify_candidate_assigned(
                    getattr(result, "staff_id", ""),
                    {
                        "id": candidate_id,
                        "full_name": profile.full_name,
                        "email": profile.email,
                    },
                    staff_name=getattr(result, "staff_name", None),
                )
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto-allocation step failed for candidate %s: %s", candidate_id, exc)
        return False

    def _announce_rejection(self, email, att, exc: ForeignNationalityError) -> None:
        """Tell the admins a CV arrived and was turned away.

        The only trace this refusal leaves where anybody looks: there is no
        candidate row to find, by design. Best-effort like every other
        announcement here — a missed notification must not turn a deliberate
        refusal into a failed batch.
        """
        verdict = exc.verdict if isinstance(exc.verdict, dict) else {}
        try:
            notify_candidate_rejected(
                reason=str(exc),
                filename=att.filename,
                from_addr=getattr(email, "from_addr", "") or "",
                country=str(verdict.get("country") or ""),
            )
        except Exception as note_exc:  # noqa: BLE001
            log.debug("Could not announce the rejection of %s: %s", att.filename, note_exc)

    def _announce(self, candidate_id: str, profile: CandidateProfile) -> None:
        """Tell the open dashboards that a candidate just landed.

        The allocation notification already carries this for the usual path, so
        this is the case where nothing was allocated — no active staff, auto
        assignment switched off, the balancer erroring. The candidate is in
        Mongo either way, and the queue on screen has to say so without anybody
        pressing reload.
        """
        try:
            from app.api import websocket as ws

            ws.publish_event(
                ws.candidate_ingested_event(
                    {
                        "id": candidate_id,
                        "full_name": profile.full_name,
                        "email": profile.email,
                    },
                    staff_name=None,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a missed toast is not an error
            log.debug("Could not announce candidate %s: %s", candidate_id, exc)

    # ---------------------------------------------------------------- #
    def _build_record(
        self,
        email: EmailMessage,
        att: Attachment,
        data: bytes,
        resume_hash: str,
        extracted,
        profile: CandidateProfile,
        email_key: Optional[str],
        phone_key: Optional[str],
        ingested_at: Optional[datetime] = None,
    ) -> CandidateRecord:
        candidate_id = uuid.uuid4().hex
        storage_key = self._storage_key(candidate_id, att.filename)
        stored = StoredResume(
            original_filename=att.filename,
            mime_type=att.mime_type,
            size=len(data),
            sha256=resume_hash,
            storage_backend=self.storage.name,
            storage_key=storage_key,
            extraction_method=extracted.method,
            ocr_used=extracted.ocr_used,
        )
        source = SourceEmail(
            message_id=email.message_id,
            thread_id=email.thread_id,
            from_addr=email.from_addr,
            from_name=email.from_name,
            to_addr=email.to_addr,
            subject=email.subject,
            received_date=email.date,
        )
        return CandidateRecord(
            id=candidate_id,
            profile=profile,
            resume=stored,
            source_email=source,
            email_key=email_key,
            phone_key=phone_key,
            resume_hash=resume_hash,
            status="ingested",
            raw_ocr=profile.raw_ocr if getattr(profile, "raw_ocr", None) else None,
            # Extraction and structuring are done by the time this is called, so
            # `processed_at` is now; `ingested_at` is when the file arrived, which
            # is however long ago the parser started.
            ingested_at=ingested_at or utcnow(),
            processed_at=utcnow(),
        )

    def _storage_key(self, candidate_id: str, filename: str) -> str:
        now = datetime.now(timezone.utc)
        safe = filename.replace("/", "_").replace("\\", "_")
        return f"{now:%Y/%m}/{candidate_id}_{safe}"

    def _store_file(self, record: CandidateRecord, data: bytes, att: Attachment) -> None:
        """Store the original upload, and confirm it is really there.

        The file the candidate sent is the one artefact of this whole pipeline
        that cannot be recreated: the parsed profile can be re-derived, the OCR
        can be re-run, but a résumé nobody kept is gone the moment the mail is
        filed. A save that quietly did nothing — a full disk, a GridFS write
        rejected — would otherwise produce a candidate record whose download
        button can only fail, which is exactly what a recruiter discovers at the
        moment they need the file.

        So the write is read back. Failing here aborts the ingestion, and the
        message is left unlabelled for the next poll to retry.
        """
        key = record.resume.storage_key
        self.storage.save(key, data, content_type=att.mime_type)

        if not self.storage.exists(key):
            raise PipelineError(
                f"Stored '{att.filename}' to {self.storage.name}:{key} but it is not "
                f"there when read back — refusing to create a candidate with no file."
            )
