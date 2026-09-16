"""Recover the exact original application from its source mailbox."""
from __future__ import annotations

from app.core.models import CandidateRecord
from app.db.dedup import sha256_hex
from app.db.repository import CandidateRepository
from app.email_client.factory import get_all_email_clients
from app.logging_config import get_logger
from app.storage.factory import get_storage_backend

log = get_logger(__name__)


def recover_from_email(record: CandidateRecord) -> bytes | None:
    """Fetch lazy Gmail attachments too, and verify the bytes before restoring.

    Message UIDs can change after filing and can overlap between accounts. A
    matching content hash is required so recovery never serves another file.
    """
    source = record.source_email
    resume = record.resume
    if not (source and source.message_id and resume and resume.sha256):
        return None

    try:
        clients = get_all_email_clients()
    except Exception as exc:  # A mailbox outage must not hide the storage error.
        log.warning("Could not open mailboxes for resume recovery: %s", exc)
        return None

    for client in clients:
        try:
            finder = getattr(client, "get_message_by_rfc_id", None)
            message = finder(source.thread_id) if source.thread_id and callable(finder) else None
            if message is None:
                message = client.get_message(source.message_id)
            if message is None:
                continue

            for attachment in message.attachments:
                try:
                    data = attachment.data
                    if data is None:
                        data = client.download_attachment(message.message_id, attachment)
                    if data and sha256_hex(data) == resume.sha256:
                        _restore(record, data)
                        return data
                except Exception as exc:  # One broken attachment need not lose the CV.
                    log.debug("Could not read an attachment for candidate %s: %s", record.id, exc)

            # Body-only applications are stored as UTF-8 text during ingestion.
            if resume.original_filename == "email_body.txt":
                data = message.body_text.strip().encode("utf-8")
                if data and sha256_hex(data) == resume.sha256:
                    _restore(record, data)
                    return data
        except Exception as exc:
            log.debug("Could not recover candidate %s from a mailbox: %s", record.id, exc)
    return None


def _restore(record: CandidateRecord, data: bytes) -> None:
    """Cache recovered bytes without letting a failed cache write stop the save."""
    if not (record.resume and record.resume.storage_key):
        return
    try:
        backend = get_storage_backend()
        backend.save(record.resume.storage_key, data, content_type=record.resume.mime_type)
        CandidateRepository().set_storage_backend(record.id, backend.name)
    except Exception as exc:
        log.warning("Could not restore candidate %s into storage: %s", record.id, exc)
