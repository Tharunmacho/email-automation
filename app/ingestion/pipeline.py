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
)
from app.ai.reply_generator import generate_contextual_reply
from app.config import settings
from app.db.dedup import normalize_email, normalize_phone, sha256_hex
from app.db.ledger import IngestLedger
from app.db.repository import CandidateRepository
from app.ingestion.detector import detect
from app.logging_config import get_logger
from app.storage.base import StorageBackend
from app.storage.factory import get_storage_backend
from app.extraction.text_extractor import extract_text

log = get_logger(__name__)

# Below this confidence we keep the record but flag it for human review.
_MIN_CONFIDENCE = 0.55


@dataclass
class AttachmentResult:
    filename: str
    status: str                       # ingested | duplicate | suppressed | not_resume | error
    candidate_id: Optional[str] = None
    detail: str = ""
    reply_sent: bool = False


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
            return ProcessResult(email.message_id, "skipped", "already processed (ledger)")

        existing = self.repo.find_by_message_id(email.message_id)
        if existing:
            return ProcessResult(email.message_id, "skipped", "already processed")

        detection = detect(email)
        if not detection.is_candidate:
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
            if hasattr(self.parser, "parse_file"):
                profile, extracted = self.parser.parse_file(data, att.filename)
            else:
                extracted = extract_text(data, att.filename)
                if extracted.is_resume is False:
                    raise NotAResumeError(
                        f"Attachment '{att.filename}' is not a resume: "
                        f"{extracted.classification_reason}"
                    )
                # (3) AI structuring — résumé pages only, so a 30-page bundle
                #     costs the two pages that hold the CV, not all thirty.
                hint = f"Subject: {email.subject}; From: {email.from_name or email.from_addr}"
                profile = self.parser.parse(extracted.resume_text, hint=hint)

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

            person_dup = self.repo.find_by_email_or_phone(email_key, phone_key)
            if person_dup:
                self.ledger.record(email.message_id, resume_hash, person_dup.id, "duplicate")
                return AttachmentResult(
                    att.filename, "duplicate", person_dup.id,
                    "same candidate (email/phone in resume) already exists",
                )

            # (5) Store original file + insert record.
            record = self._build_record(
                email, att, data, resume_hash, extracted, profile, email_key, phone_key
            )
            if profile.confidence < _MIN_CONFIDENCE:
                record.status = "needs_review"

            self._store_file(record, data, att)
            candidate_id = self.repo.insert(record)
            self.ledger.record(email.message_id, resume_hash, candidate_id, "ingested")

            # (6) Contextual Auto-Reply if enabled.
            reply_sent = False
            # Never on a record we are unsure about. A reply is irreversible and
            # goes to a real person; a profile heading for human review has not
            # earned one.
            if settings.auto_reply_enabled and record.status != "needs_review":
                try:
                    reply_text = generate_contextual_reply(profile, email)
                    # Send reply directly to the sender who emailed cv@adiragroups.com.
                    # This ensures the applicant receives the reply and avoids bounces
                    # from invalid/sample emails printed on resume documents.
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
            )

        except (NotAResumeError,) as exc:
            log.info("Skipping attachment: %s", exc)
            return AttachmentResult(att.filename, "not_resume", detail=str(exc))
        except (UnsupportedFileTypeError, TextExtractionError, AIParseError) as exc:
            log.warning("Attachment failed (%s): %s", att.filename, exc)
            return AttachmentResult(att.filename, "error", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — never let one attachment kill the batch
            log.exception("Unexpected error on attachment %s", att.filename)
            return AttachmentResult(att.filename, "error", detail=str(exc))

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
        )

    def _storage_key(self, candidate_id: str, filename: str) -> str:
        now = datetime.now(timezone.utc)
        safe = filename.replace("/", "_").replace("\\", "_")
        return f"{now:%Y/%m}/{candidate_id}_{safe}"

    def _store_file(self, record: CandidateRecord, data: bytes, att: Attachment) -> None:
        self.storage.save(record.resume.storage_key, data, content_type=att.mime_type)
