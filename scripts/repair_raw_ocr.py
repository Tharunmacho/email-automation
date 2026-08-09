"""Restore `raw_ocr` to the verbatim Veris OCR payload.

Two defects wrote non-Veris data into the stored JSON:

  * `CandidateRepository.update_profile` copied the edited profile into
    `raw_ocr["profile"]` on every save. The next save copied the result in
    again, one level deeper, so an edited record carried a nested doll of
    itself and grew toward Mongo's 16 MB document ceiling.
  * `map_veris_to_profile` added an `extracted_text` key holding *our* text
    extraction, which Veris never returned.

Both are fixed at the source; this script cleans records written before the
fix and re-syncs the two copies of the payload (record-level `raw_ocr` and
`profile.raw_ocr`) so they are identical.

    python scripts/repair_raw_ocr.py            # report only
    python scripts/repair_raw_ocr.py --apply    # write the cleaned payloads
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.mongo import get_candidates_collection  # noqa: E402

# Keys this codebase injected. Veris returns neither: its own page text lives
# under `pages[].text`, and it has no concept of our candidate profile.
INJECTED_KEYS = ("profile", "extracted_text")

BACKUP_DIR = Path("data/backups")


def _strip(raw: dict) -> tuple[dict, list[str]]:
    """Drop injected keys, at every depth the nesting reached."""
    cleaned = copy.deepcopy(raw)
    removed: list[str] = []
    for key in INJECTED_KEYS:
        if key in cleaned:
            removed.append(key)
            cleaned.pop(key)
    return cleaned, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes to MongoDB")
    args = parser.parse_args()

    coll = get_candidates_collection()
    docs = list(coll.find({}))
    print(f"Scanning {len(docs)} candidate record(s)\n")

    backup: list[dict] = []
    touched = 0

    for doc in docs:
        cid = doc["_id"]
        name = (doc.get("profile") or {}).get("full_name") or "(no name)"
        top = doc.get("raw_ocr")
        inner = (doc.get("profile") or {}).get("raw_ocr")

        source = top if isinstance(top, dict) and top else inner
        if not isinstance(source, dict) or not source:
            print(f"- {cid}  {name}: no raw_ocr stored, skipped")
            continue

        cleaned, removed = _strip(source)
        needs_sync = cleaned != top or cleaned != inner

        if not removed and not needs_sync:
            print(f"= {cid}  {name}: already verbatim ({len(cleaned)} keys)")
            continue

        touched += 1
        detail = f"removed {removed}" if removed else "re-synced copies"
        print(f"* {cid}  {name}: {detail}; keys now {len(cleaned)}")

        backup.append({"_id": cid, "raw_ocr": top, "profile_raw_ocr": inner})

        if args.apply:
            coll.update_one(
                {"_id": cid},
                {"$set": {
                    "raw_ocr": cleaned,
                    "profile.raw_ocr": cleaned,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )

    print(f"\n{touched} record(s) need repair.")
    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply to write.")
        return 0

    if backup:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = BACKUP_DIR / f"raw_ocr_backup_{stamp}.json"
        path.write_text(json.dumps(backup, indent=2, default=str), encoding="utf-8")
        print(f"Previous payloads backed up to {path}")
    print("Applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
