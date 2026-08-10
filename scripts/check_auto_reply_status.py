"""Check auto_reply_sent status across all candidate records in MongoDB.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.mongo import get_candidates_collection

def check_auto_reply():
    coll = get_candidates_collection()
    records = list(coll.find({}))
    
    print(f"Total Database Candidate Records: {len(records)}\n")
    
    auto_reply_true = []
    auto_reply_false = []
    
    for r in records:
        name = r.get("profile", {}).get("full_name") or "Unknown"
        email = r.get("profile", {}).get("email") or "No email"
        reply_sent = bool(r.get("auto_reply_sent", False))
        c_id = r.get("_id")
        
        if reply_sent:
            auto_reply_true.append((name, email, c_id))
        else:
            auto_reply_false.append((name, email, c_id))

    print(f"Candidates WITH auto_reply_sent = True ({len(auto_reply_true)}):")
    for name, email, c_id in auto_reply_true:
        print(f"  - {name:<25} | Email: {email:<30} | ID: {c_id}")

    print(f"\nCandidates WITH auto_reply_sent = False ({len(auto_reply_false)}):")
    for name, email, c_id in auto_reply_false:
        print(f"  - {name:<25} | Email: {email:<30} | ID: {c_id}")

if __name__ == "__main__":
    check_auto_reply()
