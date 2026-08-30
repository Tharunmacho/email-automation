"""Filing the Aadhaar and passport a candidate sent over WhatsApp.

The email pipeline gets these for free: an application bundle arrives, the page
classifier finds the Aadhaar on page 54, and `multipass` writes the record. A
WhatsApp candidate sends the same two documents as two photographs, and until
now the section describing them was dropped at the door — the intake model is
an allow-list and nothing named `identity`, so the bot's Aadhaar and passport
went nowhere and a recruiter opening the profile saw no documents at all.

This is the other door into the same two collections, and it is deliberately
*only* a door: the projection is `store_aadhaar_record` and
`store_passport_record`, unchanged, fed the extractor's payload untouched. The
bot's OCR service and the mailbox pipeline's return the same shape, so writing
a second mapping here would be two implementations of one projection — and the
one over there is the one that has been in front of recruiters.

Three properties this file exists to hold:

* **A document belongs to one candidate, permanently.** The record id is the
  bot's upload id, which is stable across every re-send and is what makes a
  partial sync idempotent. That same stability is a hazard: a record id that
  turned up under a second candidate would silently move the document. It is
  refused instead.
* **Nothing here can cost a candidate their registration.** A document that
  will not file is logged. The profile is already written by the time this
  runs, and an unreadable passport must not undo it.
* **Provenance survives.** `source` says which conversation, which message and
  which upload a document came off, in the same shape the email pipeline fills
  with a message id and an attachment id. "Where did this Aadhaar come from" has
  an answer on both paths or it has one on neither.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.db import identity_records
from app.logging_config import get_logger

log = get_logger(__name__)

#: The writers, by document type. The same two functions the email pipeline
#: calls — see the module docstring on why this is not its own mapping.
_WRITERS = {
    "aadhaar": identity_records.store_aadhaar_record,
    "passport": identity_records.store_passport_record,
}


@dataclass(frozen=True)
class FiledDocument:
    document_type: str
    record_id: str
    #: Why it was skipped, or None when it was written.
    skipped: Optional[str] = None

    @property
    def stored(self) -> bool:
        return self.skipped is None


def account_from_key(idempotency_key: str) -> str:
    """The line the conversation ran on: `whatsapp/{account}/{wa_id}`.

    Read out of the key rather than asked for separately, because the key is
    already the one identifier every submission carries and a second field
    saying the same thing is a second field that can disagree with it. A key in
    an unexpected shape yields "" — provenance with a gap in it is worth more
    than a refused document.
    """
    parts = (idempotency_key or "").split("/")
    return parts[1] if len(parts) >= 3 else ""


def _owner_conflict(document_type: str, record_id: str, candidate_id: str) -> Optional[str]:
    """The candidate this record is already filed under, if it is someone else.

    The check that makes a stable record id safe. The bot's upload id is stable
    precisely so a re-send overwrites its own row — and that means a bug on
    either side which sent it under a second candidate would move the document
    rather than duplicate it, quietly, with no trace of where it had been.
    Refusing costs a log line; the alternative costs an audit.
    """
    try:
        existing = identity_records.find_by_record_id(document_type, record_id)
    except Exception as exc:  # noqa: BLE001 — a lookup failure is not a conflict
        log.warning(
            "Could not check the owner of %s record %s: %s", document_type, record_id, exc
        )
        return None
    if not existing:
        return None
    owner = existing.get("candidate_id")
    return owner if owner and owner != candidate_id else None


def file_documents(
    *,
    candidate_id: str,
    section: Dict[str, List[Dict[str, Any]]],
    idempotency_key: str,
) -> List[FiledDocument]:
    """Write every identity document in one WhatsApp submission.

    `section` is the bot's `{"aadhaar": [...], "passport": [...]}`, already
    validated by the route. Returns one entry per document, so the caller can
    say what happened without this having to know about HTTP.

    Never raises. Every document is attempted independently: an Aadhaar whose
    payload the projection chokes on must not take the passport beside it down,
    and neither may touch the candidate that has already been written.
    """
    filed: List[FiledDocument] = []
    account_id = account_from_key(idempotency_key)

    for document_type, documents in (section or {}).items():
        writer = _WRITERS.get(document_type)
        if writer is None:
            # Unreachable through the route, which validates the keys. Worth
            # saying rather than ignoring, because reaching it means the
            # contract moved and this file did not.
            log.warning("Ignoring unknown identity document type %r", document_type)
            continue

        for document in documents or []:
            record_id = str(document.get("record_id") or "").strip()
            if not record_id:
                filed.append(FiledDocument(document_type, "", "no record id"))
                continue

            conflict = _owner_conflict(document_type, record_id, candidate_id)
            if conflict:
                log.error(
                    "Refusing to re-file %s document %s: it belongs to candidate %s, "
                    "not %s",
                    document_type, record_id, conflict, candidate_id,
                )
                filed.append(
                    FiledDocument(document_type, record_id, "belongs to another candidate")
                )
                continue

            try:
                writer(
                    record_id,
                    # Untouched. Every projected field is derived from it, so a
                    # mapping bug is recoverable without asking the candidate
                    # for their passport again.
                    dict(document.get("result") or {}),
                    candidate_id=candidate_id,
                    provider="whatsapp",
                    account_id=account_id,
                    # The message the file arrived on, where the bot knows it.
                    # The email pipeline's `message_id` in the same slot.
                    message_id=str(document.get("message_id") or ""),
                    # The upload it came off — this path's attachment.
                    attachment_id=record_id,
                    filename=str(document.get("filename") or ""),
                    sha256=str(document.get("sha256") or ""),
                    # A WhatsApp upload is one document in one file. The pages
                    # list is what the email path uses to say "page 54 of the
                    # bundle", and claiming a page here would be inventing one.
                    pages=[],
                )
            except Exception as exc:  # noqa: BLE001 — a document must not cost the candidate
                log.error(
                    "Could not file the %s document %s for candidate %s: %s",
                    document_type, record_id, candidate_id, exc,
                )
                filed.append(FiledDocument(document_type, record_id, str(exc)))
                continue

            filed.append(FiledDocument(document_type, record_id))

    if filed:
        log.info(
            "Filed %d of %d identity document(s) for candidate %s",
            sum(1 for f in filed if f.stored), len(filed), candidate_id,
        )
    return filed
