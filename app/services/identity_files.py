"""The scan behind an Aadhaar or passport row, when a recruiter asks for it.

The identity collections hold what an extractor *read* — a number, an MRZ, a
checksum verdict. Until now nothing held the page it read them off, so a
documentation officer who wanted to check a misread digit had to open the whole
application bundle and find page 54 themselves. This module is the missing
half: given an identity record, it produces the file.

There are two places the bytes can come from, and the order matters.

* **A file stored against the record.** The `file` block names a key in the
  CRM's own storage. This is what a document that arrived on its own — a
  candidate sending their Aadhaar over WhatsApp, one scan, one upload — has.
* **The pages of the bundle it was read off.** The email pipeline never stores
  the identity pages separately; it stores the attachment, and records which of
  its pages held the passport. Re-cutting those pages on demand is exact, needs
  no migration, and works for every record already in the database — which is
  all of them.

The second path is why this is a module rather than a `storage.load()` in the
route. Nothing is copied, nothing is written: the subset is built from the
untouched original every time it is asked for, so a record whose provenance
says "page 54" cannot drift away from what page 54 actually holds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.models import CandidateRecord
from app.db.dedup import sha256_hex
from app.extraction import pdf_pages
from app.logging_config import get_logger
from app.storage.factory import get_storage_backend

log = get_logger(__name__)


#: Anything larger is refused rather than stored. An Aadhaar card photographed
#: on a phone is a couple of megabytes; the same ceiling the résumé path uses,
#: for the same reason — one caller must not be able to fill the database.
MAX_IDENTITY_BYTES = 20 * 1024 * 1024

#: What an identity document is allowed to be. Narrower than the résumé set on
#: purpose: a passport data page is a photograph or a scan, never a .docx.
ALLOWED_IDENTITY_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
    "image/tiff",
}


class IdentityRejected(Exception):
    """The bytes offered cannot be stored as an identity document."""

    def __init__(self, message: str, code: str = "invalid_identity_document"):
        super().__init__(message)
        self.message = message
        self.code = code


class IdentityFileMissing(Exception):
    """No file can be produced for this identity record.

    Carries the reason, because "the bundle is gone from storage" and "this
    record never had a file behind it" are different facts and a recruiter
    seeing the same message for both learns nothing.
    """


@dataclass(frozen=True)
class IdentityFile:
    data: bytes
    mime_type: str
    filename: str


def _stored(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The record's own file block, if it has one with a key in it."""
    block = doc.get("file")
    if isinstance(block, dict) and block.get("storage_key"):
        return block
    return None


def _pages(doc: Dict[str, Any]) -> List[int]:
    source = doc.get("source") or {}
    try:
        return [int(n) for n in (source.get("pages") or [])]
    except (TypeError, ValueError):
        return []


def _bundle_matches(record: CandidateRecord, doc: Dict[str, Any]) -> bool:
    """Whether the candidate's stored file is the one this record was read off.

    Both fingerprints are the sha256 of the whole attachment, so when both are
    present they settle it. A record written before the fingerprint was carried
    has none, and the candidate id it is filed under is then the only link
    there is — which is the link the caller has already checked. Refusing those
    would take the feature away from exactly the older records most likely to
    need looking up.
    """
    stored_hash = (doc.get("source") or {}).get("sha256")
    if not stored_hash or not record.resume:
        return True
    return stored_hash == record.resume.sha256


def _load(backend_name: str, key: str) -> bytes:
    """Load `key`, trying the other backend if the named one has nothing.

    `download_resume` already does this, for a real reason: a deployment that
    moved from local disk to GridFS left records naming the backend they were
    written under, and the file is in one of the two. Same situation, same fix.
    """
    try:
        return get_storage_backend(backend_name).load(key)
    except Exception as first:  # noqa: BLE001
        alternate = "local" if backend_name == "gridfs" else "gridfs"
        try:
            return get_storage_backend(alternate).load(key)
        except Exception as second:  # noqa: BLE001
            raise IdentityFileMissing(
                f"the file is not in storage ({backend_name}: {first}; {alternate}: {second})"
            ) from second


def _stem(name: Optional[str]) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] or "document"


def available(record: CandidateRecord, doc: Dict[str, Any]) -> bool:
    """Whether `load` could produce a file — without reading any bytes.

    The profile screen asks this for every row it renders, so it has to be
    cheap: it answers from what the records already say, not from storage. A
    key that names a file somebody deleted out from under it still reads as
    available here and 404s on the click, and that is the right trade — the
    alternative is a storage round trip per row on every profile open, to
    pre-empt a case that means somebody has been deleting files by hand.
    """
    if _stored(doc):
        return True
    return bool(
        record.resume
        and record.resume.storage_key
        and _bundle_matches(record, doc)
    )


