"""Exhaustive check of all Hostinger IMAP folders and message counts.
"""
from __future__ import annotations
import email, os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient, _decode_header_str

def check_all_folders():
    client = SMTPIMAPClient()
    mail = client._connect_imap()
    
    status, folders = mail.list()
    if status != "OK":
        print("Could not list IMAP folders")
        return
        
    print("--- ALL HOSTINGER IMAP FOLDERS ---")
    folder_names = []
    for f in folders:
        # Extract folder name from list string
        raw_str = f.decode("utf-8")
        print("Raw folder entry:", raw_str)
        # Parse folder name (last quoted or unquoted string)
        if '"."' in raw_str:
            parts = raw_str.split('"."')
            folder_name = parts[-1].strip().strip('"')
        elif '"/"' in raw_str:
            parts = raw_str.split('"/"')
            folder_name = parts[-1].strip().strip('"')
        else:
            folder_name = raw_str.split()[-1].strip('"')
        folder_names.append(folder_name)

    print("\n--- COUNTING MESSAGES PER FOLDER ---")
    for f_str in folders:
        # Parse exact string to select
        raw = f_str.decode("utf-8")
        # Extract name after delimiter
        import re
        m = re.search(r'\([^)]*\)\s+"([^"]+)"\s+"?([^"]+)"?$', raw)
        if not m:
            m = re.search(r'\([^)]*\)\s+(\S+)\s+"?([^"]+)"?$', raw)
        
        target = m.group(2) if m else raw.split()[-1].strip('"')
        
        res, data = mail.select(f'"{target}"')
        if res == "OK":
            search_res, search_data = mail.uid("search", None, "ALL")
            count = len(search_data[0].split()) if (search_res == "OK" and search_data and search_data[0]) else 0
            print(f"Folder: '{target}' | Message Count: {count}")
            
            if count > 0 and "INBOX" not in target.upper() and target != "INBOX":
                # Print subjects
                uids = search_data[0].split()
                print(f"  Messages in '{target}':")
                for uid_b in uids[:10]:
                    uid = uid_b.decode("utf-8")
                    st, md = mail.uid("fetch", uid, "(RFC822.HEADER)")
                    if st == "OK" and md and md[0] and isinstance(md[0], tuple):
                        msg = email.message_from_bytes(md[0][1])
                        subj = _decode_header_str(msg.get("Subject", ""))
                        frm = _decode_header_str(msg.get("From", ""))
                        print(f"    UID: {uid} | From: {frm[:30]} | Subject: '{subj[:40]}'")
        else:
            print(f"Folder: '{target}' | Could not select ({res})")

    mail.logout()

if __name__ == "__main__":
    check_all_folders()
