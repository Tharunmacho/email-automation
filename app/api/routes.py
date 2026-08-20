"""FastAPI application — REST seam for the future recruiter dashboard / frontend.

Deliberately minimal for now: health, candidate list/detail, resume download, and
a manual poll trigger. Search, filtering, ranking, JD-matching, scoring, and auth
all slot in here later without touching the ingestion pipeline.

Run:
    uvicorn app.api.routes:app --reload --port 8000
"""
from __future__ import annotations

import os
import threading

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from app.config import settings
from app.core.models import EVALUATION_STATUSES, CandidateProfile
from app.core.security import create_token, read_token
from app.assignment.balancer import (
    allocate_unassigned,
    assign_candidate,
    rebalance_all,
    redistribute_from_staff,
    rehome_orphans,
)
from app.db.mongo import ensure_indexes
from app.db.notifications import NotificationRepository
from app.db.repository import CandidateRepository
from app.db.users import (
    ADMIN_ROLE,
    STAFF_ROLE,
    UserRepository,
    ensure_demo_accounts,
    ensure_seed_user,
)
from app.notifications import notify_candidate_assigned
from app.storage.factory import get_storage_backend
from app.tasks import sla_checker
from app.api.websocket import router as websocket_router

from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from app.logging_config import get_logger

log = get_logger(__name__)

app = FastAPI(
    title="Resume Ingestion API",
    version="0.1.0",
    description="Structured candidate profiles ingested from resume emails.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(websocket_router)


@app.exception_handler(ServerSelectionTimeoutError)
async def mongo_timeout_exception_handler(request: Request, exc: ServerSelectionTimeoutError):
    return JSONResponse(
        status_code=503,
        content={"detail": "Database connection timeout. Please verify internet connection or MongoDB Atlas IP whitelist."},
    )


@app.exception_handler(PyMongoError)
async def mongo_exception_handler(request: Request, exc: PyMongoError):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Database operation failed: {exc}"},
    )


@app.on_event("startup")
def _seed_admin() -> None:
    """Create the initial admin account once. Never resets an existing one."""
    try:
        ensure_seed_user(settings.admin_email, settings.admin_password)
    except Exception:  # noqa: BLE001 — the API must still boot without it
        pass

    # The accounts the login screen advertises. Separate from the operator's own
    # admin above, and create-only: changing a demo password must survive a
    # restart.
    if settings.demo_accounts_enabled:
        try:
            ensure_demo_accounts(
                settings.demo_admin_email,
                settings.demo_admin_password,
                settings.demo_staff_email,
                settings.demo_staff_password,
            )
        except Exception:  # noqa: BLE001
            pass


@app.on_event("startup")
def _startup() -> None:
    try:
        ensure_indexes()
    except Exception as exc:
        import logging
        logging.getLogger("uvicorn.error").warning("MongoDB index creation deferred: %s", exc)





def repo() -> CandidateRepository:
    return CandidateRepository()


# --------------------------------------------------------------------------- #
#  Auth
# --------------------------------------------------------------------------- #
users = UserRepository()


class LoginRequest(BaseModel):
    email: str
    password: str


def current_user(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> dict:
    """Resolve `Authorization: Bearer <token>` or `?token=<token>` into a user, or 401."""
    raw_token = ""
    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()
    elif token:
        raw_token = token.strip()
    subject = read_token(raw_token, settings.auth_secret)
    if not subject:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = users.get(subject)
    if not user:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    return user.to_public()


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Super Admin role required")
    return user


def _staff_scope(user: dict) -> str | None:
    """Return the staff_id when scoped to a staff member, or None for admin."""
    return user["id"] if user.get("role") == STAFF_ROLE else None


def _owned_or_404(candidate_id: str, user: dict):
    """The record, or 404 if it does not exist *or* belongs to someone else.

    404 rather than 403 on purpose: 403 confirms the record exists, which is the
    fact the isolation rule is there to withhold. Another staff member's id has
    to be indistinguishable from one that was never issued.
    """
    record = repo().get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if user.get("role") == STAFF_ROLE and record.assigned_staff_id != user["id"]:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return record


@app.post("/auth/login")
def login(payload: LoginRequest) -> dict:
    user = users.authenticate(payload.email, payload.password)
    if not user:
        # One message for both cases, so the response cannot be used to work
        # out which email addresses have accounts.
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_token(
        subject=user.id,
        secret=settings.auth_secret,
        ttl_seconds=settings.auth_token_ttl_hours * 3600,
    )
    return {
        "token": token,
        "user": user.to_public(),
        "expires_in": settings.auth_token_ttl_hours * 3600,
    }


@app.get("/auth/demo-accounts")
def demo_accounts() -> dict:
    """The quick-fill button on the login screen.

    One way in, and it is the admin console — the staff account exists and signs
    in normally, it is just not published here. This endpoint is
    unauthenticated, so anything it lists is a credential handed to whoever
    loads the page; turning demo mode off has to silence it completely rather
    than merely hide the button.
    """
    if not settings.demo_accounts_enabled:
        return {"enabled": False, "accounts": []}

    return {
        "enabled": True,
        "accounts": [
            {
                "role": "admin",
                "label": "Super Admin",
                "description": "Full access: allocation, staff accounts and SLA.",
                "email": settings.admin_email,
                "password": settings.admin_password,
            },
        ],
    }


@app.get("/config")
def get_ui_config(_user: dict = Depends(current_user)) -> dict:
    """UI configuration (SLA threshold, auto-assign flag)."""
    return {
        "sla_threshold_hours": settings.sla_threshold_hours,
        "auto_assign_enabled": settings.auto_assign_enabled,
    }


@app.get("/auth/me")
def whoami(user: dict = Depends(current_user)) -> dict:
    """Used by the frontend on load to decide whether a stored token is valid."""
    return {"user": user}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "candidates": repo().count()}


