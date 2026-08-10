"""Verify all 50 candidates in MongoDB Atlas.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.mongo import get_candidates_collection

def verify_all():
    coll = get_candidates_collection()
    records = list(coll.find().sort("created_at", -1))
    
    print(f"Total Database Candidates: {len(records)}\n")
    
    valid_resumes = 0
    missing_resumes = 0
    
    for idx, r in enumerate(records, 1):
        c_id = r.get("_id")
        profile = r.get("profile", {})
        resume = r.get("resume", {})
        
        name = profile.get("full_name") or "N/A"
        email = profile.get("email") or "N/A"
        phone = profile.get("phone") or "N/A"
        
        filename = resume.get("original_filename")
        size = resume.get("size")
        backend = resume.get("storage_backend")
        key = resume.get("storage_key")
        
        has_file = bool(filename and size and size > 0)
        if has_file:
            valid_resumes += 1
            print(f"[{idx:02d}] OK | Candidate: {name:<25} | File: {filename:<35} | Size: {size} bytes | Phone: {phone}")
        else:
            missing_resumes += 1
            print(f"[{idx:02d}] MISSING FILE | Candidate: {name} | ID: {c_id}")

    print(f"\nVerification Summary:")
    print(f"Total Candidates: {len(records)}")
    print(f"Valid Resume Attachments: {valid_resumes}")
    print(f"Missing Resume Attachments: {missing_resumes}")

if __name__ == "__main__":
    verify_all()
