"""List exact email addresses that received an auto-reply.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.mongo import get_candidates_collection

def list_auto_replies():
    coll = get_candidates_collection()
    records = list(coll.find({"auto_reply_sent": True}))
    
    print(f"Total Candidate Records with Auto-Reply Sent: {len(records)}\n")
    for idx, r in enumerate(records, 1):
        name = r.get("profile", {}).get("full_name") or "N/A"
        email = r.get("profile", {}).get("email") or "N/A"
        from_addr = r.get("source_email", {}).get("from_addr") or "N/A"
        subject = r.get("source_email", {}).get("subject") or "N/A"
        c_id = r.get("_id")
        print(f"[{idx:02d}] Candidate: {name:<22} | Reply Address: {email:<30} | From: {from_addr}")

if __name__ == "__main__":
    list_auto_replies()
