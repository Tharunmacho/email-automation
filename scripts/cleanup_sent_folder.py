"""Script to clean up sent auto-reply copies in Hostinger INBOX.Sent folder.
"""
from __future__ import annotations
import email, os, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient, _decode_header_str

def cleanup_sent():
    client = SMTPIMAPClient()
    mail = client._connect_imap()
    
    sent_folders = ["INBOX.Sent", "Sent"]
    target_folder = None
    for sf in sent_folders:
        res, _ = mail.select(f'"{sf}"')
        if res == "OK":
            target_folder = sf
            break
            
    if not target_folder:
        print("[INFO] Sent folder not found.")
        mail.logout()
        return

    status, data = mail.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        print(f"[INFO] Sent folder '{target_folder}' is empty (0 emails).")
        mail.logout()
        return

    uids = [u.decode("utf-8") for u in data[0].split()]
    print(f"Found {len(uids)} message(s) in Sent folder '{target_folder}'. Cleaning up...")

    moved_count = 0
    for uid in uids:
        st, md = mail.uid("fetch", uid, "(RFC822.HEADER)")
        if st == "OK" and md and md[0] and isinstance(md[0], tuple):
            msg = email.message_from_bytes(md[0][1])
            to_addr = _decode_header_str(msg.get("To", ""))
            subj = _decode_header_str(msg.get("Subject", ""))
            print(f"  Deleting Sent UID {uid} | To: {to_addr} | Subject: {subj}")
            mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
            moved_count += 1

    mail.expunge()
    print(f"\n[DONE] Expunged {moved_count} message(s) from Hostinger Sent folder '{target_folder}'.")
    mail.logout()

if __name__ == "__main__":
    cleanup_sent()
