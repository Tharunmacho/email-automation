"""Report on ingestion rows the reconciler keeps retrying and can never finish.

Why this exists
---------------
The reconciler's job is to go back for OCR answers the pipeline could not wait
out. It re-reads the stored résumé, or failing that re-downloads the attachment
from the mailbox. When *both* sources are gone the row is not recoverable — but
nothing says so, so the sweep picks it up again on every tick, fails the same
way, and writes the same wall of warnings for ever:

    Could not read 2026/08/..._Muhammad Usman CV Bus Driver.pdf from storage:
      No file in GridFS bucket 'resumes' for key '...'
    Could not re-download attachment ANGjdJ9G... : UID command error: BAD
      [b'Error in IMAP command UID FETCH: Invalid uidset']

That is what a database migration leaves behind: the rows move, the GridFS
files do not, and the stored IMAP attachment ids stop resolving.

What this does, and does not, do
--------------------------------
**It never deletes anything.** Not a row, not a file, not a ledger entry. By
default it writes nothing at all — it reads, and it prints a table.

With ``--abandon`` it makes exactly one kind of change: setting a row's
``status`` to ``abandoned`` for rows it has proved are unrecoverable. That is
not a deletion. ``abandoned`` is a terminal state the reconciler stops picking
up, and rows in it are precisely what ``IngestionStateStore.review_queue()``
returns — so an abandoned row is *more* visible to an operator afterwards, not
less. It can be moved back by setting the status again.

    python scripts/inspect_stuck_ingestion.py                # report only
    python scripts/inspect_stuck_ingestion.py --abandon      # + mark the dead ones

Check the banner before using ``--abandon``. This connects to whatever
``MONGO_URI``/``MONGO_DB`` the local ``.env`` names, and a laptop's ``.env``
does not necessarily point at the same database the production API uses.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.db.ingestion_state import (
    ABANDONED,
    IngestionStateStore,
    get_ingestion_state_collection,
)
from app.db.mongo import get_candidates_collection, get_db


def _redacted_host(uri: str) -> str:
    """The server a URI points at, without the credentials in front of it."""
    tail = uri.split("@")[-1]
    return tail.split("/")[0] or "?"


def _banner() -> None:
    print("=" * 72)
    print("  server     :", _redacted_host(settings.mongo_uri))
    print("  database   :", settings.mongo_db)
    print("  storage    :", settings.storage_backend)
    print("=" * 72)


def _storage_has(key: str) -> bool:
    if not key:
        return False
    try:
        from app.storage.factory import get_storage_backend

        return bool(get_storage_backend().exists(key))
    except Exception:  # noqa: BLE001 — an unreachable backend is not a verdict
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stuck-after", type=int, default=900,
        help="seconds since a row was last touched before it counts as stuck (default 900)",
    )
    parser.add_argument("--limit", type=int, default=200, help="rows to examine (default 200)")
    parser.add_argument(
        "--abandon", action="store_true",
        help="mark unrecoverable rows 'abandoned' so the reconciler stops retrying "
             "them. Updates a status field; deletes nothing.",
    )
    args = parser.parse_args()

    _banner()

    store = IngestionStateStore()
    rows = store.find_stuck(args.stuck_after, limit=args.limit)
    if not rows:
        print("No stuck ingestion rows. Nothing to report.")
        return 0

    coll = get_ingestion_state_collection()
    candidates = get_candidates_collection()
    ledger = get_db()["ingest_ledger"]

    unrecoverable: list = []
    print(f"\n{len(rows)} stuck row(s):\n")
    for row in rows:
        key = getattr(row, "storage_key", "") or ""
        has_file = _storage_has(key)
        has_candidate = bool(
            row.candidate_id and candidates.count_documents({"_id": row.candidate_id}, limit=1)
        )
        in_ledger = bool(
            row.message_id and ledger.count_documents({"message_id": row.message_id}, limit=1)
        )

        # Recoverable means the reconciler has something left to work with. Only
        # the stored file is checked here: re-downloading from the mailbox is
        # the reconciler's own fallback and it is the half that has been failing
        # on stale attachment ids, so a row with no file is reported as beyond
        # this script's ability to confirm either way.
        verdict = "has stored file" if has_file else "NO stored file"
        if not has_file:
            unrecoverable.append(row)

        print(f"  {str(row.id)[:34]:36} status={row.status:11} {verdict}")
        print(f"      file      : {key or '(none recorded)'}")
        print(f"      candidate : {row.candidate_id or '(none)'}"
              f"{'' if has_candidate or not row.candidate_id else '  <-- candidate row is gone'}")
        print(f"      message   : {row.message_id or '(none)'}"
              f"{'  (in ledger)' if in_ledger else '  (not in ledger)'}")

    print(f"\n{len(unrecoverable)} of {len(rows)} row(s) have no stored file to re-read.")

    if not args.abandon:
        print(
            "\nRead-only run — nothing was written.\n"
            "Re-run with --abandon to mark those rows 'abandoned', which stops the\n"
            "reconciler retrying them. It sets a status field; it deletes nothing,\n"
            "and abandoned rows are what review_queue() shows an operator."
        )
        return 0

    if not unrecoverable:
        print("Nothing to mark.")
        return 0

    from app.core.models import utcnow

    marked = 0
    for row in unrecoverable:
        result = coll.update_one(
            {"_id": row.id},
            {"$set": {
                "status": ABANDONED,
                "updated_at": utcnow(),
                # Kept so the reason survives with the row, and so a future
                # reader can tell this apart from a row the pipeline itself gave
                # up on after exhausting its attempts.
                "abandoned_reason": "stored file missing; not recoverable by the reconciler",
                "abandoned_by": "scripts/inspect_stuck_ingestion.py",
            }},
        )
        marked += result.modified_count
    print(f"\nMarked {marked} row(s) abandoned. No rows, files or ledger entries were deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
