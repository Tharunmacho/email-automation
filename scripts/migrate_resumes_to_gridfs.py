"""Move every stored résumé into MongoDB, so the file travels with the record.

Why
---
With ``STORAGE_BACKEND=local`` the original uploads live in ``data/resumes`` on
whichever machine happened to run the ingestion. The candidate record — which
does travel, because it is in Mongo — then points at a file that only exists on
one disk, and every other instance serving that record can only fail the
download. GridFS puts the bytes in the same database as the record, so there is
one thing to back up and one place to look.

What it does
------------
For each candidate with a ``storage_key``:

* already in GridFS  → just correct ``resume.storage_backend`` and move on;
* on local disk      → copy into GridFS, verify the read-back, then update the
                       record;
* nowhere            → report it, so the gap is visible rather than discovered
                       by a recruiter clicking Download.

Nothing is deleted. The local copies stay exactly where they are until you are
satisfied the migration is good, and re-running this is safe.

    python scripts/migrate_resumes_to_gridfs.py --dry-run
    python scripts/migrate_resumes_to_gridfs.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.mongo import get_db  # noqa: E402
from app.storage.gridfs import GridFSStorageBackend  # noqa: E402
from app.storage.local import LocalStorageBackend  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would move, write nothing")
    args = parser.parse_args()

    db = get_db()
    gridfs = GridFSStorageBackend()
    local = LocalStorageBackend(settings.storage_local_dir)

    candidates = list(db["candidates"].find({}, {"_id": 1, "resume": 1, "profile.full_name": 1}))
    print(f"{len(candidates)} candidate(s) to check\n")

    already = moved = missing = no_key = 0
    moved_bytes = 0

    for candidate in candidates:
        resume = candidate.get("resume") or {}
        key = resume.get("storage_key")
        who = (candidate.get("profile") or {}).get("full_name") or candidate["_id"]

        if not key:
            no_key += 1
            continue

        if gridfs.exists(key):
            already += 1
            if resume.get("storage_backend") != "gridfs" and not args.dry_run:
                db["candidates"].update_one(
                    {"_id": candidate["_id"]},
                    {"$set": {"resume.storage_backend": "gridfs"}},
                )
            continue

        try:
            data = local.load(key)
        except Exception:
            missing += 1
            print(f"  MISSING  {who}: no file for key {key!r}")
            continue

        if args.dry_run:
            print(f"  would move {who}: {len(data) / 1e6:.1f} MB")
            moved += 1
            moved_bytes += len(data)
            continue

        gridfs.save(key, data, content_type=resume.get("mime_type"))
        # Read back before pointing the record at it: a half-written file that
        # the record now claims is authoritative is worse than no migration.
        if not gridfs.exists(key) or len(gridfs.load(key)) != len(data):
            print(f"  FAILED   {who}: GridFS read-back did not match; record left alone")
            continue

        db["candidates"].update_one(
            {"_id": candidate["_id"]},
            {"$set": {"resume.storage_backend": "gridfs"}},
        )
        moved += 1
        moved_bytes += len(data)
        print(f"  moved    {who}: {len(data) / 1e6:.1f} MB")

    print(
        f"\nalready in GridFS: {already}\n"
        f"{'would move' if args.dry_run else 'moved'}: {moved} ({moved_bytes / 1e6:.1f} MB)\n"
        f"file missing everywhere: {missing}\n"
        f"no storage key on the record: {no_key}"
    )
    if missing:
        print(
            "\nThe missing ones can still be recovered from the mailbox on the next "
            "download attempt — the API falls back to re-fetching the original email."
        )
    if not args.dry_run:
        print("\nNow set STORAGE_BACKEND=gridfs in .env so new résumés land there too.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