def load(record: CandidateRecord, doc: Dict[str, Any]) -> IdentityFile:
    """The scan itself. Raises `IdentityFileMissing` when there isn't one."""
    document_type = doc.get("document_type") or "document"

    block = _stored(doc)
    if block:
        data = _load(block.get("storage_backend") or "", block["storage_key"])
        return IdentityFile(
            data=data,
            mime_type=block.get("mime_type") or "application/octet-stream",
            filename=block.get("filename") or f"{document_type}.pdf",
        )

    if not (record.resume and record.resume.storage_key):
        raise IdentityFileMissing(
            "no scan is stored for this document — it was read out of a bundle "
            "the candidate record no longer points at"
        )
    if not _bundle_matches(record, doc):
        raise IdentityFileMissing(
            "the file on the candidate record is not the one this document was "
            "read from, so the pages it names cannot be trusted"
        )

    data = _load(record.resume.storage_backend, record.resume.storage_key)
    pages = _pages(doc)
    stem = _stem((doc.get("source") or {}).get("filename") or record.resume.original_filename)

    # `subset_pdf` returns None for every case whose correct answer is "serve
    # the original": no page numbers, pages that are the whole file, a file
    # that is not a PDF at all — a phone photograph of an Aadhaar card is one
    # image and the whole of it is the document.
    subset = pdf_pages.subset_pdf(data, pages) if pages else None
    if subset is None:
        return IdentityFile(
            data=data,
            mime_type=record.resume.mime_type or "application/octet-stream",
            filename=f"{stem}_{document_type}.{_extension(record.resume.original_filename)}",
        )

    span = "-".join(str(n) for n in sorted(set(pages)))
    return IdentityFile(
        data=subset,
        mime_type="application/pdf",
        filename=f"{stem}_{document_type}_p{span}.pdf",
    )


def _extension(name: Optional[str]) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    return ext.lower() or "pdf"


def _safe_name(filename: Optional[str], document_type: str) -> str:
    """A filename that cannot escape the key it is embedded in.

    `../` in a storage key is a path traversal on the local backend and a
    confusing key on GridFS. Neither is acceptable and the fix is the same for
    both, so it happens here rather than in a backend.
    """
    name = (filename or "").strip() or f"{document_type}.jpg"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120] or f"{document_type}.jpg"


def storage_key_for(candidate_id: str, record_id: str, filename: Optional[str],
                    document_type: str) -> str:
    """`YYYY/MM/{candidate}_{record}_{name}` — the mailbox pipeline's shape.

    The record id is in the key because a candidate has more than one of these
    and two of them are the same document: the front and the back of an Aadhaar
    card arrive as separate uploads with the same suggested filename, and a key
    without the record id would have the second overwrite the first.
    """
    now = datetime.now(timezone.utc)
    return f"{now:%Y/%m}/{candidate_id}_{record_id}_{_safe_name(filename, document_type)}"


def store(
    *,
    candidate_id: str,
    document_type: str,
    record_id: str,
    data: bytes,
    filename: Optional[str],
    mime_type: Optional[str],
    existing: Optional[Dict[str, Any]] = None,
    resume=None,
) -> Dict[str, Any]:
    """Write an identity scan and describe what was written.

    Returns the `file` block to hang on the identity record. Two ways it
    returns without writing anything, and both are the point rather than an
    optimisation:

    * **The record already holds this exact file.** A partial sync runs on every
      answered question, so the same passport is offered over and over. Re-writing
      it would be a new object per question for one document.
    * **The candidate's résumé *is* this file.** A candidate who sent one PDF that
      is both their CV and their passport page has one document in this system,
      and the identity record points at the copy that is already there. Nothing
      here needs its own copy to serve it — `load` reads whatever key it is given.
    """
    if not data:
        raise IdentityRejected("the document file is empty", "empty_identity_document")
    if len(data) > MAX_IDENTITY_BYTES:
        raise IdentityRejected(
            f"the document is {len(data) // (1024 * 1024)} MB; the limit is "
            f"{MAX_IDENTITY_BYTES // (1024 * 1024)} MB",
            "identity_document_too_large",
        )

    content_type = (mime_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    if content_type not in ALLOWED_IDENTITY_TYPES:
        raise IdentityRejected(
            f"{content_type} is not an accepted identity document type",
            "unsupported_identity_document_type",
        )

    digest = sha256_hex(data)
    name = _safe_name(filename, document_type)

    if existing and existing.get("storage_key") and existing.get("sha256") == digest:
        log.info(
            "Identity document %s for candidate %s is already stored; not writing it again",
            record_id, candidate_id,
        )
        return dict(existing)

    if resume is not None and getattr(resume, "sha256", None) == digest and resume.storage_key:
        log.info(
            "Identity document %s for candidate %s is the file already stored as their "
            "resume; pointing at it rather than storing a second copy",
            record_id, candidate_id,
        )
        return {
            "storage_backend": resume.storage_backend,
            "storage_key": resume.storage_key,
            "filename": name,
            "mime_type": resume.mime_type or content_type,
            "size": len(data),
            "sha256": digest,
            # So a reader can tell this key is shared with the candidate record
            # and must not be deleted when only the identity record goes.
            "shared_with_resume": True,
        }

    key = storage_key_for(candidate_id, record_id, name, document_type)
    backend = get_storage_backend()
    backend.save(key, data, content_type=content_type)

    log.info(
        "Stored %s scan for candidate %s (%d bytes, %s, backend=%s)",
        document_type, candidate_id, len(data), content_type, backend.name,
    )

    return {
        "storage_backend": backend.name,
        "storage_key": key,
        "filename": name,
        "mime_type": content_type,
        "size": len(data),
        "sha256": digest,
    }
