"""Script to clean up Hostinger IMAP processed folders.

Strict Rule:
ONLY emails that correspond to an ingested CandidateRecord in MongoDB Atlas remain in Resumes/Processed.
ALL OTHER emails are moved back to INBOX.
"""
from __future__ import annotations

import email
import imaplib, os, sys

# Ensure app modules can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.db.repository import CandidateRepository
from app.db.ledger import IngestLedger
from app.email_client.smtp_imap_client import SMTPIMAPClient, _decode_header_str, _parse_from
from app.core.models import EmailMessage, Attachment
from app.db.dedup import normalize_email

def _parse_mime_parts(message_id: str, msg: email.message.Message) -> tuple[str, list[Attachment]]:
    body_parts: list[str] = []
    attachments: list[Attachment] = []
    part_idx = 0
    for part in msg.walk():
        part_idx += 1
        if part.is_multipart():
            continue

        content_type = part.get_content_type()
        content_disposition = str(part.get("Content-Disposition", ""))
        filename = part.get_filename()

        if filename:
            filename = _decode_header_str(filename)

        is_attachment = "attachment" in content_disposition.lower() or bool(filename)

        if is_attachment:
            payload = part.get_payload(decode=True) or b""
            att_id = f"{message_id}_{part_idx}"
            att = Attachment(
                filename=filename or f"attachment_{part_idx}.bin",
                mime_type=content_type or "application/octet-stream",
                size=len(payload),
                attachment_id=att_id,
                data=payload,
            )
            attachments.append(att)
        elif content_type == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    body_parts.append(payload.decode("latin1", errors="replace"))
        elif content_type == "text/html" and not body_parts:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                raw_html = payload.decode(charset, errors="replace")
                import re
                clean_text = re.sub(r"<[^>]+>", " ", raw_html)
                body_parts.append(clean_text)

    return "\n".join(body_parts).strip(), attachments

def cleanup_hostinger_strict():
    repo = CandidateRepository()
    ledger = IngestLedger()
    client = SMTPIMAPClient()
    
    if not settings.imap_server or not settings.imap_username:
        print("[ERROR] IMAP credentials not configured in .env")
        return

    print(f"Connecting to Hostinger IMAP ({settings.imap_server})...")
    mail = client._connect_imap()
    
    # Possible processed folder targets
    candidates_processed = [
        "INBOX.Resumes.Processed",
        "Resumes.Processed",
        "Resumes/Processed"
    ]
    target_folder = None
    for candidate in candidates_processed:
        status, data = mail.select(f'"{candidate}"')
        if status == "OK":
            target_folder = candidate
            print(f"\n[OK] Selected processed folder: '{target_folder}'")
            break

    if not target_folder:
        print("[INFO] No processed folder found on Hostinger. Nothing to move.")
        mail.logout()
        return

    # Search all emails in processed folder
    status, data = mail.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        print("[INFO] No emails found in processed folder.")
        mail.logout()
        return

    uids = data[0].split()
    print(f"Found {len(uids)} email(s) in folder '{target_folder}'. Analyzing...")

    moved_count = 0
    kept_count = 0

    for idx, uid_bytes in enumerate(uids, 1):
        uid = uid_bytes.decode("utf-8")
        status, msg_data = mail.uid("fetch", uid, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
            print(f"  [{idx}/{len(uids)}] Skipping UID {uid} (could not fetch)")
            continue

        raw_bytes = msg_data[0][1]
        msg = email.message_from_bytes(raw_bytes)
        
        raw_from = _decode_header_str(msg.get("From", ""))
        from_addr, from_name = _parse_from(raw_from)
        subject = _decode_header_str(msg.get("Subject", ""))
        header_msg_id = _decode_header_str(msg.get("Message-ID", uid))

        body_text, attachments = _parse_mime_parts(uid, msg)

        # STRICT CHECK: Must match an ingested CandidateRecord in Mongo DB
        in_repo_by_msg = repo.find_by_message_id(header_msg_id) or repo.find_by_message_id(uid)
        email_key = normalize_email(from_addr) if from_addr else None
        in_repo_by_email = repo.find_by_email_or_phone(email_key, None) if email_key else None
        
        is_ingested = bool(in_repo_by_msg or in_repo_by_email)

        if is_ingested:
            print(f"  [{idx}/{len(uids)}] [KEEP] UID {uid} ('{subject[:40]}'): Ingested candidate profile in DB.")
            kept_count += 1
        else:
            print(f"  [{idx}/{len(uids)}] [MOVE BACK -> INBOX] UID {uid} ('{subject[:40]}'): Not an ingested candidate in DB.")
            # Copy back to INBOX
            res_copy, _ = mail.uid("copy", uid, "INBOX")
            if res_copy == "OK":
                mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
                moved_count += 1
            else:
                print(f"    [WARN] Failed to copy UID {uid} back to INBOX.")

    if moved_count > 0:
        mail.expunge()
        print(f"\n[DONE] Successfully moved {moved_count} non-candidate email(s) back to INBOX.")
    else:
        print("\n[DONE] No non-candidate emails needed to be moved.")

    print(f"Processed folder state: {kept_count} candidate resume(s) kept in '{target_folder}'.")
    mail.logout()

if __name__ == "__main__":
    cleanup_hostinger_strict()
