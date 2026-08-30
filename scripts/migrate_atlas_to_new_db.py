"""Export data from MongoDB Atlas and import/migrate to new MongoDB instance.

Usage:
    python scripts/migrate_atlas_to_new_db.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import bson.json_util
from pymongo import MongoClient

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings

ATLAS_URI = os.environ.get("OLD_MONGO_URI", "").strip()
DB_NAME = os.environ.get("OLD_MONGO_DB", "resume_ats").strip()
BACKUP_DIR = Path("data/atlas_backup")


def export_from_atlas():
    if not ATLAS_URI:
        raise SystemExit("Set OLD_MONGO_URI before running this migration.")
    print(f"Connecting to old MongoDB database '{DB_NAME}'...")
    atlas_client = MongoClient(ATLAS_URI, serverSelectionTimeoutMS=15000)
    atlas_db = atlas_client[DB_NAME]
    
    collections = atlas_db.list_collection_names()
    print(f"Found {len(collections)} collections in Atlas: {collections}\n")
    
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    
    data_by_coll = {}
    for coll_name in collections:
        coll = atlas_db[coll_name]
        docs = list(coll.find({}))
        data_by_coll[coll_name] = docs
        
        # Save to JSON file with BSON preservation
        out_file = BACKUP_DIR / f"{coll_name}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(bson.json_util.dumps(docs, indent=2))
        
        print(f"  Exported {len(docs)} documents from '{coll_name}' -> {out_file}")
        
    atlas_client.close()
    return data_by_coll


def import_to_target(data_by_coll):
    target_uri = settings.mongo_uri
    print(f"\nAttempting migration to target database '{settings.mongo_db}'...")
    try:
        target_client = MongoClient(target_uri, serverSelectionTimeoutMS=5000)
        # Test connection
        target_client.admin.command("ping")
        print("  Target MongoDB connection successful!")
    except Exception as exc:
        print(f"  Target MongoDB is not reachable from this machine ({exc}).")
        print("  Backups saved to 'data/atlas_backup/'. You can run this script on the server or import via MongoDB Compass.")
        return

    target_db = target_client[settings.mongo_db]
    for coll_name, docs in data_by_coll.items():
        if not docs:
            continue
        coll = target_db[coll_name]
        inserted = 0
        skipped = 0
        for doc in docs:
            doc_id = doc.get("_id")
            if doc_id and coll.find_one({"_id": doc_id}):
                skipped += 1
            else:
                try:
                    coll.insert_one(doc)
                    inserted += 1
                except Exception as e:
                    print(f"    Error inserting doc {doc_id} into {coll_name}: {e}")
        print(f"  Collection '{coll_name}': Inserted {inserted}, Skipped {skipped} (already exists)")

    target_client.close()
    print("\nMigration to target MongoDB complete!")


if __name__ == "__main__":
    exported = export_from_atlas()
    import_to_target(exported)
