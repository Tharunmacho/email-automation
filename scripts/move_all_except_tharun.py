"""Exhaustive and robust script to clean Hostinger INBOX.Resumes.Processed folder.

Moves ALL emails in INBOX.Resumes.Processed back to INBOX, leaving ONLY Tharun's resume!
Handles Unicode and errors safely.
"""
from __future__ import annotations
import email, os, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient, _decode_header_str

def empty_processed_except_tharun():
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
        print("[INFO] Resumes.Processed folder is already empty!")
        mail.logout()
        return

    uids = [u.decode("utf-8") for u in data[0].split()]
    print(f"Found {len(uids)} message(s) in {target_folder}.")

    moved_count = 0
    kept_count = 0

    for idx, uid in enumerate(uids, 1):
        st, md = mail.uid("fetch", uid, "(RFC822.HEADER)")
        if st != "OK" or not md or not md[0] or not isinstance(md[0], tuple):
            print(f"[{idx}/{len(uids)}] Could not fetch UID {uid}")
            continue

        msg = email.message_from_bytes(md[0][1])
        subj = _decode_header_str(msg.get("Subject", ""))
        frm = _decode_header_str(msg.get("From", ""))

        # Check if this is Tharun V's candidate resume (e.g. 192424050.simats@saveetha.com or tharun)
        is_tharun_resume = ("192424050.simats@saveetha.com" in frm.lower() or "tharun" in frm.lower() or "tharun" in subj.lower()) and "Resume" in subj

        if is_tharun_resume and kept_count == 0:
            print(f"[{idx}/{len(uids)}] [KEEP] UID {uid} | From: {frm} | Subject: {subj}")
            kept_count += 1
        else:
            print(f"[{idx}/{len(uids)}] [MOVE -> INBOX] UID {uid} | From: {frm} | Subject: {subj}")
            res_copy, _ = mail.uid("copy", uid, "INBOX")
            if res_copy == "OK":
                mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
                moved_count += 1
            else:
                print(f"  [WARN] Failed to copy UID {uid} to INBOX")

    mail.expunge()
    print(f"\n[SUMMARY] Moved {moved_count} message(s) back to INBOX. Kept {kept_count} message(s) in '{target_folder}'.")
    mail.logout()

if __name__ == "__main__":
    empty_processed_except_tharun()