@app.get("/candidates")
def list_candidates(
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    view: str = Query(
        "list",
        pattern="^(list|minimal)$",
        description="'list' for the directory row; 'minimal' for id/name/email/"
                    "phone/status/confidence/created_at only.",
    ),
    user: dict = Depends(current_user),
) -> dict:
    """A page of candidates, projected in the database and scoped to the caller.

    Never the whole document. The OCR payload alone — stored twice per record,
    under `raw_ocr` and again under `profile.raw_ocr` — put megabytes into a
    200-row response that the frontend was re-fetching every five seconds. Full
    profiles come from `GET /candidates/{id}`, one candidate at a time, when
    something actually opens one.

    A staff member sees only what is allocated to them; an admin sees the lot.
    """
    repository = repo()
    staff_id = _staff_scope(user)
    items = repository.list_summaries(
        limit=limit, skip=skip, minimal=view == "minimal", staff_id=staff_id
    )

    # A first page that came back short *is* the whole collection, so counting it
    # again is a second round trip to Atlas for an answer already in hand — and
    # against a remote cluster the round trip, not the work, is the response
    # time. Any other page still has to ask.
    total = (
        len(items)
        if skip == 0 and len(items) < limit
        else repository.count(staff_id=staff_id)
    )

    return {
        "total": total,
        "count": len(items),
        "items": items,
    }


@app.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, user: dict = Depends(current_user)) -> dict:
    """The whole record, OCR payload included. The only place that serves it."""
    record = _owned_or_404(candidate_id, user)
    return record.model_dump(mode="json")


@app.get("/candidates/{candidate_id}/resume")
def download_resume(candidate_id: str, user: dict = Depends(current_user)) -> Response:
    import urllib.parse
    record = _owned_or_404(candidate_id, user)
    if not record.resume or not record.resume.storage_key:
        raise HTTPException(status_code=404, detail="Candidate resume attachment not found")
    
    backend_name = record.resume.storage_backend or settings.storage_backend
    try:
        data = get_storage_backend(backend_name).load(record.resume.storage_key)
    except Exception:
        # Fallback check: if record backend failed, try alternate storage backend (local vs gridfs)
        try:
            alt_backend = "local" if backend_name == "gridfs" else "gridfs"
            data = get_storage_backend(alt_backend).load(record.resume.storage_key)
        except Exception:
            filename = record.resume.original_filename or "resume.pdf"
            raise HTTPException(
                status_code=404,
                detail=f"Resume file '{filename}' is missing from server storage."
            )
    
    original_name = record.resume.original_filename or "resume.pdf"
    safe_filename = original_name.replace('"', '').replace("'", "")
    encoded_filename = urllib.parse.quote(safe_filename)
    
    return Response(
        content=data,
        media_type=record.resume.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"; filename*=UTF-8\'\'{encoded_filename}'
        },
    )


