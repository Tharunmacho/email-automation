"""Instant high-speed batch mailbox organization:
Move candidate emails with attachments (PDFs/Word docs/resumes) from INBOX -> Resumes.Processed
Keep system emails (Google Security alerts, OpenAI, Canva) in INBOX
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.email_client import get_all_email_clients
from app.logging_config import get_logger

log = get_logger("organize_mailbox")


def organize_mailbox():
    print("=" * 60)
    print("Organizing Mailbox: Candidate Emails -> Resumes.Processed, System Emails -> INBOX")
    print("=" * 60)

    clients = get_all_email_clients()
    total_processed_moved = 0

    for idx, client in enumerate(clients, 1):
        account_name = getattr(client, "imap_username", getattr(client, "_user_email", f"Account #{idx}"))
        print(f"\n[{idx}/{len(clients)}] Processing Account: {account_name}")

        if hasattr(client, "_connect_imap"):
            try:
                mail = client._connect_imap()
                
                target_proc = "Resumes.Processed"
                inbox_proc = "INBOX.Resumes.Processed"

                for tf in [target_proc, inbox_proc]:
                    try:
                        mail.create(tf)
                    except Exception:
                        pass

                st, _ = mail.select("INBOX")
                if st == "OK":
                    # Fetch all headers in ONE single batch command
                    st_f, fetch_data = mail.uid("fetch", "1:*", "(BODY[HEADER.FIELDS (FROM SUBJECT CONTENT-TYPE)])")
                    if st_f == "OK" and fetch_data:
                        uids_to_move = []
                        for item in fetch_data:
                            if not isinstance(item, tuple) or len(item) < 2:
                                continue
                            
                            header_str = item[0].decode(errors="ignore") if isinstance(item[0], bytes) else str(item[0])
                            # Extract UID from header response (e.g. "1 (UID 5 BODY[HEADER...]")
                            uid = None
                            if "UID" in header_str:
                                parts = header_str.split()
                                for i, p in enumerate(parts):
                                    if p == "UID" and i + 1 < len(parts):
                                        uid = parts[i + 1]
                                        break
                            
                            hdr_text = item[1].decode(errors="ignore").lower() if isinstance(item[1], bytes) else ""

                            # Skip non-resume system emails
                            if any(s in hdr_text for s in [
                                "google", "openai", "canva", "security alert",
                                "sign-in", "verification", "no-reply", "noreply",
                                "updates to", "2-step"
                            ]):
                                continue

                            # Identify candidate emails with attachments or resume keywords
                            if any(k in hdr_text for k in [
                                "resume", "cv", "applicant", "saravanan", "uday", "tharun",
                                "multipart/mixed", "application/pdf", "application/vnd", "pdf"
                            ]):
                                if uid and uid not in uids_to_move:
                                    uids_to_move.append(uid)

                        if uids_to_move:
                            uid_set = ",".join(uids_to_move)
                            res, _ = mail.uid("copy", uid_set, target_proc)
                            if res != "OK":
                                res, _ = mail.uid("copy", uid_set, inbox_proc)
                            if res == "OK":
                                mail.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
                                mail.expunge()
                                total_processed_moved += len(uids_to_move)
                                print(f"  BATCH SUCCESS: Moved {len(uids_to_move)} candidate resume email(s) from INBOX to '{target_proc}'")
                mail.logout()
            except Exception as err:
                print(f"  IMAP error: {err}")

    print("\n" + "=" * 60)
    print(f"ORGANIZATION COMPLETE: {total_processed_moved} Candidate Email(s) Moved to Resumes/Processed")
    print("=" * 60)


if __name__ == "__main__":
    organize_mailbox()
