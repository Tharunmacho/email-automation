"""Candidate intake driven entirely by recruiter-uploaded documents.

The browser supplies files, never extracted identity/profile values.  Resume,
passport and Aadhaar bytes are sent through the same VeriIS modes used by the
mail pipeline, then only the CRM's declared structured fields are copied into
the candidate and protected identity collections.  Provider payloads stay on
the server for identity audit; they are not part of the response contract.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from pydantic import BaseModel

from app.ai.resume_parser import ResumeParser
from app.config import settings
from app.core.models import CandidateProfile, CandidateRecord, utcnow
from app.db import identity_records
from app.db.dedup import normalize_email, normalize_passport, normalize_phone, sha256_hex
from app.extraction import ocr_gateway
from app.logging_config import get_logger
from app.services import identity_files
from app.services.resume_store import ResumeRejected, store_resume, validate_resume
from app.storage.factory import get_storage_backend


log = get_logger(__name__)


class CandidateUploadError(Exception):
    """A safe, user-facing refusal from the upload intake."""

    def __init__(self, message: str, *, status_code: int = 422, code: str = "upload_rejected"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class UploadedDocument:
    data: bytes
    filename: str
    mime_type: str


@dataclass(frozen=True)
class ExtractedIdentity:
    document_type: str
    upload: UploadedDocument
    result: Dict[str, Any]
    job_id: Optional[str]


@dataclass(frozen=True)
class CandidateUploadResult:
    candidate: CandidateRecord
    identity: Dict[str, list[Dict[str, Any]]]


_AADHAAR_FIELDS = (
    "name", "aadhaar_number", "masked_aadhaar_number", "aadhaar_number_valid",
    "date_of_birth", "year_of_birth", "gender", "mobile_number", "address",
    "care_of", "pincode", "vid", "enrollment_id", "document_side",
)
_PASSPORT_FIELDS = (
    "passport_number", "surname", "given_names", "nationality", "issuing_country",
    "date_of_birth", "sex", "expiry_date", "date_of_issue", "personal_number",
    "all_check_digits_valid",
)


def _declared(value: Any) -> Any:
    """Recursively remove provider-specific Pydantic extras from a parsed profile."""
    if isinstance(value, BaseModel):
        return {
            name: _declared(getattr(value, name))
            for name in value.__class__.model_fields
        }
    if isinstance(value, list):
        return [_declared(item) for item in value]
    if isinstance(value, dict):
        return {key: _declared(item) for key, item in value.items()}
    return value


def _curated_profile(profile: CandidateProfile) -> CandidateProfile:
    data = _declared(profile)
    # These are audit/provider payloads, not candidate facts.  Keeping them out
    # also prevents the generic "Additional information" editor from surfacing
    # fields the user never asked to store.
    data["raw_ocr"] = None
    data["additional_info"] = {}
    data["full_name"] = str(data.get("full_name") or "").strip() or None
    data["email"] = str(data.get("email") or "").strip() or None
    data["phone"] = str(data.get("phone") or "").strip() or None
    return CandidateProfile.model_validate(data)


def _extract_identity(
    document_type: str,
    upload: UploadedDocument,
    *,
    require_passport_number: bool = True,
) -> ExtractedIdentity:
    digest = sha256_hex(upload.data)
    try:
        handle, outcome = ocr_gateway.run_job(
            upload.data,
            upload.filename,
            document_type,
            f"candidate-upload/{document_type}/{digest}",
            budget_seconds=settings.identity_job_wait_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - translate provider details at the boundary
        raise CandidateUploadError(
            f"VeriIS could not read the {document_type} file: {exc}",
            status_code=502,
            code=f"{document_type}_ocr_failed",
        ) from exc

    if outcome is None or getattr(outcome, "timed_out", False):
        raise CandidateUploadError(
            f"VeriIS did not finish reading the {document_type} file in time. Please retry.",
            status_code=504,
            code=f"{document_type}_ocr_timeout",
        )
    if not getattr(outcome, "succeeded", False):
        detail = getattr(outcome, "error", None) or "the extraction job failed"
        raise CandidateUploadError(
            f"VeriIS could not read the {document_type} file: {detail}",
            status_code=422,
            code=f"invalid_{document_type}",
        )

    result = dict(getattr(outcome, "result", None) or {})
    if document_type == "aadhaar":
        facts = result.get("aadhaar")
        if not isinstance(facts, dict) or not any(facts.get(key) for key in _AADHAAR_FIELDS):
            raise CandidateUploadError(
                "The Aadhaar upload did not contain readable Aadhaar details.",
                code="invalid_aadhaar",
            )
        if facts.get("aadhaar_number_valid") is False:
            raise CandidateUploadError(
                "The Aadhaar number failed validation. Upload a clearer scan.",
                code="invalid_aadhaar_number",
            )
    else:
        facts = result.get("mrz")
        printed_fields = result.get("fields")
        has_passport_content = (
            isinstance(facts, dict) and any(facts.values())
        ) or (
            isinstance(printed_fields, dict) and any(printed_fields.values())
        )
        if not has_passport_content or (
            require_passport_number
            and (not isinstance(facts, dict) or not facts.get("passport_number"))
        ):
            raise CandidateUploadError(
                "The passport upload did not contain a readable passport number.",
                code="invalid_passport",
            )
        if isinstance(facts, dict) and facts.get("all_check_digits_valid") is False:
            raise CandidateUploadError(
                "The passport MRZ checksum failed. Upload a clearer passport scan.",
                code="invalid_passport_mrz",
            )

    return ExtractedIdentity(
        document_type=document_type,
        upload=upload,
        result=result,
        job_id=getattr(outcome, "job_id", None) or getattr(handle, "job_id", None),
    )


def _public_identity(extracted: ExtractedIdentity) -> Dict[str, Any]:
    if extracted.document_type == "aadhaar":
        facts = dict(extracted.result.get("aadhaar") or {})
        # Upload is available to staff. The response follows the same boundary
        # as the identity profile endpoint: no full Aadhaar number or VID ever
        # rides back to a staff browser. Administrators can inspect the full
        # protected record from the candidate profile afterward.
        return {
            key: facts.get(key)
            for key in _AADHAAR_FIELDS
            if key not in {"aadhaar_number", "vid"} and facts.get(key) is not None
        }

    mrz = dict(extracted.result.get("mrz") or {})
    public = {key: mrz.get(key) for key in _PASSPORT_FIELDS if mrz.get(key) is not None}
    public["check_digits_valid"] = public.pop("all_check_digits_valid", None)
    fields = extracted.result.get("fields")
    if isinstance(fields, dict) and fields:
        public["printed_fields"] = fields
    confidence = extracted.result.get("confidence")
    if confidence is not None:
        public["confidence"] = confidence
    return public


def _rollback_created_candidate(repository, candidate_id: str, resume, files: list[dict]) -> None:
    """Best-effort removal of this request's writes when document filing fails."""
    try:
        identity_records.delete_for_candidate(candidate_id)
    except Exception:  # noqa: BLE001
        log.exception("Could not remove identity rows while rolling back candidate %s", candidate_id)

    seen: set[tuple[str, str]] = set()
    resume_block = resume.model_dump(mode="python") if resume is not None else None
    for block in [*files, resume_block]:
        if not block or block.get("shared_with_resume"):
            continue
        key = block.get("storage_key")
        backend = block.get("storage_backend")
        marker = (str(backend or ""), str(key or ""))
        if not key or marker in seen:
            continue
        seen.add(marker)
        try:
            get_storage_backend(backend).delete(key)
        except Exception:  # noqa: BLE001
            log.exception("Could not remove %s while rolling back candidate %s", key, candidate_id)

    try:
        repository.delete(candidate_id)
    except Exception:  # noqa: BLE001
        log.exception("Could not remove candidate %s after upload filing failed", candidate_id)


