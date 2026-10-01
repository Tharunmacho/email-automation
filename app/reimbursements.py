"""Expense reimbursements claimed by staff and paid through payroll.

An employee claims an amount, optionally with a photo or document of the bill.
Only the super admin (Yoosuf, `settings.sla_super_admin_name`) decides a claim.
An approved claim is paid in the payroll month it was approved in: its amount
is added to that month's net payable salary (see `app.payroll`).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import PurePath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from app.api.routes import current_user, users
from app.attendance.engine import local_day
from app.branches import can_see
from app.config import settings
from app.db.mongo import ensure_index, get_db
from app.db.notifications import REIMBURSEMENT_REQUEST, NotificationRepository
from app.db.users import ADMIN_ROLE, MANAGER_ROLE, STAFF_ROLE
from app.logging_config import get_logger
from app.storage.factory import get_storage_backend

log = get_logger(__name__)

router = APIRouter(prefix="/payroll/reimbursements", tags=["payroll"])
REIMBURSEMENTS = "payroll_reimbursements"
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ATTACHMENT_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class ReimbursementDecision(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=1000)


def _collection():
    return get_db()[REIMBURSEMENTS]


def _public(doc: dict | None) -> dict | None:
    if not doc:
        return None
    row = dict(doc)
    row["id"] = row.pop("_id")
    attachment = row.get("attachment")
    if attachment:
        row["attachment"] = {key: attachment.get(key) for key in ("filename", "content_type", "size")}
    for key in ("created_at", "decided_at"):
        if isinstance(row.get(key), datetime):
            row[key] = row[key].replace(tzinfo=row[key].tzinfo or timezone.utc).isoformat()
    return row


def super_admins() -> list:
    """The administrators who decide reimbursements: Yoosuf.

    Matched by name, as SLA escalation is. Should no active administrator carry
    that name, every administrator may decide, so claims never wait on nobody.
    """
    admins = users.list_admins()
    target = settings.sla_super_admin_name.strip().casefold()
    named = [admin for admin in admins if admin.name.strip().casefold() == target]
    return named or admins


def is_super_admin(user: dict) -> bool:
    return user.get("role") == ADMIN_ROLE and any(admin.id == user.get("id") for admin in super_admins())


def approved_amount(employee_id: str, year: int, month: int, db=None) -> float:
    """What approved claims add to this employee's net payable for the month."""
    collection = db[REIMBURSEMENTS] if db is not None else _collection()
    rows = collection.find({
        "employee_id": employee_id, "status": "approved",
        "payroll_year": year, "payroll_month": month,
    }, {"amount": 1})
    return round(sum(float(row.get("amount", 0) or 0) for row in rows), 2)


def _may_view(user: dict, employee_id: str) -> bool:
    if user.get("role") == ADMIN_ROLE or user.get("id") == employee_id:
        return True
    if user.get("role") == MANAGER_ROLE:
        employee = users.get(employee_id)
        return employee is not None and can_see(users.get(user["id"]), employee, users)
    return False


def _notify(user_ids: list[str], title: str, message: str) -> None:
    try:
        repository = NotificationRepository()
        for user_id in user_ids:
            repository.record(user_id, type=REIMBURSEMENT_REQUEST, title=title, message=message)
    except Exception as exc:  # The claim is durable even if its alert cannot be written.
        log.warning("Reimbursement notification failed: %s", exc)


