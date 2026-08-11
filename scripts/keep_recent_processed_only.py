"""Script to keep ONLY the most recent processed candidate email in INBOX.Resumes.Processed,
and move all other emails back to INBOX.
"""
from __future__ import annotations
import email, os, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient, _decode_header_str

def keep_recent_only():
    client = SMTPIMAPClient()
    mail = client._connect_imap()
    
    target_folder = "INBOX.Resumes.Processed"
    status, data = mail.select(f'"{target_folder}"')
    if status != "OK":
        print(f"[ERROR] Could not select {target_folder}: {status}")
        mail.logout()
        return

    status, data = mail.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        print(f"[INFO] Folder '{target_folder}' is empty (0 emails).")
        mail.logout()
        return

    uids = [u.decode("utf-8") for u in data[0].split()]
    print(f"Found {len(uids)} message(s) in {target_folder}.")

    # Sort UIDs numerically (highest UID = most recent email)
    uids_sorted = sorted(uids, key=lambda x: int(x))
    most_recent_uid = uids_sorted[-1]
    
    moved_count = 0
    kept_count = 0

    for idx, uid in enumerate(uids_sorted, 1):
        st, md = mail.uid("fetch", uid, "(RFC822.HEADER)")
        if st != "OK" or not md or not md[0] or not isinstance(md[0], tuple):
            print(f"[{idx}/{len(uids_sorted)}] Could not fetch UID {uid}")
            continue

        msg = email.message_from_bytes(md[0][1])
        subj = _decode_header_str(msg.get("Subject", ""))
        frm = _decode_header_str(msg.get("From", ""))

        if uid == most_recent_uid:
            print(f"[{idx}/{len(uids_sorted)}] ⭐ [KEEP MOST RECENT] UID {uid} | From: {frm} | Subject: {subj}")
            kept_count += 1
        else:
            print(f"[{idx}/{len(uids_sorted)}] 📥 [MOVE -> INBOX] UID {uid} | From: {frm} | Subject: {subj}")
            res_copy, _ = mail.uid("copy", uid, "INBOX")
            if res_copy == "OK":
                mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
                moved_count += 1
            else:
                print(f"  [WARN] Failed to copy UID {uid} to INBOX")

    mail.expunge()
    print(f"\n[DONE] Moved {moved_count} email(s) back to INBOX. Kept the most recent 1 email in '{target_folder}'.")
    mail.logout()

if __name__ == "__main__":
    keep_recent_only()
