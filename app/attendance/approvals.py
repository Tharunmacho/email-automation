"""Who has to approve an attendance request, and how far along it is.

Most requests need one decision: the requester's branch manager, or the
administrators for a manager's own request. Requests that move money — paid
leave, LOP (unpaid leave) and Extra OT — also need the finance manager. Both
stages must approve before the request takes effect; a rejection at either
stage rejects it outright.

The two stages are independent and may be decided in either order. When one
person answers for both — Noorul is Royapettah's branch manager *and* the
finance manager — one decision fills both, so nobody approves the same request
twice.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.branches import branch_managers, branch_of, finance_managers, manages
from app.db.users import ADMIN_ROLE, MANAGER_ROLES

MANAGER_STAGE = "manager"
FINANCE_STAGE = "finance"

#: Permission kinds that change pay, and so need the finance manager too.
FINANCE_PERMISSION_KINDS = frozenset({"paid_leave", "unpaid_leave"})

PERMISSION = "permission"
EXTRA_OT = "extra_ot"


def required_stages(request_type: str, row: dict) -> tuple[str, ...]:
    """The stages this request must clear, in display order."""
    if request_type == EXTRA_OT or row.get("kind") in FINANCE_PERMISSION_KINDS:
        return (MANAGER_STAGE, FINANCE_STAGE)
    return (MANAGER_STAGE,)


def _admins(users) -> list:
    return getattr(users, "list_admins", lambda: [])()


def stage_approvers(stage: str, employee, users) -> list:
    """Who decides one stage of this employee's request.

    Manager stage: the branch manager, or the administrators when the requester
    is a manager themselves. Finance stage: the finance managers, never the
    requester; the administrators when there is nobody else to ask.
    """
    if stage == FINANCE_STAGE:
        return finance_managers(employee, users) or _admins(users)
    if employee is None:
        return getattr(users, "list_managers", lambda: [])()
    if employee.role in MANAGER_ROLES:
        return _admins(users)
    return branch_managers(employee, users)


def approvers_for(request_type: str, row: dict, employee, users) -> list:
    """Everybody who should hear about a new request, once each."""
    seen: dict[str, object] = {}
    for stage in required_stages(request_type, row):
        for approver in stage_approvers(stage, employee, users):
            seen.setdefault(approver.id, approver)
    return list(seen.values())


def stages_for(viewer, request_type: str, row: dict, employee, users) -> list[str]:
    """The stages of this request the viewer may decide, decided or not.

    An administrator may always decide the manager stage, as before; the
    finance stage only when no finance manager other than the requester exists.
    """
    if viewer is None or (employee is not None and viewer.id == employee.id):
        return []
    is_admin = viewer.role == ADMIN_ROLE
    stages = []
    for stage in required_stages(request_type, row):
        if stage == FINANCE_STAGE:
            signers = finance_managers(employee, users)
            allowed = any(signer.id == viewer.id for signer in signers) or (is_admin and not signers)
        elif employee is None:
            allowed = is_admin or viewer.role in MANAGER_ROLES
        elif employee.role in MANAGER_ROLES:
            allowed = is_admin
        else:
            # `manages` lets any manager act when the branch has none on record.
            allowed = is_admin or manages(viewer, employee, users)
        if allowed:
            stages.append(stage)
    return stages


def outstanding(request_type: str, row: dict) -> list[str]:
    """Required stages nobody has approved yet."""
    approvals = row.get("approvals") or {}
    return [
        stage for stage in required_stages(request_type, row)
        if not (approvals.get(stage) or {}).get("approved")
    ]


def stage_record(viewer, approved: bool, reason: str) -> dict:
    return {
        "approved": approved,
        "by": viewer.id,
        "by_name": viewer.name or viewer.email,
        "reason": reason,
        "at": datetime.now(timezone.utc),
    }


def describe(viewer, request_type: str, row: dict, employee, users) -> dict:
    """``row`` with what a screen needs to draw its approval state."""
    stages = required_stages(request_type, row)
    waiting = outstanding(request_type, row) if row.get("status") == "pending" else []
    mine = stages_for(viewer, request_type, row, employee, users)
    row["approval_stages"] = list(stages)
    row["approvals"] = row.get("approvals") or {}
    row["awaiting_stages"] = waiting
    row["can_decide"] = any(stage in waiting for stage in mine)
    if employee is not None:
        row.setdefault("employee_name", employee.name or employee.email)
        row.setdefault("employee_branch", branch_of(employee))
    return row
