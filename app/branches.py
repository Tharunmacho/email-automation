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

from app.assignment.balancer import GENERAL_DESK, SINGAPORE_MALAYSIA_DESK, desk_for_staff
from app.db.users import MANAGER_ROLE, normalize_branch

ROYAPETTAH = "Royapettah"
MOUNT_ROAD = "Mount Road"

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
    if manager is None or getattr(manager, "role", None) != MANAGER_ROLE:
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
    return employee.role != MANAGER_ROLE and manages(manager, employee, users)
