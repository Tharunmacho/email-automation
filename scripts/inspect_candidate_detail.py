"""Inspect candidate records in detail.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.mongo import get_candidates_collection

def inspect_details():
    coll = get_candidates_collection()
    records = list(coll.find({"$or": [{"source_email.subject": {"$regex": "crane", "$options": "i"}}, {"profile.full_name": {"$regex": "faizan", "$options": "i"}}]}))
    
    print(f"Found {len(records)} candidate record(s) matching query:")
    for r in records:
        print("\n--------------------------------------------------")
        print("ID:", r.get("_id"))
        print("Name:", r.get("profile", {}).get("full_name"))
        print("Email:", r.get("profile", {}).get("email"))
        print("Phone:", r.get("profile", {}).get("phone"))
        print("Confidence:", r.get("profile", {}).get("confidence"))
        print("Subject:", r.get("source_email", {}).get("subject"))
        print("From:", r.get("source_email", {}).get("from_addr"))
        print("Resume Attachment Filename:", r.get("resume", {}).get("original_filename"))
        print("Resume Size (bytes):", r.get("resume", {}).get("size"))
        print("Extraction Method:", r.get("resume", {}).get("extraction_method"))

if __name__ == "__main__":
    inspect_details()