@router.post("", status_code=201)
def submit_reimbursement(
    amount: float = Form(..., gt=0, le=10_000_000),
    description: str = Form(..., min_length=1, max_length=1000),
    expense_date: date = Form(...),
    attachment: UploadFile | None = File(default=None),
    user: dict = Depends(current_user),
) -> dict:
    """An employee's own claim, with an optional bill photo or document."""
    if user.get("role") not in {STAFF_ROLE, MANAGER_ROLE, ADMIN_ROLE}:
        raise HTTPException(status_code=403, detail="Employees only")
    claim_id = uuid.uuid4().hex
    doc = {
        "_id": claim_id,
        "employee_id": user["id"],
        "employee_name": user.get("name", ""),
        "amount": round(float(amount), 2),
        "description": description.strip(),
        "expense_date": expense_date.isoformat(),
        "status": "pending",
        "attachment": None,
        "created_at": datetime.now(timezone.utc),
    }
    if attachment is not None and attachment.filename:
        content_type = (attachment.content_type or "").lower()
        if not (content_type.startswith("image/") or content_type in ATTACHMENT_TYPES):
            raise HTTPException(status_code=415, detail="Attach an image, PDF, Word or Excel file.")
        data = attachment.file.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="Attachment must be 10 MB or smaller.")
        filename = PurePath(attachment.filename).name or "attachment"
        key = f"reimbursements/{claim_id}/{filename}"
        get_storage_backend().save(key, data, content_type)
        doc["attachment"] = {"key": key, "filename": filename, "content_type": content_type, "size": len(data)}
    _collection().insert_one(doc)
    _notify(
        [admin.id for admin in super_admins()],
        "Reimbursement request",
        f"{doc['employee_name']} claimed ₹{doc['amount']:,.2f}: {doc['description']}",
    )
    return {"status": "pending", "reimbursement": _public(doc)}


@router.get("")
def list_reimbursements(
    status: str | None = Query(default=None, pattern="^(pending|approved|rejected)$"),
    user: dict = Depends(current_user),
) -> dict:
    """Own claims for staff; the branch's for managers; everyone's for admins."""
    query: dict = {}
    if status:
        query["status"] = status
    if user.get("role") == STAFF_ROLE:
        query["employee_id"] = user["id"]
    rows = [
        _public(row) for row in _collection().find(query).sort("created_at", DESCENDING).limit(500)
        if _may_view(user, row["employee_id"])
    ]
    return {"items": rows, "can_decide": is_super_admin(user)}


@router.get("/{claim_id}/attachment")
def reimbursement_attachment(claim_id: str, user: dict = Depends(current_user)) -> Response:
    doc = _collection().find_one({"_id": claim_id})
    if not doc or not _may_view(user, doc["employee_id"]) or not doc.get("attachment"):
        raise HTTPException(status_code=404, detail="Attachment not found")
    attachment = doc["attachment"]
    data = get_storage_backend().load(attachment["key"])
    safe_name = attachment["filename"].replace('"', "")
    return Response(
        content=data,
        media_type=attachment.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


@router.post("/{claim_id}/decision")
def decide_reimbursement(claim_id: str, payload: ReimbursementDecision, user: dict = Depends(current_user)) -> dict:
    """Yoosuf approves or rejects. Approval pays it in this month's payroll."""
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail=f"Only {settings.sla_super_admin_name} can decide reimbursements")
    now = datetime.now(timezone.utc)
    payroll_day = local_day(now)
    updates = {
        "status": "approved" if payload.approved else "rejected",
        "decision_note": payload.note.strip(),
        "decided_by": user["id"],
        "decided_by_name": user.get("name", ""),
        "decided_at": now,
    }
    if payload.approved:
        updates.update(payroll_year=payroll_day.year, payroll_month=payroll_day.month)
    doc = _collection().find_one_and_update(
        {"_id": claim_id, "status": "pending"}, {"$set": updates}, return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=409, detail="Pending reimbursement not found")
    outcome = (
        f"approved and added to your {payroll_day.strftime('%B %Y')} salary"
        if payload.approved else "rejected"
    )
    _notify([doc["employee_id"]], "Reimbursement " + doc["status"],
            f"Your claim of ₹{doc['amount']:,.2f} was {outcome}.")
    return {"status": doc["status"], "reimbursement": _public(doc)}


def ensure_reimbursement_indexes() -> None:
    collection = get_db()[REIMBURSEMENTS]
    ensure_index(collection, [("employee_id", ASCENDING), ("created_at", DESCENDING)], "reimbursement_employee")
    ensure_index(
        collection,
        [("employee_id", ASCENDING), ("status", ASCENDING), ("payroll_year", ASCENDING), ("payroll_month", ASCENDING)],
        "reimbursement_payroll_period",
    )