def _post_delete_cleanup(storage_key: str | None, message_ids: list[str]) -> None:
    """Slow, best-effort cleanup for a deleted candidate.

    Runs after the response is sent: building a Gmail service refreshes the
    OAuth token and then costs more round trips per message, so several seconds
    of network time would otherwise be charged to the caller. Neither step
    changes what the API returned, and both are safe to lose.
    """
    if storage_key:
        try:
            get_storage_backend().delete(storage_key)
        except Exception as err:
            log.warning("Could not delete stored resume %s: %s", storage_key, err)

    if message_ids:
        try:
            from app.email_client import get_email_client

            gmail = get_email_client()
            for message_id in message_ids:
                # Retire rather than free: the search excludes both labels, so
                # these emails never come back, while a *new* email carrying the
                # same resume arrives unlabelled and ingests as a new candidate.
                if settings.gmail_deleted_label:
                    gmail.apply_label(message_id, settings.gmail_deleted_label)
                if settings.gmail_processed_label:
                    gmail.remove_label(message_id, settings.gmail_processed_label)
            log.info(
                "Marked %d message(s) '%s' after candidate deletion",
                len(message_ids), settings.gmail_deleted_label,
            )
        except Exception as err:
            log.warning("Could not re-label Gmail messages %s: %s", message_ids, err)


