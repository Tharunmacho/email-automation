"""Check current state of Hostinger Resumes.Processed folder and Mongo candidate records.
"""
from __future__ import annotations
import email, os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.db.repository import CandidateRepository
from app.email_client.smtp_imap_client import SMTPIMAPClient, _decode_header_str

def check_processed():
    repo = CandidateRepository()
    client = SMTPIMAPClient()
    
    print(f"MongoDB Candidates Count: {repo.count()}")
    for c in repo.list_candidates(limit=100):
        print(f"  DB Candidate: {c.profile.full_name} | Email: {c.profile.email} | ID: {c.id}")
        
    print("\nConnecting to Hostinger IMAP...")
    mail = client._connect_imap()
    processed_folders = ["INBOX.Resumes.Processed", "Resumes.Processed", "Resumes/Processed"]
    target = None
    for pf in processed_folders:
        res, _ = mail.select(f'"{pf}"')
        if res == "OK":
            target = pf
            break
            
    if not target:
        print("[INFO] Processed folder not found or empty.")
        mail.logout()
        return

    status, data = mail.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        print(f"[INFO] Processed folder '{target}' is empty (0 emails).")
        mail.logout()
        return

    uids = data[0].split()
    print(f"\nFound {len(uids)} email(s) currently in Hostinger folder '{target}':")
    for idx, uid_b in enumerate(uids, 1):
        uid = uid_b.decode("utf-8")
        status, msg_data = mail.uid("fetch", uid, "(RFC822)")
        if status == "OK" and msg_data and msg_data[0] and isinstance(msg_data[0], tuple):
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode_header_str(msg.get("Subject", ""))
            from_addr = _decode_header_str(msg.get("From", ""))
            header_msg_id = _decode_header_str(msg.get("Message-ID", uid))
            
            in_repo = repo.find_by_message_id(header_msg_id) or repo.find_by_message_id(uid)
            c_name = in_repo.profile.full_name if in_repo else "NOT IN DB"
            
            print(f"  [{idx:02d}] UID: {uid:<5} | From: {from_addr:<35} | Subject: '{subject:<40}' | DB Candidate: {c_name}")
            
    mail.logout()

if __name__ == "__main__":
    check_processed()
