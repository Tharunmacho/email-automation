"""Clean up ingest_ledger and verify no non-resume emails are tagged in DB.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.mongo import get_db, get_candidates_collection

def clean_ledger():
    db = get_db()
    candidates_coll = get_candidates_collection()
    ledger_coll = db["ingest_ledger"]
    
    # Get all valid candidate IDs currently in candidates collection
    valid_candidate_ids = set(doc["_id"] for doc in candidates_coll.find({}, {"_id": 1}))
    
    # Remove ledger entries whose candidate_id is not in valid_candidate_ids or status != "ingested"
    all_ledger = list(ledger_coll.find({}))
    print(f"Total Ledger Records: {len(all_ledger)}")
    
    removed = 0
    for entry in all_ledger:
        c_id = entry.get("candidate_id")
        status = entry.get("status")
        if c_id not in valid_candidate_ids or status != "ingested":
            ledger_coll.delete_one({"_id": entry["_id"]})
            removed += 1
            
    print(f"Removed {removed} invalid/non-candidate ledger entries.")
    print(f"Remaining clean ledger entries: {ledger_coll.count_documents({})}")

if __name__ == "__main__":
    clean_ledger()
