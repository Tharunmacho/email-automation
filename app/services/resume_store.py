"""Putting a résumé that arrived over the API into the CRM's own storage.

The whole point of this module is the sentence it makes true: *the CRM stores
the file*. The bot has its own disk, and a `storage_key` from that disk names
something no process here can open — a recruiter clicking "download résumé"
would get a 404 for a document that exists. So the bytes cross the wire and are
written through `get_storage_backend()`, exactly as the mailbox pipeline writes
an attachment, and what lands on the record is a pointer into *this* system.

Two callers, one implementation:

* `POST /candidates` with a résumé attached — the CV-required case, where the
  file has to arrive with the submission because a candidate who needs a CV
  cannot be created without one.
* `POST /candidates/{id}/resume` — attaching a file to a candidate who already
  exists, which is what happens when someone exempt from the requirement sends
  a CV anyway, or sends one later.

The storage key follows the mailbox pipeline's convention (`YYYY/MM/{id}_{name}`)
because a recruiter browsing the bucket should not be able to tell which door a
résumé came in through, and because a second convention is a second thing to
remember when something has to be found by hand.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.core.models import CandidateAttachment, StoredResume
from app.db.dedup import sha256_hex
from app.logging_config import get_logger
from app.storage.factory import get_storage_backend

log = get_logger(__name__)

#: Anything larger is refused rather than stored. A phone photograph of a CV
#: runs to a few megabytes; twenty is not a résumé, and accepting it means one
#: caller can fill the database.
MAX_RESUME_BYTES = 20 * 1024 * 1024

#: What a résumé is allowed to be. Deliberately the same set the mailbox
#: pipeline accepts as an attachment — a CV photographed on a phone is the
#: normal case here, not the exception.
ALLOWED_RESUME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/rtf",
    "text/plain",
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
    "image/tiff",
}


class ResumeRejected(Exception):
    """The bytes offered cannot be stored as a résumé."""

    def __init__(self, message: str, code: str = "invalid_resume"):
        super().__init__(message)
        self.message = message
        self.code = code


#: Spellings of an accepted type that clients send but no registry uses.
_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "text/rtf": "application/rtf"}


def validate_resume(
    data: bytes,
    mime_type: Optional[str],
    filename: Optional[str] = None,
    noun: str = "resume",
) -> str:
    """Validate upload bytes without writing them and return the media type.

    `noun` only changes the wording: an attachment is held to the same types
    and the same size limit, and is told so in its own name.
    """
    if not data:
        raise ResumeRejected(f"the {noun} file is empty", f"empty_{noun}")
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeRejected(
            f"the {noun} is {len(data) // (1024 * 1024)} MB; the limit is "
            f"{MAX_RESUME_BYTES // (1024 * 1024)} MB",
            f"{noun}_too_large",
        )

    content_type = (mime_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    content_type = _MIME_ALIASES.get(content_type, content_type)
    if content_type not in ALLOWED_RESUME_TYPES:
        # The declared type is the sender's guess, and WhatsApp's is often
        # `application/octet-stream` for a perfectly ordinary PDF. Refusing on
        # the label alone lost real CVs, so the bytes get the final word — the
        # same sniff the mailbox pipeline trusts over a declared type.
        from app.extraction.file_type import detect

        sniffed = detect(data, filename or "").mime
        sniffed = _MIME_ALIASES.get(sniffed, sniffed)
        if sniffed in ALLOWED_RESUME_TYPES:
            return sniffed
    if content_type not in ALLOWED_RESUME_TYPES:
        raise ResumeRejected(
            f"{content_type} is not an accepted {noun} type", f"unsupported_{noun}_type"
        )
    return content_type


def _safe_name(filename: Optional[str], fallback: str = "resume.pdf") -> str:
    """A filename that cannot escape the key it is embedded in.

    `../` in a storage key is a path traversal on the local backend and simply
    a confusing key on GridFS. Neither is acceptable and the fix is the same
    for both, so it happens here rather than in one backend.
    """
    name = (filename or "").strip() or fallback
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120] or fallback


def storage_key_for(
    candidate_id: str, filename: Optional[str], discriminator: Optional[str] = None
) -> str:
    """`YYYY/MM/{candidate_id}_{filename}` — the mailbox pipeline's shape.

    `discriminator` goes between the two when a candidate can hold more than
    one file under the same name. A replacement CV is usually the same
    "resume.pdf" uploaded the same month, and without it the new upload would
    be written over the very file it is meant to be kept beside.
    """
    now = datetime.now(timezone.utc)
    middle = f"{discriminator}_" if discriminator else ""
    return f"{now:%Y/%m}/{candidate_id}_{middle}{_safe_name(filename)}"


def store_resume(
    *,
    candidate_id: str,
    data: bytes,
    filename: Optional[str],
    mime_type: Optional[str],
    discriminator: Optional[str] = None,
) -> StoredResume:
    """Write the bytes and describe what was written.

    Returns the `StoredResume` to hang on the record. The caller decides when
    that happens — on a new candidate it is part of the insert, and on an
    existing one it is an update — because only the caller knows whether there
    is a record yet.

    `extraction_method` and `ocr_used` are left at their defaults, and that is
    accurate rather than lazy: nothing has parsed this file. The mailbox
    pipeline fills them because it extracted the text on the way past; a CV
    handed over by the bot has been read by nobody here, and claiming a method
    would be a fact about a process that never ran.
    """
    content_type = validate_resume(data, mime_type, filename)

    digest = sha256_hex(data)
    key = storage_key_for(candidate_id, filename, discriminator)
    backend = get_storage_backend()
    backend.save(key, data, content_type=content_type)

    log.info(
        "Stored resume for candidate %s (%d bytes, %s, backend=%s)",
        candidate_id,
        len(data),
        content_type,
        backend.name,
    )

    return StoredResume(
        original_filename=_safe_name(filename),
        mime_type=content_type,
        size=len(data),
        sha256=digest,
        storage_backend=backend.name,
        storage_key=key,
    )


def store_attachment(
    *,
    candidate_id: str,
    data: bytes,
    filename: Optional[str],
    mime_type: Optional[str],
    kind: str,
    label: Optional[str],
    uploaded_by: Optional[str],
    uploaded_by_name: Optional[str],
) -> CandidateAttachment:
    """Write a recruiter's upload and describe it as an attachment.

    Held to the résumé's rules — a certificate or an experience letter is the
    same kind of file, photographed on the same phone — and keyed under
    `attachments/` with its own id, so two uploads named "certificate.pdf" in
    the same month are two files rather than one overwriting the other.
    """
    content_type = validate_resume(data, mime_type, filename, noun="attachment")

    attachment_id = uuid.uuid4().hex
    name = _safe_name(filename, fallback="attachment")
    now = datetime.now(timezone.utc)
    key = f"attachments/{now:%Y/%m}/{candidate_id}_{attachment_id[:12]}_{name}"
    backend = get_storage_backend()
    backend.save(key, data, content_type=content_type)

    log.info(
        "Stored %s attachment for candidate %s (%d bytes, %s, backend=%s)",
        kind,
        candidate_id,
        len(data),
        content_type,
        backend.name,
    )

    return CandidateAttachment(
        id=attachment_id,
        kind=kind,
        label=(label or "").strip() or None,
        original_filename=name,
        mime_type=content_type,
        size=len(data),
        sha256=sha256_hex(data),
        storage_backend=backend.name,
        storage_key=key,
        uploaded_by=uploaded_by,
        uploaded_by_name=uploaded_by_name,
    )
