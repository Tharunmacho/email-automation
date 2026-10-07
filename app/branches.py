"""Which branch an employee belongs to, and which manager answers for it.

The agency runs two branches, split on the same line as candidate allocation:

* **Royapettah** — the Singapore and Malaysia desk, managed by Noorul.
* **Mount Road** — every other destination, managed by Rafi.

Payroll groups by branch, and leave, permission and Extra OT requests are
routed to the manager of the requester's branch. Both read `branch_of`, so an
employee can never be payrolled at one branch and approved by the other.

Managers are not named here. A manager belongs to a branch exactly as staff do
(by an explicit branch, else by desk), so moving Rafi or Noorul — or adding a
second manager to a branch — is a User Management change, not a code change.
"""
from __future__ import annotations

import re
import uuid

from app.assignment.balancer import GENERAL_DESK, SINGAPORE_MALAYSIA_DESK, desk_for_staff
from app.core.models import utcnow
from app.db.mongo import get_db
from app.db.users import ADMIN_ROLE, MANAGER_ROLES, normalize_branch

ROYAPETTAH = "Royapettah"
MOUNT_ROAD = "Mount Road"

#: The branches the console switches between, in display order.
BRANCHES = (ROYAPETTAH, MOUNT_ROAD)

#: Further offices added from Data Management, one document per branch.
BRANCHES_COLLECTION = "branches"


def _branch_collection(db=None):
    return (db if db is not None else get_db())[BRANCHES_COLLECTION]


def all_branches(db=None) -> list[str]:
    """The two original branches, then every branch added since, oldest first."""
    names = list(BRANCHES)
    for row in _branch_collection(db).find({}, {"name": 1}).sort("created_at", 1):
        name = normalize_branch(row.get("name"))
        if name and not any(name.casefold() == seen.casefold() for seen in names):
            names.append(name)
    return names


def add_branch(name: str, actor_id: str, db=None) -> str:
    """Register a new branch. Raises ValueError for a blank or existing name."""
    name = normalize_branch(name)
    if not name:
        raise ValueError("Branch name is required.")
    if any(name.casefold() == existing.casefold() for existing in all_branches(db)):
        raise ValueError(f"The branch {name} already exists.")
    _branch_collection(db).insert_one({
        "_id": uuid.uuid4().hex, "name": name, "created_by": actor_id, "created_at": utcnow(),
    })
    return name


def remove_branch(name: str, db=None) -> bool:
    """Remove an added branch. The two original branches cannot be removed."""
    name = normalize_branch(name)
    if any(name.casefold() == fixed.casefold() for fixed in BRANCHES):
        raise ValueError(f"{name} is one of the original branches and cannot be removed.")
    result = _branch_collection(db).delete_many(
        {"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}
    )
    return result.deleted_count > 0


#: The branch each allocation desk works from.
DESK_BRANCH_NAMES = {
    SINGAPORE_MALAYSIA_DESK: ROYAPETTAH,
    GENERAL_DESK: MOUNT_ROAD,
}


def branch_of(employee) -> str:
    """Which branch this employee belongs to.

    An explicitly assigned branch wins — an administrator who typed one into
    User Management meant it. Everyone else falls to their desk's branch, so
    every employee lands in a branch.
    """
    explicit = normalize_branch(getattr(employee, "branch", ""))
    return explicit or DESK_BRANCH_NAMES[desk_for_staff(employee)]


def display_branch(employee) -> str:
    """The branch a screen should file this account under.

    Staff and managers always belong to one (`branch_of`). An administrator is
    not on either desk, so they appear under a branch only when one was typed in
    for them; otherwise they are shown under every branch's "All" view only.
    """
    if getattr(employee, "role", None) == ADMIN_ROLE:
        return normalize_branch(getattr(employee, "branch", ""))
    return branch_of(employee)


def with_branch(employee, row: dict) -> dict:
    """``row`` (a public user dict) with the account's effective branch added."""
    row["effective_branch"] = display_branch(employee)
    return row


def same_branch(first, second) -> bool:
    return branch_of(first).casefold() == branch_of(second).casefold()


def branch_managers(employee, users) -> list:
    """The active managers who approve this employee's requests.

    The managers of the employee's own branch. When that branch has no manager
    — a third office nobody has been put in charge of yet — every manager is
    returned instead, so a request is never left with nobody to decide it.
    """
    managers = [
        manager for manager in getattr(users, "list_managers", lambda: [])()
        if manager.id != employee.id
    ]
    own = [manager for manager in managers if same_branch(manager, employee)]
    return own or managers


def manages(manager, employee, users) -> bool:
    """Whether this manager may see and decide this employee's requests."""
    if manager is None or getattr(manager, "role", None) not in MANAGER_ROLES:
        return False
    responsible = branch_managers(employee, users)
    # Nobody on record at all: any manager may act rather than nobody.
    return not responsible or any(candidate.id == manager.id for candidate in responsible)


def can_see(manager, employee, users) -> bool:
    """Whether a manager may view this employee's attendance and payroll.

    Themselves, and the staff of their own branch. Not the other branch, and
    not another manager — a manager's records are an administrator's concern.
    """
    if manager is None:
        return False
    if employee.id == manager.id:
        return True
    return employee.role not in MANAGER_ROLES and manages(manager, employee, users)


def finance_managers(employee, users) -> list:
    """The active finance managers who countersign this employee's money requests.

    Finance is not split by branch: Noorul signs for Royapettah and Mount Road
    alike. Nobody countersigns their own request, so a finance manager's own
    leave falls to whoever else holds the role — or, if nobody does, to the
    administrators (see `app.attendance.approvals`).
    """
    employee_id = getattr(employee, "id", None)
    return [
        manager for manager in getattr(users, "list_finance_managers", lambda: [])()
        if manager.id != employee_id
    ]
