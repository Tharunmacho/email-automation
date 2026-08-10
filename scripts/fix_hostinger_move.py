"""Debug and fix Hostinger IMAP move from subfolder to INBOX.
"""
from __future__ import annotations
import email, os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient, _decode_header_str

def fix_move():
    client = SMTPIMAPClient()
    mail = client._connect_imap()
    
    target_folder = "INBOX.Resumes.Processed"
    status, data = mail.select(f'"{target_folder}"')
    if status != "OK":
        print(f"Could not select {target_folder}: {status}")
        mail.logout()
        return

    status, data = mail.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        print("No messages in folder.")
        mail.logout()
        return

    uids = [u.decode("utf-8") for u in data[0].split()]
    print(f"Found {len(uids)} UIDs in {target_folder}: {uids}")

    # Inspect each message subject
    # We want to keep ONLY UID for Tharun V if present, move everything else to INBOX
    for uid in uids:
        st, md = mail.uid("fetch", uid, "(RFC822.HEADER)")
        if st != "OK" or not md or not md[0] or not isinstance(md[0], tuple):
            continue
        msg = email.message_from_bytes(md[0][1])
        subj = _decode_header_str(msg.get("Subject", ""))
        frm = _decode_header_str(msg.get("From", ""))
        print(f"\nEvaluating UID {uid}: From '{frm}' | Subject '{subj}'")
        
        # Test copy targets: try "INBOX", "inbox", etc.
        if "tharun" in frm.lower() or "tharun" in subj.lower():
            print(f"  -> KEEPING UID {uid} in {target_folder}")
            continue

        print(f"  -> Moving UID {uid} to INBOX...")
        res_copy, resp_data = mail.uid("copy", uid, "INBOX")
        print(f"     COPY to 'INBOX' result: status={res_copy}, resp={resp_data}")
        
        if res_copy != "OK":
            # Try without quotes or with INBOX
            res_copy, resp_data = mail.uid("copy", uid, "inbox")
            print(f"     COPY to 'inbox' result: status={res_copy}, resp={resp_data}")

        if res_copy == "OK":
            # Mark deleted in subfolder
            mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
            print(f"     Marked UID {uid} \\Deleted")
        else:
            print(f"     [ERROR] COPY failed for UID {uid}")

    mail.expunge()
    print("\nExpunged folder.")
    mail.logout()

if __name__ == "__main__":
    fix_move()
