"""Identify original candidates vs newly ingested hostinger candidates.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.mongo import get_candidates_collection

def identify():
    coll = get_candidates_collection()
    records = list(coll.find().sort("created_at", 1)) # Ascending order (oldest first)
    
    print(f"Total Candidates: {len(records)}\n")
    for idx, r in enumerate(records, 1):
        c_id = r.get("_id")
        created_at = r.get("created_at")
        profile = r.get("profile", {})
        source = r.get("source_email", {})
        
        name = profile.get("full_name") or "N/A"
        from_addr = source.get("from_addr") or "N/A"
        subject = source.get("subject") or "N/A"
        
        print(f"[{idx:02d}] Created: {created_at} | Name: {name:<22} | From: {from_addr:<35} | ID: {c_id}")

if __name__ == "__main__":
    identify()
