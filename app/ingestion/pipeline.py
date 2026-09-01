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
    reply_sent: bool = False
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

            # A person explicitly removed from the CRM must stay removed even
            # when the same mailbox later receives a renamed or slightly
            # modified resume with a different file hash.
            was_deleted = getattr(self.repo, "was_deleted", None)
            if callable(was_deleted) and was_deleted(
                email_key=email_key,
                phone_key=phone_key,
                resume_hash=resume_hash,
                message_id=email.message_id,
            ):
                self.ledger.suppress_hash(resume_hash)
                return AttachmentResult(
                    att.filename,
                    "suppressed",
                    detail="candidate was previously deleted from the CRM",
                )

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

            # (6) Contextual Auto-Reply if enabled.
            reply_sent = False
            # Never on a record we are unsure about. A reply is irreversible and
            # goes to a real person; a profile heading for human review has not
            # earned one.
            if settings.auto_reply_enabled and record.status != "needs_review":
                try:
                    reply_text = generate_contextual_reply(profile, email)
                    # Send reply directly to the sender address where the email arrived from.
                    reply_to = email.from_addr or email_key
                    if gmail and hasattr(gmail, "send_reply") and reply_to:
                        gmail.send_reply(
                            message_id=email.message_id,
                            thread_id=email.thread_id,
                            to_addr=reply_to,
                            subject=email.subject,
                            body_text=reply_text,
                        )
                        reply_sent = True
                        self.repo.mark_auto_reply_sent(candidate_id)
                        log.info(
                            "Auto-reply sent to candidate %s at %s (mail arrived from %s)",
                            candidate_id, reply_to, email.from_addr,
                        )
                    else:
                        log.info("Auto-reply generated for candidate %s (reply_sent=False, Gmail client not connected)", candidate_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Failed to send auto-reply to %s: %s", email.from_addr, exc)

            return AttachmentResult(
                att.filename,
                "ingested",
                candidate_id,
                f"confidence={profile.confidence:.2f}",
                reply_sent=reply_sent,
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
