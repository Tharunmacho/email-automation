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
from datetime import datetime, timezone
from typing import Optional

from app.core.models import StoredResume
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


def validate_resume(data: bytes, mime_type: Optional[str]) -> str:
    """Validate upload bytes without writing them and return the media type."""
    if not data:
        raise ResumeRejected("the resume file is empty", "empty_resume")
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeRejected(
            f"the resume is {len(data) // (1024 * 1024)} MB; the limit is "
            f"{MAX_RESUME_BYTES // (1024 * 1024)} MB",
            "resume_too_large",
        )

    content_type = (mime_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    if content_type not in ALLOWED_RESUME_TYPES:
        raise ResumeRejected(
            f"{content_type} is not an accepted resume type", "unsupported_resume_type"
        )
    return content_type


def _safe_name(filename: Optional[str]) -> str:
    """A filename that cannot escape the key it is embedded in.

    `../` in a storage key is a path traversal on the local backend and simply
    a confusing key on GridFS. Neither is acceptable and the fix is the same
    for both, so it happens here rather than in one backend.
    """
    name = (filename or "").strip() or "resume.pdf"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120] or "resume.pdf"


def storage_key_for(candidate_id: str, filename: Optional[str]) -> str:
    """`YYYY/MM/{candidate_id}_{filename}` — the mailbox pipeline's shape."""
    now = datetime.now(timezone.utc)
    return f"{now:%Y/%m}/{candidate_id}_{_safe_name(filename)}"


def store_resume(
    *,
    candidate_id: str,
    data: bytes,
    filename: Optional[str],
    mime_type: Optional[str],
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
    content_type = validate_resume(data, mime_type)

    digest = sha256_hex(data)
    key = storage_key_for(candidate_id, filename)
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