@app.delete("/candidates/{candidate_id}")
@app.delete("/api/v1/candidates/{candidate_id}")
def delete_candidate(
    candidate_id: str,
    background: BackgroundTasks,
    _user: dict = Depends(current_user),
) -> dict:
    rec = repo().get(candidate_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Candidate not found")

    msg_id = rec.source_email.message_id if rec.source_email else None
    res_hash = rec.resume.sha256 if (rec.resume and rec.resume.sha256) else rec.resume_hash

    # Drop the document first: it is the authoritative step, so nothing else in
    # the handler can widen the window for a concurrent delete.
    try:
        removed = repo().delete(candidate_id)
    except Exception as err:
        log.exception("Deleting candidate %s failed", candidate_id)
        raise HTTPException(status_code=500, detail=f"Failed to delete candidate: {err}") from err

    if not removed:
        # A duplicate/concurrent DELETE won the race. The candidate is gone
        # either way, so report success instead of a spurious 500.
        log.info("Candidate %s was already deleted by a concurrent request", candidate_id)

    from app.db.ledger import IngestLedger

    ledger = IngestLedger()

    # Read the ledger before retiring it: the same resume often arrives on
    # several emails, and every one of them has to be retired or the next poll
    # brings the candidate back.
    message_ids = ledger.message_ids_for_candidate(candidate_id, resume_hash=res_hash)
    if msg_id and msg_id not in message_ids:
        message_ids.append(msg_id)

    # Tombstone the emails, free the file. The Gmail label alone is not enough:
    # its effect on search lags by a minute or more, and a poll inside that
    # window re-ingested the candidate that was just deleted.
    cleared = ledger.retire_candidate(candidate_id, message_ids, resume_hash=res_hash)

    storage_key = rec.resume.storage_key if rec.resume else None
    background.add_task(_post_delete_cleanup, storage_key, message_ids)

    return {
        "status": "success",
        "message": f"Candidate {candidate_id} deleted permanently",
        "cleared_entries": cleared,
    }


# Single-flight guard for the inline poll. The Celery path takes a Redis lock;
# this path had none, so two overlapping requests each ran a full batch over the
# same messages. Both would miss the dedup check, one would ingest, and the
# other — finishing later — reported the candidate as an existing duplicate. The
# UI showed that second summary: "Ingested=0" for a poll that had just added a
# profile. A plain lock is enough because this endpoint runs the batch in-process.
_inline_poll_lock = threading.Lock()


@app.post("/ingest/poll")
def trigger_poll(query: str | None = None, _user: dict = Depends(current_user)) -> dict:
    """Run one Gmail poll cycle inline and return its summary.

    Blocks for the whole batch (OCR + LLM per attachment), so it only suits
    small inboxes and local testing. Prefer `/ingest/poll/async` when a worker
    is running; this stays as the no-worker fallback.
    """
    from app.ingestion.runner import IngestionRunner
    from app.tasks.jobs import summary_to_dict

    if not _inline_poll_lock.acquire(blocking=False):
        log.info("Inline poll declined: another cycle is already running")
        return {
            "fetched": 0, "processed": 0, "skipped": 0, "suppressed": 0,
            "errors": 0, "ingested_candidates": 0, "results": [],
            "skipped_reason": "Another poll cycle is already running.",
        }

    try:
        return summary_to_dict(IngestionRunner().run_once(query=query))
    finally:
        _inline_poll_lock.release()


@app.get("/ingest/rules")
def ingest_rules(_user: dict = Depends(current_user)) -> dict:
    """The rules the ingestion pipeline actually applies, for the Email Rules screen.

    Read-only and deliberately hand-listed rather than dumped from `settings`:
    the settings object holds mailbox passwords and API keys, and a blanket
    serialisation would put them on a web page the first time someone adds a
    field. Credentials are reported only as "is this configured", never by value.
    """
    return {
        "provider": settings.email_provider,
        "mailbox": {
            # The address is shown so a recruiter can confirm which inbox is
            # being drained; the password behind it never leaves the server.
            "account": settings.imap_username or settings.smtp_username,
            "configured": bool(settings.imap_username and settings.imap_password),
            "inbox_folder": settings.imap_folder,
            "processed_folder": settings.imap_processed_folder,
            "deleted_folder": settings.imap_deleted_folder,
            "gmail_query": settings.gmail_query,
        },
        "gates": {
            "detector_min_score": settings.detector_min_score,
            "inspect_all_documents": settings.inspect_all_documents,
            "min_image_attachment_bytes": settings.min_image_attachment_bytes,
            "min_ingest_confidence": settings.min_ingest_confidence,
        },
        "attachments": {"accepted_extensions": settings.resume_extensions},
        "ignored_senders": settings.ignore_sender_fragments,
        "ocr": {
            "min_text_chars": settings.ocr_min_text_chars,
            "dpi": settings.ocr_dpi,
            "chunk_pages": settings.ocr_chunk_pages,
            "max_pages": settings.ocr_max_pages,
            "give_up_pages": settings.ocr_give_up_pages,
            "languages": settings.ocr_languages,
            "provider_configured": bool(settings.veris_ocr_api_key),
        },
        "extraction": {
            "model": settings.anthropic_model,
            "configured": bool(settings.anthropic_api_key),
        },
        "auto_reply": {"enabled": settings.auto_reply_enabled},
    }


# ---- Background ingestion ------------------------------------------------- #
# ---- Multipass OCR state machine ------------------------------------------ #
@app.get("/ingest/ocr-state")
def ocr_state(_user: dict = Depends(current_user)) -> dict:
    """How much OCR work is in flight, and how much of it is stuck.

    One number matters more than the rest: `abandoned`. Everything else drains
    on its own — a queued job finishes, a failed one is retried by the
    reconciler — but an abandoned row is waiting for a person.
    """
    from app.db.ingestion_state import IngestionStateStore

    try:
        counts = IngestionStateStore().status_counts()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Ingestion state unavailable: {exc}")

    queue: dict = {}
    if settings.veris_ocr_api_key:
        from app.extraction.jobs import AsyncOCRJobClient

        # Never fatal: this is the OCR service reporting on itself, and the
        # local state above is still worth serving when it cannot be reached.
        with AsyncOCRJobClient() as client:
            queue = client.queue_stats()

    return {
        "rows": counts,
        "in_flight": counts["received"] + counts["submitting"] + counts["running"],
        "needs_review": counts["abandoned"],
        "ocr_queue": queue,
        "config": {
            "async_jobs": settings.ocr_async_jobs_enabled,
            "multipass": settings.multipass_extraction_enabled,
            "max_attempts": settings.ocr_job_max_attempts,
            "stuck_after_seconds": settings.reconciler_stuck_after_seconds,
        },
    }


@app.get("/ingest/ocr-state/review")
def ocr_review_queue(limit: int = 100, _user: dict = Depends(current_user)) -> dict:
    """The rows that exhausted their retries and now need a human."""
    from app.tasks.reconciler import review_queue

    try:
        return {"items": review_queue(limit)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Ingestion state unavailable: {exc}")


@app.post("/ingest/ocr-state/reconcile")
def run_reconciler(limit: int | None = None, _user: dict = Depends(require_admin)) -> dict:
    """Run a reconciler sweep now, inline, instead of waiting for beat.

    Admin-only and synchronous: it re-submits OCR work and therefore spends
    money, so it is not something a staff account triggers by refreshing a page.
    """
    from app.tasks.reconciler import reconcile_once

    try:
        return reconcile_once(limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Reconciler failed: {exc}")


@app.post("/ingest/ocr-state/{row_id}/retry")
def retry_ocr_row(row_id: str, _user: dict = Depends(require_admin)) -> dict:
    """Put one abandoned row back in the queue. The next sweep picks it up."""
    from app.db.ingestion_state import IngestionStateStore

    store = IngestionStateStore()
    if not store.reset_for_retry(row_id):
        raise HTTPException(
            status_code=404,
            detail="No abandoned ingestion row with that id (only abandoned rows can be retried).",
        )
    return {"row_id": row_id, "status": "received"}


@app.get("/candidates/{candidate_id}/identity")
def candidate_identity_documents(candidate_id: str, user: dict = Depends(current_user)) -> dict:
    """The Aadhaar and passport read out of this candidate's application.

    Numbers are masked unless the caller is an administrator. A recruiter needs
    to know the document is on file and reads correctly; the full Aadhaar number
    is a different question with a different answer.
    """
    from app.db.identity_records import find_for_candidate

    # 404s a record that belongs to another staff member, exactly as the
    # candidate endpoints do — an identity document must not be the thing that
    # confirms a candidate id exists.
    _owned_or_404(candidate_id, user)

    try:
        found = find_for_candidate(candidate_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Identity records unavailable: {exc}")

    is_admin = user.get("role") == ADMIN_ROLE

    def _clean(doc: dict) -> dict:
        doc = dict(doc)
        # The raw OCR payload carries the unmasked number in a dozen places.
        doc.pop("raw", None)
        if not is_admin:
            doc.pop("aadhaar_number", None)
            doc.pop("vid", None)
            doc.pop("raw_mrz", None)
        return doc

    return {
        "candidate_id": candidate_id,
        "aadhaar": [_clean(d) for d in found["aadhaar"]],
        "passport": [_clean(d) for d in found["passport"]],
    }


@app.get("/ingest/workers")
def ingest_workers(_user: dict = Depends(current_user)) -> dict:
    """Lets the frontend pick the async path only when it will actually work."""
    from app.tasks.health import workers_online

    return {"available": workers_online()}


@app.post("/ingest/poll/async")
def trigger_poll_async(query: str | None = None, _user: dict = Depends(current_user)) -> dict:
    """Queue a poll cycle on a worker and return immediately with its task id."""
    from app.tasks.health import reset_cache, workers_online

    if not workers_online():
        raise HTTPException(
            status_code=503,
            detail="No ingestion worker is running. Start one with: "
                   "celery -A app.tasks.celery_app worker --loglevel=INFO --concurrency=4",
        )

    from app.tasks.jobs import run_poll_cycle

    try:
        async_result = run_poll_cycle.delay(query)
    except Exception as exc:  # noqa: BLE001
        # The ping passed but the enqueue did not — the broker died in between,
        # so the memoised "yes" is now wrong. Drop it rather than serve it for
        # the rest of its TTL.
        reset_cache()
        raise HTTPException(status_code=503, detail=f"Could not queue the poll: {exc}")

    return {"task_id": async_result.id, "state": "PENDING"}


@app.get("/ingest/tasks/{task_id}")
def ingest_task_status(task_id: str, _user: dict = Depends(current_user)) -> dict:
    """Poll a queued cycle. `result` is the batch summary once state is SUCCESS."""
    try:
        from app.tasks.celery_app import celery_app

        async_result = celery_app.AsyncResult(task_id)
        state = async_result.state
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Result backend unavailable: {exc}")

    payload: dict = {"task_id": task_id, "state": state, "ready": state in ("SUCCESS", "FAILURE")}

    if state == "SUCCESS":
        payload["result"] = async_result.result
    elif state == "FAILURE":
        err_obj = async_result.result
        err_str = str(err_obj) if err_obj is not None else "Worker task execution failed"
        if isinstance(err_obj, KeyError) or "NotRegistered" in type(err_obj).__name__ or (err_str.startswith("'") and err_str.endswith("'")):
            err_str = f"Task {err_str} is not registered on Celery worker. Restart worker with: celery -A app.tasks.celery_app worker --loglevel=INFO --concurrency=4"
        payload["error"] = err_str

    return payload


@app.put("/candidates/{candidate_id}")
def update_candidate_profile(candidate_id: str, profile: CandidateProfile, _user: dict = Depends(current_user)) -> dict:
    """Update a candidate's structured profile (e.g. to correct fields during verification)."""
    repository = repo()
    record = repository.get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    repository.update_profile(candidate_id, profile)
    updated_record = repository.get(candidate_id)
    return updated_record.model_dump(mode="json")


@app.post("/candidates/{candidate_id}/verify")
def verify_candidate(candidate_id: str, _user: dict = Depends(current_user)) -> dict:
    """Verify a candidate's profile, marking their status as 'verified'."""
    repository = repo()
    record = repository.get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    repository.update_status(candidate_id, "verified")
    updated_record = repository.get(candidate_id)
    return updated_record.model_dump(mode="json")


# ---- Sourcing Clients DB Endpoints ---------------------------------------- #
@app.get("/sourcing-clients")
def list_sourcing_clients(_user: dict = Depends(current_user)) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["sourcing_clients"]
    items = list(coll.find({}, {"_id": 0}))
    return {"items": items}


@app.post("/sourcing-clients")
def create_sourcing_client(client_data: dict, _user: dict = Depends(current_user)) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["sourcing_clients"]
    client_id = client_data.get("id")
    if client_id:
        coll.replace_one({"id": client_id}, client_data, upsert=True)
    else:
        coll.insert_one(client_data)
    return {"status": "ok", "record": client_data}


@app.delete("/sourcing-clients/{client_id}")
def delete_sourcing_client(client_id: str, _user: dict = Depends(current_user)) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["sourcing_clients"]
    coll.delete_one({"id": client_id})
    return {"status": "deleted", "id": client_id}


# ---- Job Orders DB Endpoints --------------------------------------------- #
@app.get("/job-orders")
def list_job_orders(_user: dict = Depends(current_user)) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["job_orders"]
    items = list(coll.find({}, {"_id": 0}))
    return {"items": items}


@app.post("/job-orders")
def create_job_order(order_data: dict, _user: dict = Depends(current_user)) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["job_orders"]
    order_id = order_data.get("id")
    if order_id:
        coll.replace_one({"id": order_id}, order_data, upsert=True)
    else:
        coll.insert_one(order_data)
    return {"status": "ok", "record": order_data}


@app.put("/job-orders/{order_id}")
def update_job_order(order_id: str, order_data: dict, _user: dict = Depends(current_user)) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["job_orders"]
    coll.replace_one({"id": order_id}, order_data, upsert=True)
    return {"status": "updated", "record": order_data}


@app.delete("/job-orders/{order_id}")
def delete_job_order(order_id: str, _user: dict = Depends(current_user)) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["job_orders"]
    coll.delete_one({"id": order_id})
    return {"status": "deleted", "id": order_id}


# --------------------------------------------------------------------------- #
#  Staff Administration
# --------------------------------------------------------------------------- #
class CreateStaffRequest(BaseModel):
    email: str
    password: str
    name: str | None = None
    keywords: list[str] = Field(default_factory=list)


class UpdateStaffRequest(BaseModel):
    name: str | None = None
    keywords: list[str] | None = None
    active: bool | None = None
    password: str | None = None


@app.get("/staff")
def list_staff(
    include_inactive: bool = Query(True),
    _admin: dict = Depends(require_admin),
) -> dict:
    staff_items = users.list_staff(include_inactive=include_inactive)
    return {"count": len(staff_items), "items": [u.to_public() for u in staff_items]}


@app.get("/staff/workload")
def staff_workload(_admin: dict = Depends(require_admin)) -> dict:
    """The workload matrix: one row per active staff member, plus the totals.

    `orphaned` is counted against the *whole* roster, deactivated accounts
    included — a deactivated staff member still owns their work, so only
    profiles pointing at an account that is gone are unreachable, and nothing
    but a rebalance brings those back.
    """
    repository = repo()
    everyone = users.list_staff(include_inactive=True)
    active = [member for member in everyone if member.active]

    # Give an owner to anything ingested while the roster was empty, without
    # waiting to be asked. Deliberately `allocate_unassigned` and not
    # `rebalance_all`: reading this screen must not move work that already
    # belongs to someone, and a full re-level on every page load did exactly
    # that — a staff member's queue could change while they were working it,
    # because an admin opened a dashboard.
    if active and repository.unassigned_count() > 0:
        try:
            allocate_unassigned(repo=repository, users=users)
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto-allocation on staff workload fetch failed: %s", exc)

    items = repository.staff_workload(active)
    roster_ids = [member.id for member in everyone]
    return {
        "items": items,
        # The whole roster, deactivated accounts included. `items` carries only
        # the active ones, so without this the console cannot tell a profile
        # owned by a deactivated colleague from one owned by a deleted account —
        # and would flag the first as orphaned, which it is not.
        "roster_ids": roster_ids,
        "totals": {
            "staff": len(items),
            "assigned": sum(row["assigned"] for row in items),
            "evaluated": sum(row["evaluated"] for row in items),
            "unassigned": repository.unassigned_count(),
            "orphaned": repository.orphaned_count(roster_ids),
        },
    }


@app.post("/staff")
def create_staff(
    payload: CreateStaffRequest, _admin: dict = Depends(require_admin)
) -> dict:
    """Add a staff account to the roster. Nothing is reallocated.

    Creating an account used to re-level the whole collection on the spot. It no
    longer does: adding a colleague is a roster change, and an admin doing it at
    9am should not thereby reshuffle queues that people are part-way through.
    The new account starts empty and receives its share through the normal
    least-loaded rule as résumés arrive; moving the existing pile is the
    Rebalance control, which is explicit and says what it did.
    """
    try:
        user = users.create_staff(
            email=payload.email,
            password=payload.password,
            name=payload.name,
            keywords=payload.keywords,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log.info(
        "Created staff account %s (%s); existing allocations left untouched",
        user.name, user.email,
    )
    return {"staff": user.to_public()}


@app.patch("/staff/{staff_id}")
def update_staff(
    staff_id: str, payload: UpdateStaffRequest, _admin: dict = Depends(require_admin)
) -> dict:
    try:
        user = users.update_staff(
            staff_id,
            name=payload.name,
            keywords=payload.keywords,
            active=payload.active,
            password=payload.password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not user:
        raise HTTPException(status_code=404, detail="Staff account not found")

    # Deactivating no longer re-levels the collection. A deactivated account can
    # still sign in and still owns its queue — "receives nothing new" is the
    # whole of what deactivation means — so pulling their unread profiles out
    # from under them contradicted both the console's own wording and the rule
    # that rebalancing is an explicit act.
    return {"staff": user.to_public()}


@app.delete("/staff/{staff_id}")
def delete_staff(
    staff_id: str,
    rebalance: bool = Query(True),
    _admin: dict = Depends(require_admin),
) -> dict:
    """Remove a staff account and deal with the queue it leaves behind.

    Splits that queue rather than re-levelling the whole collection: unviewed
    profiles go straight to the least-loaded remaining staff, while anything
    already read or judged is left orphaned so its evaluation survives. The
    admin console reports the orphans and re-homes them on request.

    `rebalance=false` skips the redistribution entirely, leaving the whole queue
    orphaned — for an admin who wants to place it by hand.
    """
    deleted = users.delete_staff(staff_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Staff account not found")

    outcome = (
        redistribute_from_staff(staff_id, repo=repo(), users=users)
        if rebalance
        else {"reallocated": 0, "orphaned": repo().count({"assigned_staff_id": staff_id})}
    )
    return {
        "status": "deleted",
        "id": staff_id,
        "reallocated": outcome.get("reallocated", 0),
        "orphaned": outcome.get("orphaned", 0),
    }


# --------------------------------------------------------------------------- #
#  Allocation
# --------------------------------------------------------------------------- #
class AssignRequest(BaseModel):
    staff_id: str


@app.post("/candidates/{candidate_id}/assign")
def assign_candidate_route(
    candidate_id: str, payload: AssignRequest, _admin: dict = Depends(require_admin)
) -> dict:
    member = users.get(payload.staff_id)
    if not member or member.role != STAFF_ROLE or not member.active:
        raise HTTPException(status_code=400, detail="Target staff member is not active")

    repository = repo()
    record = repository.get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")

    repository.assign(candidate_id, member.id, member.name)
    notify_candidate_assigned(
        member.id,
        {
            "id": candidate_id,
            "full_name": record.profile.full_name if record else None,
            "email": record.profile.email if record else None,
        },
        staff_name=member.name,
    )
    return {
        "status": "assigned",
        "candidate_id": candidate_id,
        "assigned_staff_id": member.id,
        "assigned_staff_name": member.name,
    }


@app.post("/candidates/{candidate_id}/auto-assign")
def auto_assign_candidate(candidate_id: str, _admin: dict = Depends(require_admin)) -> dict:
    repository = repo()
    record = repository.get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")

    result = assign_candidate(candidate_id, record.profile, repo=repository)
    if not result.assigned:
        raise HTTPException(status_code=409, detail="No active staff member to assign to")

    notify_candidate_assigned(
        result.staff_id,
        {"id": candidate_id, "full_name": record.profile.full_name, "email": record.profile.email},
        staff_name=result.staff_name,
    )
    return result.to_public()


@app.post("/candidates/rebalance")
def rebalance_candidates(_admin: dict = Depends(require_admin)) -> dict:
    """Level untouched profiles across the roster. Reviewed work stays put."""
    result = rebalance_all()
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("detail"))
    return result


@app.post("/candidates/rehome-orphans")
def rehome_orphaned_candidates(_admin: dict = Depends(require_admin)) -> dict:
    """Re-home profiles stranded on a deleted account, verdicts intact.

    Separate from `/candidates/rebalance` because it does the opposite thing to
    the same rows: a rebalance refuses to move reviewed profiles, and every
    orphan is reviewed — that is why it was orphaned instead of reallocated when
    the account was deleted. Only this endpoint can clear them.
    """
    result = rehome_orphans(repo=repo(), users=users)
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("detail"))
    return result


# --------------------------------------------------------------------------- #
#  Evaluation (the staff workspace)
# --------------------------------------------------------------------------- #
class EvaluationRequest(BaseModel):
    status: str = Field(description="One of " + ", ".join(EVALUATION_STATUSES))
    score: int | None = Field(default=None, ge=1, le=5, description="Star rating, 1-5.")
    notes: str | None = None


@app.post("/candidates/{candidate_id}/view")
def mark_candidate_viewed(candidate_id: str, user: dict = Depends(current_user)) -> dict:
    _owned_or_404(candidate_id, user)
    stamped = repo().mark_viewed(candidate_id, staff_id=_staff_scope(user))
    return {"status": "ok", "candidate_id": candidate_id, "first_view": stamped}


@app.post("/candidates/{candidate_id}/evaluate")
def evaluate_candidate(
    candidate_id: str, payload: EvaluationRequest, user: dict = Depends(current_user)
) -> dict:
    if payload.status not in EVALUATION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown status '{payload.status}'. Expected one of {', '.join(EVALUATION_STATUSES)}.",
        )
    _owned_or_404(candidate_id, user)

    record = repo().save_evaluation(
        candidate_id,
        staff_id=_staff_scope(user),
        status=payload.status,
        score=payload.score,
        notes=payload.notes,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return record.model_dump(mode="json")


# --------------------------------------------------------------------------- #
#  Notifications
# --------------------------------------------------------------------------- #
class MarkReadRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    all: bool = False


@app.get("/notifications")
def list_notifications(
    limit: int = Query(30, ge=1, le=100),
    unread_only: bool = Query(False),
    user: dict = Depends(current_user),
) -> dict:
    notifications = NotificationRepository()
    return {
        "items": notifications.list_for(user["id"], limit=limit, unread_only=unread_only),
        "unread": notifications.unread_count(user["id"]),
    }


@app.post("/notifications/read")
def mark_notifications_read(
    payload: MarkReadRequest, user: dict = Depends(current_user)
) -> dict:
    notifications = NotificationRepository()
    if payload.all:
        updated = notifications.mark_all_read(user["id"])
    else:
        updated = notifications.mark_read(user["id"], payload.ids)
    return {"updated": updated, "unread": notifications.unread_count(user["id"])}


# --------------------------------------------------------------------------- #
#  SLA
# --------------------------------------------------------------------------- #
@app.get("/sla/alerts")
def list_sla_alerts(
    status: str = Query("active", pattern="^(active|resolved|all)$"),
    limit: int = Query(100, ge=1, le=500),
    _admin: dict = Depends(require_admin),
) -> dict:
    items = sla_checker.list_alerts(status=None if status == "all" else status, limit=limit)
    return {"count": len(items), "items": items, "threshold_hours": settings.sla_threshold_hours}


@app.get("/sla/breaches")
def current_sla_breaches(_admin: dict = Depends(require_admin)) -> dict:
    items = sla_checker.find_breaches()
    return {"count": len(items), "items": items, "threshold_hours": settings.sla_threshold_hours}


@app.post("/sla/scan")
def run_sla_scan(_admin: dict = Depends(require_admin)) -> dict:
    return sla_checker.scan()


# ---- Background ingestion ------------------------------------------------- #
@app.get("/ingest/workers")
def ingest_workers(_user: dict = Depends(current_user)) -> dict:
    from app.tasks.health import workers_online
    return {"available": workers_online()}


@app.post("/ingest/poll/async")
def trigger_poll_async(query: str | None = None, _user: dict = Depends(current_user)) -> dict:
    from app.tasks.health import reset_cache, workers_online

    if not workers_online():
        raise HTTPException(
            status_code=503,
            detail="No ingestion worker is running. Start one with: "
                   "celery -A app.tasks.celery_app worker --loglevel=INFO --concurrency=4",
        )

    from app.tasks.jobs import run_poll_cycle

    try:
        async_result = run_poll_cycle.delay(query)
    except Exception as exc:
        reset_cache()
        raise HTTPException(status_code=503, detail=f"Could not queue the poll: {exc}")

    return {"task_id": async_result.id, "state": "PENDING"}


@app.get("/ingest/tasks/{task_id}")
def poll_task_status(task_id: str, _user: dict = Depends(current_user)) -> dict:
    from app.tasks.celery_app import celery_app

    result = celery_app.AsyncResult(task_id)
    payload = {"task_id": task_id, "state": result.state, "ready": result.ready()}
    if result.state == "SUCCESS":
        payload["result"] = result.result
    elif result.state == "FAILURE":
        payload["error"] = str(result.result)
    return payload


# Serve the static files from the Next.js export.
# This must be mounted AFTER all other routes so it acts as a fallback.
frontend_out_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "out")
)
if os.path.exists(frontend_out_dir):
    app.mount("/", StaticFiles(directory=frontend_out_dir, html=True), name="frontend")


