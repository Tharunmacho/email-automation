"""Rename existing candidates to "Given-names Surname" from their passport.

New passports do this as they are filed (`app.db.identity_records`); this
applies the same rule to candidates whose passport was filed before that.

    python scripts/backfill_passport_names.py           # show what would change
    python scripts/backfill_passport_names.py --apply   # write the names
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.identity_records import apply_passport_name, get_passport_collection, passport_name  # noqa: E402
from app.db.mongo import get_db  # noqa: E402
from app.db.repository import _id_filter  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the names (default: dry run)")
    args = parser.parse_args()

    candidates = get_db()[settings.mongo_candidates_collection]
    changed = 0
    # Newest passport first, so a candidate with two takes the latest reading.
    seen: set[str] = set()
    for doc in get_passport_collection().find({"candidate_id": {"$nin": [None, ""]}}).sort("updated_at", -1):
        candidate_id = str(doc["candidate_id"])
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        name = passport_name(doc)
        if not name:
            continue
        current = candidates.find_one(_id_filter(candidate_id), {"profile.full_name": 1})
        if not current:
            continue
        old = (current.get("profile") or {}).get("full_name") or ""
        if old == name:
            continue
        changed += 1
        print(f"{candidate_id}: {old!r} -> {name!r}")
        if args.apply:
            apply_passport_name(candidate_id, doc)
    print(f"{changed} candidate(s) {'renamed' if args.apply else 'would be renamed'}.")


if __name__ == "__main__":
    main()
