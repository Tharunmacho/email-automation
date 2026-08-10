"""Inspect candidate records in MongoDB Atlas.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.repository import CandidateRepository
from app.db.mongo import get_candidates_collection

def inspect_db():
    repo = CandidateRepository()
    total = repo.count()
    print(f"Total Candidate Records in MongoDB Atlas: {total}")
    
    coll = get_candidates_collection()
    records = list(coll.find().sort("created_at", -1))
    
    print("\n--- Summary of Database Candidate Records ---")
    for i, r in enumerate(records, 1):
        c_id = r.get("_id")
        profile = r.get("profile", {})
        name = profile.get("full_name") or "Unknown"
        email = profile.get("email") or "No Email"
        phone = profile.get("phone") or "No Phone"
        confidence = profile.get("confidence", 0)
        source = r.get("source_email", {})
        subject = source.get("subject", "No Subject")
        from_addr = source.get("from_addr", "No From")
        
        print(f"[{i:02d}] ID: {c_id} | Name: {name} | Subject: '{subject}' | From: {from_addr} | Conf: {confidence:.2f}")

if __name__ == "__main__":
    inspect_db()
