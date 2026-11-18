"""Find, and optionally fix, WhatsApp candidates sitting on the wrong country desk.

Before the fix, the allocation sweep that runs after every mail poll (and on
every Staff workload load) placed WhatsApp registrations while the candidate was
still answering. The destination was often still blank, a blank destination is
the other-countries desk (Mount Road), and finishing the registration with
"Singapore" or "Malaysia" did not move them, because they already had an owner.

    python scripts/correct_whatsapp_desks.py            # report only
    python scripts/correct_whatsapp_desks.py --apply    # move the untouched ones

Only profiles nobody has worked are moved: automatically placed, never opened,
not judged. Each move goes through `correct_desk` (next in rotation on the right
desk) and is recorded in `assignment_history` with reason `desk_correction`.
Anything opened, judged or assigned by hand is listed for a person to move with
the Reassign action on the candidate, because moving it would discard their work
or overrule their choice.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.assignment.balancer import (  # noqa: E402
    DESK_LABELS,
    correct_desk,
    desk_for_candidate,
    desk_for_staff,
)
from app.db.mongo import get_candidates_collection  # noqa: E402
from app.db.repository import CandidateRepository  # noqa: E402
from app.db.users import UserRepository  # noqa: E402


def _untouched(doc: dict) -> bool:
    return (
        not doc.get("manually_assigned")
        and not doc.get("viewed_at")
        and (doc.get("evaluation_status") or "pending") == "pending"
    )


def find_misplaced(coll, users: UserRepository) -> list[dict]:
    everyone = {member.id: member for member in users.list_staff(include_inactive=True)}
    everyone.update({member.id: member for member in users.list_employees(include_inactive=True)})
    rows = []
    cursor = coll.find(
        {
            "source": "whatsapp",
            "assigned_staff_id": {"$ne": None},
            # Finished registrations only; one still in progress is corrected
            # by the intake on the delivery that completes it.
            "registration.complete": {"$ne": False},
        },
        {
            "assigned_staff_id": 1, "assigned_staff_name": 1, "viewed_at": 1,
            "evaluation_status": 1, "manually_assigned": 1,
            "profile.full_name": 1, "profile.destination_country": 1,
        },
    ).sort("created_at", 1)
    for doc in cursor:
        owner = everyone.get(doc.get("assigned_staff_id"))
        if owner is None:
            continue  # orphaned: re-homed from the admin console instead
        if desk_for_staff(owner) != desk_for_candidate(doc):
            rows.append({"doc": doc, "owner": owner})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="move the untouched profiles")
    args = parser.parse_args()

    users = UserRepository()
    repo = CandidateRepository()
    rows = find_misplaced(get_candidates_collection(), users)
    if not rows:
        print("No WhatsApp candidate is on the wrong desk.")
        return 0

    moved = 0
    manual: list[str] = []
    for row in rows:
        doc, owner = row["doc"], row["owner"]
        profile = doc.get("profile") or {}
        label = (
            f"{doc['_id']}  {profile.get('full_name') or '(no name)'}  "
            f"destination={profile.get('destination_country') or '-'}  "
            f"owner={owner.name} ({DESK_LABELS[desk_for_staff(owner)]})"
        )
        if not _untouched(doc):
            manual.append(label)
            continue
        if not args.apply:
            print(f"would move  {label}")
            continue
        result = correct_desk(str(doc["_id"]), profile, owner.id, repo=repo, users=users)
        if result.assigned:
            moved += 1
            print(f"moved       {label}  ->  {result.staff_name}")
        else:
            print(f"left        {label}  ({result.reason})")

    if manual:
        print("\nOpened, judged or hand-assigned; move these with Reassign if they should go:")
        for label in manual:
            print(f"  {label}")

    if args.apply:
        print(f"\n{moved} moved, {len(manual)} left for a person.")
    else:
        print(f"\n{len(rows) - len(manual)} would move, {len(manual)} need a person. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
