"""Find, and optionally restore, manual assignments the old poll step undid.

Before the fix, every mail poll ran `rebalance_all`, which moved candidates
staff had reassigned by hand. Those automatic moves wrote no history entry, so
the evidence is a candidate whose *latest* ownership entry is a manual move to
one person while the record is now owned by somebody else.

    python scripts/restore_reverted_manual_assignments.py            # report only
    python scripts/restore_reverted_manual_assignments.py --apply    # restore

Only untouched profiles are restored automatically: if the current (automatic)
owner has since opened or judged the candidate, restoring would throw that work
away, so those are listed for a person to decide. A restore goes back to the
staff member the manual move chose, provided that account is still active, and
is recorded in `assignment_history` with reason `restore_manual_assignment`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.models import utcnow  # noqa: E402
from app.db.mongo import get_candidates_collection  # noqa: E402
from app.db.users import UserRepository  # noqa: E402


def _is_manual(entry: dict) -> bool:
    return entry.get("reason") == "manual_reassignment" or entry.get("type") == "reassignment"


def find_reverted(coll) -> list[dict]:
    rows = []
    cursor = coll.find(
        {"assignment_history.0": {"$exists": True}},
        {
            "assigned_staff_id": 1, "assigned_staff_name": 1, "viewed_at": 1,
            "evaluation_status": 1, "profile.full_name": 1,
            "assignment_history": {"$slice": -1},
        },
    )
    for doc in cursor:
        latest = (doc.get("assignment_history") or [{}])[-1]
        if not isinstance(latest, dict) or not _is_manual(latest):
            continue
        chosen = latest.get("to_staff_id")
        if chosen and chosen != doc.get("assigned_staff_id"):
            rows.append({"doc": doc, "entry": latest, "chosen": chosen})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the restores")
    args = parser.parse_args()

    coll = get_candidates_collection()
    active = {member.id: member for member in UserRepository().list_assignable_staff()}

    restorable, review = [], []
    for row in find_reverted(coll):
        doc = row["doc"]
        untouched = not doc.get("viewed_at") and (doc.get("evaluation_status") or "pending") == "pending"
        if row["chosen"] in active and untouched:
            restorable.append(row)
        else:
            review.append(row)

    def describe(row):
        doc, entry = row["doc"], row["entry"]
        name = (doc.get("profile") or {}).get("full_name") or doc["_id"]
        return (
            f"{doc['_id']}  {name}: now {doc.get('assigned_staff_name') or doc.get('assigned_staff_id')}"
            f", manually moved to {entry.get('to_staff_name') or row['chosen']}"
            f" at {entry.get('at')}"
        )

    print(f"{len(restorable)} candidate(s) can be restored to their manual owner:")
    for row in restorable:
        print("  " + describe(row))
    print(f"{len(review)} need a person to decide (opened/judged since, or chosen staff inactive):")
    for row in review:
        print("  " + describe(row))

    if not args.apply:
        print("\nReport only. Re-run with --apply to restore the first group.")
        return 0

    restored = 0
    for row in restorable:
        doc, member = row["doc"], active[row["chosen"]]
        now = utcnow()
        # Compare-and-set on the owner we just read, so a move made meanwhile wins.
        result = coll.update_one(
            {"_id": doc["_id"], "assigned_staff_id": doc.get("assigned_staff_id"), "viewed_at": None},
            {
                "$set": {
                    "assigned_staff_id": member.id,
                    "assigned_staff_name": member.name,
                    "assigned_at": now,
                    "manually_assigned": True,
                    "updated_at": now,
                },
                "$push": {"assignment_history": {
                    "from_staff_id": doc.get("assigned_staff_id"),
                    "from_staff_name": doc.get("assigned_staff_name"),
                    "to_staff_id": member.id,
                    "to_staff_name": member.name,
                    "by_user_id": None,
                    "by_user_name": "system repair",
                    "remarks": "Restored manual assignment undone by automatic rebalance",
                    "reason": "restore_manual_assignment",
                    "at": now,
                }},
            },
        )
        restored += result.modified_count
    print(f"\nRestored {restored} of {len(restorable)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