def intake_uploaded_candidate(
    *,
    resume: UploadedDocument | None,
    repository,
    uploader_id: str,
    aadhaar: UploadedDocument | Sequence[UploadedDocument] | None = None,
    passport: UploadedDocument | Sequence[UploadedDocument] | None = None,
    parser: ResumeParser | None = None,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    job_id: str | None = None,
    job_title: str | None = None,
    destination_country: str | None = None,
) -> CandidateUploadResult:
    """Create a candidate from any available details or documents.

    Every business field is optional. An empty submission intentionally creates
    a placeholder record that staff can complete later; uploaded files still
    go through their normal safety and OCR validation.
    """
    def identity_uploads(
        value: UploadedDocument | Sequence[UploadedDocument] | None,
        document_type: str,
    ) -> list[UploadedDocument]:
        uploads = [value] if isinstance(value, UploadedDocument) else list(value or [])
        if len(uploads) > 2:
            raise CandidateUploadError(
                f"Upload no more than two {document_type} files.",
                code=f"too_many_{document_type}_files",
            )
        return uploads

    aadhaar_uploads = identity_uploads(aadhaar, "aadhaar")
    passport_uploads = identity_uploads(passport, "passport")

    if (resume or aadhaar_uploads or passport_uploads) and not settings.veris_ocr_api_key:
        raise CandidateUploadError(
            "VeriIS OCR is not configured. Add VERIS_OCR_API_KEY before uploading candidates.",
            status_code=503,
            code="veris_not_configured",
        )

    try:
        if resume:
            validate_resume(resume.data, resume.mime_type)
        for upload in [*aadhaar_uploads, *passport_uploads]:
            identity_files.validate_identity(upload.data, upload.mime_type)
    except (ResumeRejected, identity_files.IdentityRejected) as exc:
        raise CandidateUploadError(exc.message, code=exc.code) from exc

    resume_hash = sha256_hex(resume.data) if resume else None
    extracted_resume = None
    if resume:
        existing = repository.find_by_resume_hash(resume_hash)
        if existing:
            raise CandidateUploadError(
                f"This resume already belongs to {existing.profile.full_name or existing.candidate_code}.",
                status_code=409,
                code="duplicate_resume",
            )

        try:
            parsed, extracted_resume = (parser or ResumeParser()).parse_file(
                resume.data, resume.filename
            )
        except Exception as exc:  # noqa: BLE001 - expose a stable upload contract
            raise CandidateUploadError(
                f"VeriIS could not read the resume: {exc}",
                status_code=502,
                code="resume_ocr_failed",
            ) from exc

        source = str((parsed.additional_info or {}).get("extraction_source") or "")
        if source != "veris_ocr_api":
            raise CandidateUploadError(
                "VeriIS did not return a structured resume result. The candidate was not created.",
                status_code=502,
                code="resume_veris_failed",
            )
        if not parsed.is_resume:
            raise CandidateUploadError("The uploaded file is not a candidate resume.", code="not_a_resume")
        if parsed.confidence < settings.min_ingest_confidence:
            raise CandidateUploadError(
                f"The resume confidence is too low ({parsed.confidence:.0%}). Upload a clearer resume.",
                code="low_resume_confidence",
            )
        profile = _curated_profile(parsed)
    else:
        profile = CandidateProfile(
            is_resume=False,
            confidence=0.0,
            full_name=(full_name or "").strip() or None,
            email=(email or "").strip() or None,
            phone=(phone or "").strip() or None,
        )

    # Recruiter-entered current preferences take precedence over an older CV.
    manual_overrides = {
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "job_id": job_id,
        "job_category": job_id,
        "job_title": job_title,
        "job_preference": job_title,
        "destination_country": destination_country,
    }
    for field, value in manual_overrides.items():
        cleaned = value.strip() if isinstance(value, str) else value
        if cleaned:
            setattr(profile, field, cleaned)
    email_key = normalize_email(profile.email)
    phone_key = normalize_phone(profile.phone)
    was_deleted = getattr(repository, "was_deleted", None)
    if callable(was_deleted) and was_deleted(
        email_key=email_key,
        phone_key=phone_key,
        resume_hash=resume_hash,
    ):
        raise CandidateUploadError(
            "This candidate was deleted from the CRM and cannot be imported again.",
            status_code=410,
            code="candidate_deleted",
        )
    person = repository.find_by_email_or_phone(email_key, phone_key)
    if person:
        raise CandidateUploadError(
            f"A candidate with this email or phone already exists: "
            f"{person.profile.full_name or person.candidate_code}.",
            status_code=409,
            code="duplicate_candidate",
        )

    identities: list[ExtractedIdentity] = []
    identities.extend(
        _extract_identity("aadhaar", upload) for upload in aadhaar_uploads
    )
    identities.extend(
        _extract_identity("passport", upload, require_passport_number=False)
        for upload in passport_uploads
    )

    if passport_uploads and not any(
        (item.result.get("mrz") or {}).get("passport_number")
        for item in identities
        if item.document_type == "passport"
    ):
        raise CandidateUploadError(
            "The passport uploads did not contain a readable passport number.",
            code="invalid_passport",
        )

    passport_result = next(
        (
            item
            for item in identities
            if item.document_type == "passport"
            and (item.result.get("mrz") or {}).get("passport_number")
        ),
        None,
    )
    passport_key = None
    if passport_result:
        mrz = passport_result.result.get("mrz") or {}
        profile.passport_number = mrz.get("passport_number") or None
        profile.passport_expiry = mrz.get("expiry_date") or None
        passport_key = normalize_passport(profile.passport_number)

    passport_finder = getattr(repository, "find_by_passport_key", None)
    passport_owner = (
        passport_finder(passport_key)
        if callable(passport_finder) and passport_key
        else None
    )
    if passport_owner:
        raise CandidateUploadError(
            f"A candidate with this passport already exists: "
            f"{passport_owner.profile.full_name or passport_owner.candidate_code}.",
            status_code=409,
            code="duplicate_passport",
        )

    candidate_id = uuid.uuid4().hex
    stored_resume = None
    if resume:
        stored_resume = store_resume(
            candidate_id=candidate_id,
            data=resume.data,
            filename=resume.filename,
            mime_type=resume.mime_type,
        )
        stored_resume.extraction_method = extracted_resume.method
        stored_resume.ocr_used = True

    now = utcnow()
    record = CandidateRecord(
        id=candidate_id,
        source="upload" if resume else "manual",
        profile=profile,
        resume=stored_resume,
        resume_hash=resume_hash,
        email_key=email_key,
        phone_key=phone_key,
        passport_key=passport_key,
        passport_key_source="ocr" if passport_key else None,
        status="needs_review" if profile.confidence < 0.55 else "ingested",
        cv_required=bool(resume),
        ingested_at=now,
        processed_at=now,
        created_at=now,
        updated_at=now,
    )
    stored_id = repository.insert(record)
    if stored_id != candidate_id:
        existing = repository.get(stored_id)
        _rollback_created_candidate(repository, candidate_id, stored_resume, [])
        if passport_key and existing and existing.passport_key == passport_key:
            raise CandidateUploadError(
                f"A candidate with this passport already exists: "
                f"{existing.profile.full_name or existing.candidate_code}.",
                status_code=409,
                code="duplicate_passport",
            )
        raise CandidateUploadError(
            f"This resume already belongs to {(existing.profile.full_name if existing else stored_id)}.",
            status_code=409,
            code="duplicate_resume",
        )

    public_identity: Dict[str, list[Dict[str, Any]]] = {"aadhaar": [], "passport": []}
    stored_identity_files: list[dict] = []
    try:
        for item in identities:
            record_id = f"upload-{item.document_type}-{uuid.uuid4().hex}"
            file_block = identity_files.store(
                candidate_id=candidate_id,
                document_type=item.document_type,
                record_id=record_id,
                data=item.upload.data,
                filename=item.upload.filename,
                mime_type=item.upload.mime_type,
                resume=stored_resume,
            )
            stored_identity_files.append(file_block)
            writer = (
                identity_records.store_aadhaar_record
                if item.document_type == "aadhaar"
                else identity_records.store_passport_record
            )
            writer(
                record_id,
                item.result,
                candidate_id=candidate_id,
                provider="manual_upload",
                account_id=uploader_id,
                message_id=f"manual-upload/{candidate_id}",
                attachment_id=record_id,
                filename=item.upload.filename,
                sha256=sha256_hex(item.upload.data),
                pages=[],
                ocr_job_id=item.job_id,
                file=file_block,
            )
            public_identity[item.document_type].append(_public_identity(item))
    except Exception as exc:  # noqa: BLE001 - leave no half-created candidate behind
        _rollback_created_candidate(
            repository, candidate_id, stored_resume, stored_identity_files
        )
        raise CandidateUploadError(
            f"The extracted documents could not be stored: {exc}",
            status_code=503,
            code="document_storage_failed",
        ) from exc

    return CandidateUploadResult(candidate=record, identity=public_identity)
