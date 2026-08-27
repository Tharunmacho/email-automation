"""FastAPI application — REST seam for the future recruiter dashboard / frontend.

Deliberately minimal for now: health, candidate list/detail, resume download, and
a manual poll trigger. Search, filtering, ranking, JD-matching, scoring, and auth
all slot in here later without touching the ingestion pipeline.

Run:
    uvicorn app.api.routes:app --reload --port 8000
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import threading
import uuid
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.core.models import EVALUATION_STATUSES, CandidateProfile
from app.core.security import create_token, read_token, verify_service_key
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
from app.policy.cv_policy import (
    JOB_CATEGORIES,
    is_cv_required,
    known_job_ids,
    policy_version,
)
from app.services.candidate_intake import IntakeError, intake_whatsapp_candidate
from app.services.identity_intake import file_documents as file_identity_documents
from app.services.resume_store import ResumeRejected, store_resume
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

from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError
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


def _attachment_response(data: bytes, mime_type: str | None, filename: str) -> Response:
    """A download, named so every browser gets the name right.

    Both header forms, because they fail in opposite directions: the plain one
    mangles anything non-ASCII, and the `filename*` one is ignored by enough
    old clients to matter. Quotes are stripped rather than escaped — a quote in
    a filename is worth nothing and a quote that terminates the header early is
    worth a broken download.
    """
    import urllib.parse

    safe = (filename or "download").replace('"', "").replace("'", "")
    # `UTF-8''<pct-encoded>` — the two quotes are the empty language tag RFC
    # 5987 requires between the charset and the name, not a stray pair.
    extended = f"UTF-8''{urllib.parse.quote(safe)}"
    return Response(
        content=data,
        media_type=mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"; filename*={extended}'
        },
    )


@app.get("/candidates/{candidate_id}/resume")
def download_resume(candidate_id: str, user: dict = Depends(current_user)) -> Response:
    record = _owned_or_404(candidate_id, user)
    if not record.resume or not record.resume.storage_key:
        raise HTTPException(status_code=404, detail="Candidate resume attachment not found")
    
    backend_name = record.resume.storage_backend or settings.storage_backend
    try:
        data = get_storage_backend(backend_name).load(record.resume.storage_key)
    except Exception as e1:
        # Fallback check: if record backend failed, try alternate storage backend (local vs gridfs)
        try:
            alt_backend = "local" if backend_name == "gridfs" else "gridfs"
            data = get_storage_backend(alt_backend).load(record.resume.storage_key)
        except Exception as e2:
            import logging
            logging.error(f"Failed to load resume. e1={e1}, e2={e2}")
            filename = record.resume.original_filename or "resume.pdf"
            raise HTTPException(
                status_code=404,
                detail=f"Resume file '{filename}' is missing from server storage."
            )
    
    return _attachment_response(
        data,
        record.resume.mime_type,
        record.resume.original_filename or "resume.pdf",
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
    from app.services import identity_files

    # 404s a record that belongs to another staff member, exactly as the
    # candidate endpoints do — an identity document must not be the thing that
    # confirms a candidate id exists.
    record = _owned_or_404(candidate_id, user)

    try:
        found = find_for_candidate(candidate_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Identity records unavailable: {exc}")

    is_admin = user.get("role") == ADMIN_ROLE

    def _may_download(doc: dict) -> bool:
        """An Aadhaar scan is the full Aadhaar number, in a picture.

        The number itself is masked for everyone but an administrator in
        `_clean` below, and serving the card would hand back exactly what the
        masking withholds. A passport is a different judgement: its number is
        on the candidate record already, and overseas placement is decided on
        whether it is in date.
        """
        return is_admin or doc.get("document_type") != "aadhaar"

    def _clean(doc: dict) -> dict:
        doc = dict(doc)
        # The raw OCR payload carries the unmasked number in a dozen places.
        doc.pop("raw", None)
        if not is_admin:
            doc.pop("aadhaar_number", None)
            doc.pop("vid", None)
            doc.pop("raw_mrz", None)

        # Whether there is a scan to download, answered here rather than
        # guessed at in the browser. The screen offering a button that can only
        # 404 is the same mistake the résumé button already avoids, and the
        # facts it turns on — a `file` block, a bundle to re-cut, the caller's
        # role — are all on this side of the wire.
        doc["file_available"] = _may_download(doc) and identity_files.available(record, doc)
        # A storage key is an implementation detail and a thing to probe. The
        # name, type and size are what a recruiter is shown before clicking.
        block = doc.get("file")
        if isinstance(block, dict):
            doc["file"] = {
                key: block.get(key)
                for key in ("filename", "mime_type", "size", "sha256")
                if block.get(key) is not None
            }
        return doc

    return {
        "candidate_id": candidate_id,
        "aadhaar": [_clean(d) for d in found["aadhaar"]],
        "passport": [_clean(d) for d in found["passport"]],
    }


@app.get("/candidates/{candidate_id}/identity/{document_type}/{record_id}/file")
def download_identity_document(
    candidate_id: str,
    document_type: str,
    record_id: str,
    user: dict = Depends(current_user),
) -> Response:
    """The Aadhaar or passport scan itself, as a download.

    The row above it says what an extractor read. This is the page it read it
    off, which is what a documentation officer checking a digit actually needs
    — and, for the email pipeline, it is cut out of the stored bundle on the
    way past rather than kept as a second copy. See `app/services/identity_files`.

    An Aadhaar is administrators only, for the same reason the number is masked
    for everybody else: the card is the number.
    """
    from app.db.identity_records import DOCUMENT_TYPES, find_one
    from app.services import identity_files

    if document_type not in DOCUMENT_TYPES:
        raise HTTPException(
            status_code=404,
            detail=f"'{document_type}' is not an identity document type",
        )

    record = _owned_or_404(candidate_id, user)

    if document_type == "aadhaar" and user.get("role") != ADMIN_ROLE:
        raise HTTPException(
            status_code=403,
            detail=(
                "An Aadhaar scan carries the full Aadhaar number, which is "
                "masked for anyone who is not an administrator. Ask an "
                "administrator for the card."
            ),
        )

    try:
        doc = find_one(candidate_id, document_type, record_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Identity records unavailable: {exc}")
    if not doc:
        raise HTTPException(
            status_code=404, detail=f"No {document_type} record with that id for this candidate"
        )

    try:
        found = identity_files.load(record, doc)
    except identity_files.IdentityFileMissing as exc:
        raise HTTPException(
            status_code=404, detail=f"The {document_type} scan could not be served — {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Identity file for candidate %s (%s %s) could not be built: %s",
            candidate_id, document_type, record_id, exc,
        )
        raise HTTPException(
            status_code=500, detail=f"The {document_type} scan could not be built: {exc}"
        )

    return _attachment_response(found.data, found.mime_type, found.filename)


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
#  B2B Enquiries — the recruiter's side
#
#  A manpower requirement an agent raised over WhatsApp, and what the agency
#  decided to do about it. The bot's own way in is POST /b2b-enquiries down in
#  the integration section; these are the endpoints the screen uses, and they
#  take a staff session.
#
#  Admin-only, unlike the sourcing and job-order endpoints beside them. An
#  enquiry carries a company's contact details and its hiring plans before the
#  agency has agreed to anything, and a staff account exists to review the
#  candidates allocated to it — there is no version of that job that needs this.
# --------------------------------------------------------------------------- #


def _enquiry_json(doc: dict) -> dict:
    """One enquiry, with its timestamps as ISO strings.

    Mongo hands back `datetime`, the frontend sorts and formats strings, and
    leaving the conversion to whichever encoder happens to run means the same
    field arrives in two shapes depending on the route. Done once, here.
    """
    from datetime import datetime as _dt

    out = {k: v for k, v in doc.items() if k != "_id"}
    for field in ("received_at", "updated_at", "handled_at"):
        value = out.get(field)
        if isinstance(value, _dt):
            out[field] = value.isoformat()
    return out


class EnquiryPatch(BaseModel):
    """The edits the screen may make. Absent means "leave it alone".

    Every field is optional and `None` is not a value — `update_enquiry` skips
    it. A PATCH that sends only `{"status": "reviewing"}` therefore cannot blank
    the requirement text by omitting it, which is the failure a partial update
    written against a full model produces on its first use.
    """

    model_config = ConfigDict(extra="ignore")

    status: str | None = None
    party_type: str | None = None
    company_name: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    country: str | None = None
    city: str | None = None
    requirement: str | None = None
    job_title: str | None = None
    job_id: str | None = None
    headcount: int | None = None
    destination_country: str | None = None
    salary_budget: str | None = None
    experience_required: str | None = None
    skills: list[str] | None = None
    needed_by: str | None = None
    notes: str | None = None


class EnquiryIn(EnquiryPatch):
    """An enquiry an admin typed in themselves.

    Same fields as the patch, with the one the screen cannot render a row
    without made mandatory. Sourced as `manual` so a phone call logged by hand
    is never mistaken for something the bot heard.
    """

    contact_name: str = Field(min_length=1, max_length=200)


class ConvertEnquiryIn(BaseModel):
    """What the recruiter filled in on the job order before raising it.

    The enquiry supplies the defaults and the recruiter supplies the judgement:
    a title the client will recognise, a real due date, and a headcount they are
    willing to commit to. Sent back here rather than derived, because "40
    welders, before Eid" is not a requisition until somebody has decided what it
    means.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    client: str = Field(min_length=1, max_length=200)
    headcount: int = Field(default=1, ge=1)
    salary: str = ""
    skills: list[str] = Field(default_factory=list)
    description: str = ""
    due_date: str = ""
    industry: str = ""
    designation: str = ""


@app.get("/b2b-enquiries")
def list_b2b_enquiries(
    status: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    _user: dict = Depends(require_admin),
) -> dict:
    """Every enquiry, newest first, with the per-state counts beside them.

    The counts are computed over the whole collection rather than over the page
    that came back, so the tab that reads "Converted 12" still reads 12 while
    the list is filtered to the four that are new.
    """
    from app.db.b2b_enquiries import STATUSES, list_enquiries, status_counts

    items = [_enquiry_json(doc) for doc in list_enquiries(status=status, limit=limit)]
    return {"items": items, "counts": status_counts(), "statuses": list(STATUSES)}


@app.post("/b2b-enquiries/manual", status_code=201)
def create_manual_b2b_enquiry(payload: EnquiryIn, user: dict = Depends(require_admin)) -> dict:
    """Log an enquiry that arrived some other way — a phone call, an email.

    A separate path from the bot's POST /b2b-enquiries rather than a flag on
    it: that one authenticates a system and this one authenticates a person, and
    the credential is what decides which. Collapsing them would mean a route
    that accepts either, which is a route that accepts the service key for a
    recruiter's action.
    """
    from app.db.b2b_enquiries import record_enquiry

    doc, _created = record_enquiry(payload.model_dump(exclude_none=True), source="manual")
    log.info("B2B enquiry %s logged by %s", doc.get("id"), user.get("email"))
    return {"status": "ok", "enquiry": _enquiry_json(doc)}


@app.get("/b2b-enquiries/{enquiry_id}")
def get_b2b_enquiry(enquiry_id: str, _user: dict = Depends(require_admin)) -> dict:
    from app.db.b2b_enquiries import get_enquiry

    doc = get_enquiry(enquiry_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    return _enquiry_json(doc)


@app.patch("/b2b-enquiries/{enquiry_id}")
def update_b2b_enquiry(
    enquiry_id: str, payload: EnquiryPatch, user: dict = Depends(require_admin)
) -> dict:
    """Edit an enquiry, or move it along.

    `converted` is refused here — it means a job order exists, and the only way
    to make that true is to convert the enquiry, which writes the order's id at
    the same time. A status that claims an order nothing points at is a dead end
    on the screen and a job somebody raises twice.
    """
    from app.db.b2b_enquiries import update_enquiry

    changes = payload.model_dump(exclude_none=True)
    # Who moved it. Recorded from the session rather than accepted from the
    # body: an audit field a caller can set is not one.
    if changes.get("status") in ("reviewing", "closed"):
        changes.setdefault("handled_by", user.get("email") or "")

    try:
        doc = update_enquiry(enquiry_id, changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not doc:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    return {"status": "ok", "enquiry": _enquiry_json(doc)}


@app.post("/b2b-enquiries/{enquiry_id}/convert", status_code=201)
def convert_b2b_enquiry(
    enquiry_id: str, payload: ConvertEnquiryIn, user: dict = Depends(require_admin)
) -> dict:
    """Turn an enquiry into a job order the agency has committed to.

    The order is written first and the enquiry is stamped second. That order
    matters: an order that exists while the enquiry still reads `new` is a
    visible inconsistency a recruiter can resolve by looking at the Job Orders
    screen, whereas an enquiry marked `converted` pointing at an order that was
    never written is a dead end with nothing behind it.

    Converting twice is refused. The second call would raise a second
    requisition for one vacancy, and the recruiter who made it would have no way
    of knowing — both orders look real.
    """
    from app.db.b2b_enquiries import get_enquiry, mark_converted
    from app.db.mongo import get_db

    enquiry = get_enquiry(enquiry_id)
    if not enquiry:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    if enquiry.get("converted_job_order_id"):
        raise HTTPException(
            status_code=409,
            detail=(
                "This enquiry was already converted into job order "
                f"{enquiry['converted_job_order_id']}."
            ),
        )

    order_id = f"JO-{uuid.uuid4().hex[:8].upper()}"
    order = {
        "id": order_id,
        "title": payload.title.strip(),
        "client": payload.client.strip(),
        "headcount": payload.headcount,
        "salary": payload.salary.strip(),
        "skills": [s.strip() for s in payload.skills if s.strip()],
        "description": payload.description.strip(),
        "dueDate": payload.due_date.strip(),
        "status": "OPEN",
        "industry": payload.industry.strip(),
        "designation": payload.designation.strip(),
        "fulfilledCount": 0,
        "shortlistedCandidateIds": [],
        "rejectedCandidateIds": [],
        # Where this requisition came from, so the Job Orders screen can point
        # back at the conversation that produced it.
        "sourceEnquiryId": enquiry_id,
    }
    get_db()["job_orders"].insert_one(dict(order))
    order.pop("_id", None)

    updated = mark_converted(enquiry_id, order_id, handled_by=user.get("email") or "")
    log.info(
        "B2B enquiry %s converted into job order %s by %s",
        enquiry_id,
        order_id,
        user.get("email"),
    )

    return {
        "status": "ok",
        "job_order": order,
        "enquiry": _enquiry_json(updated) if updated else None,
    }


@app.delete("/b2b-enquiries/{enquiry_id}")
def delete_b2b_enquiry(enquiry_id: str, user: dict = Depends(require_admin)) -> dict:
    """Remove an enquiry outright — a duplicate, or a wrong number.

    Deletion, not closure. `closed` is the answer for an enquiry that was real
    and came to nothing, and it is the one a recruiter almost always wants: it
    keeps the record of what was asked for. This is for the enquiries that
    should never have been filed.
    """
    from app.db.b2b_enquiries import delete_enquiry

    if not delete_enquiry(enquiry_id):
        raise HTTPException(status_code=404, detail="Enquiry not found")
    log.info("B2B enquiry %s deleted by %s", enquiry_id, user.get("email"))
    return {"status": "deleted", "id": enquiry_id}


# --------------------------------------------------------------------------- #
#  Staff Administration
# --------------------------------------------------------------------------- #
class CreateStaffRequest(BaseModel):
    email: str
    password: str
    name: str | None = None
    keywords: list[str] = Field(default_factory=list)
    # Capped rather than pattern-matched: a country code, spaces, an extension
    # and a second number for the same person all have to fit, and the console
    # only ever displays this or dials it.
    phone: str = Field(default="", max_length=40)


class UpdateStaffRequest(BaseModel):
    name: str | None = None
    keywords: list[str] | None = None
    active: bool | None = None
    password: str | None = None
    phone: str | None = Field(default=None, max_length=40)


@app.get("/staff")
def list_staff(
    include_inactive: bool = Query(True),
    _admin: dict = Depends(require_admin),
) -> dict:
    staff_items = users.list_staff(include_inactive=include_inactive)
    return {"count": len(staff_items), "items": [u.to_public() for u in staff_items]}


@app.get("/staff/workload")
def staff_workload(_admin: dict = Depends(require_admin)) -> dict:
    """The workload matrix: one row per staff member, plus the totals.

    Every account, deactivated ones included. It used to be the active roster
    only, which meant a deactivated colleague's queue was in no row and in no
    total — the console could neither show that work nor offer to reactivate
    the account holding it, and the candidate pool silently under-reported by
    however much they were carrying. `active` is on each row, so the console
    still knows which accounts new profiles may be routed to.

    `orphaned` is counted against the same whole roster: a deactivated staff
    member still owns their work, so only profiles pointing at an account that
    is gone are unreachable, and nothing but a rebalance brings those back.
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

    items = repository.staff_workload(everyone)
    roster_ids = [member.id for member in everyone]
    return {
        "items": items,
        # The whole roster, deactivated accounts included. `items` carries only
        # the active ones, so without this the console cannot tell a profile
        # owned by a deactivated colleague from one owned by a deleted account —
        # and would flag the first as orphaned, which it is not.
        "roster_ids": roster_ids,
        "totals": {
            # The accounts work is routed to, not the number of rows: `items`
            # now carries deactivated accounts as well.
            "staff": len(active),
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
            phone=payload.phone,
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
            phone=payload.phone,
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

    # Assigning somebody the candidate they already hold is not an assignment.
    # Two things follow from letting it through, and both are destructive:
    # `assign` clears `viewed_at` and the verdict, so a double-click on the
    # button throws away the evaluation that person just wrote; and the
    # notification goes out a second time for work they were told about the
    # first time. Ownership did not change, so nothing happens.
    if record.assigned_staff_id == member.id:
        return {
            "status": "unchanged",
            "candidate_id": candidate_id,
            "assigned_staff_id": member.id,
            "assigned_staff_name": member.name,
        }

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


# --------------------------------------------------------------------------- #
#  WhatsApp bot integration
#
#  The recruitment bot's entire surface on this system: ask what the CV policy
#  says, submit a finished registration, hand over the résumé, and — when the
#  person on the other end is not a candidate at all — file the manpower
#  requirement they came to raise. Authenticated with a service key rather than
#  a staff session, and none of them reachable with a recruiter's token.
#
#  The bot talks to two kinds of people and this section reflects that. A
#  candidate answers questions about themselves and becomes a row in
#  `candidates`. An agent describes a vacancy and becomes a row in
#  `b2b_enquiries` — a different collection, because filing a company as a
#  candidate would put it in a recruiter's review queue and allocate it to a
#  staff member as if it were a person.
#
#  What is deliberately absent is as important as what is here. There is no
#  endpoint to assign a candidate, evaluate one, or change a hiring decision:
#  those belong to the CRM and the bot has no business in them. Nor can the bot
#  raise a job order — it files what an agent *said*, and turning that into a
#  commitment the agency has made is a decision a recruiter takes on the B2B
#  Enquiries screen. And there is no way for the bot to write to MongoDB
#  directly — every one of these goes through the same repository, balancer and
#  db modules the mailbox pipeline uses, so the business logic runs on the way
#  in rather than being re-implemented on the other side of the wire.
# --------------------------------------------------------------------------- #


def require_service_key(x_service_key: str | None = Header(default=None)) -> None:
    """Authenticate another *system*, not a person.

    Runs as a dependency so it resolves before the body is validated: a request
    with no credential is refused for having no credential, and never gets a
    422 describing the shape of an endpoint it is not allowed to call.

    An unset `WHATSAPP_SERVICE_KEY` refuses everything — see `verify_service_key`.
    A deployment that forgot to configure it serves nothing rather than serving
    an open write endpoint.
    """
    if not verify_service_key(x_service_key, settings.whatsapp_service_key):
        raise HTTPException(status_code=401, detail="Invalid or missing service key")


class WhatsAppResumeIn(BaseModel):
    """A résumé travelling with the submission that needs it.

    Base64 rather than a path, because the bot's disk is not this machine's: a
    `storage_key` from over there names a file nothing here can open, and a
    recruiter clicking "download résumé" would get a 404 for a document that
    exists. The bytes cross the wire and are written through the CRM's own
    storage backend.

    Inline rather than a second call, for the one case that has no other exit: a
    candidate the policy requires a CV for cannot be created without one, and
    `POST /candidates/{id}/resume` needs an id that does not exist yet. A résumé
    that is merely offered can still arrive either way.
    """

    filename: str = "resume.pdf"
    mime_type: str = "application/pdf"
    content_base64: str


class JobAnswerIn(BaseModel):
    """One screening answer as the bot submits it.

    The question text travels with the answer instead of being resolved from
    `job_questions` at read time — an admin rewording a question must not
    rewrite what a candidate was asked six weeks ago.
    """

    model_config = ConfigDict(extra="ignore")

    question_id: str | None = None
    question: str | None = Field(default=None, max_length=300)
    answer: str | None = Field(default=None, max_length=1000)
    kind: str | None = None
    asked_at: str | None = None


class WhatsAppProfileIn(BaseModel):
    """What the bot may say about a candidate. An allow-list, not a passthrough.

    `extra="ignore"` is doing real work here: the bot's own record carries
    Aadhaar and PAN numbers, because a documentation officer needs them, and
    this system has no screen that shows them and no workflow that reads them.
    A model that accepted whatever arrived would store them the first time a
    mapping bug sent them, and nobody would notice until an audit. Fields not
    named below are dropped at the door.
    """

    model_config = ConfigDict(extra="ignore")

    #: The only thing required. A candidate who finished registering without a
    #: readable name is still a person a recruiter has to be able to open, so
    #: the bot falls back to their WhatsApp display name and then their number.
    full_name: str = Field(min_length=1)

    phone: str | None = None
    phone_e164: str | None = None
    email: str | None = None

    # Residence.
    location: str | None = None
    city: str | None = None
    country: str | None = None

    # Destination — one actual country, never a region or a pair.
    destination_country: str | None = None

    # What they want to do, twice: the controlled value the policy reads, and
    # their own words for a person to read.
    job_category: str | None = None
    job_preference: str | None = None

    # The job they picked, from `job_designations`. The id is the join key; the
    # title is stored beside it so a job retired later still reads as the job
    # this person applied for.
    job_id: str | None = None
    job_title: str | None = None
    #: The trade qualification behind the application — "ITI Electrician".
    course_or_trade: str | None = None
    #: A state, emirate or city inside the destination. Never a substitute for
    #: `destination_country`, which is what the CV policy reads.
    state_preference: str | None = None
    #: When they can start, in their own words. Free text — "immediately",
    #: "after 2 months" — because that is how the question gets answered.
    available_from: str | None = None
    #: Their answers to the screening questions attached to that job.
    job_answers: list[JobAnswerIn] = Field(default_factory=list)

    skills: list[str] = Field(default_factory=list)
    trade_skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    #: Kept as a band. Never coerced into `total_experience_years` — "3_5"
    #: becoming 4.0 is a figure the candidate never gave.
    total_experience_band: str | None = None
    total_experience_years: float | None = None

    #: Passport only. Overseas placement turns on whether it is in date.
    passport_number: str | None = None
    passport_expiry: str | None = None


class WhatsAppIdentityDocumentIn(BaseModel):
    """One Aadhaar or passport the bot read, as it files it here.

    `result` travels untouched and is deliberately not an allow-list: it is the
    extractor's own payload, and the CRM already owns the code that projects an
    Aadhaar or a passport out of exactly this shape — `store_aadhaar_record`
    and `store_passport_record`, the same two the mailbox pipeline feeds.
    Naming the fields again here would be a second implementation of one
    projection, and the extractors gain fields.

    That is a different judgement from `WhatsAppProfileIn`, and the difference
    is where the data lands. The profile is projected onto the candidate
    document that every recruiter list reads wholesale, so an Aadhaar number
    arriving there would be served to a browser; these go to their own
    collections, masked on the way out, which is what those collections are
    for.

    No file. The bytes are their own request — see
    `POST /candidates/{id}/identity/{type}/{record_id}/file`. A partial sync
    runs on every answered question and inlining a passport photograph would
    put it on the wire twenty times for one registration.
    """

    model_config = ConfigDict(extra="ignore")

    #: The bot's upload id. Doubles as this record's `_id`, so a re-send
    #: overwrites its own row rather than adding another — the same idea as the
    #: email path's `(message, attachment, mode)` key, with the ids this path
    #: has.
    #: Not required, and that is the point: a malformed document must not cost
    #: a candidate their registration. One without an id is skipped and
    #: reported in the response — see `identity_intake.file_documents` — while
    #: the profile and the documents beside it land as normal.
    record_id: str = Field(default="", max_length=128)
    #: Which slot it arrived in: `aadhaar`, `aadhaar_back`, `passport`.
    slot: str | None = Field(default=None, max_length=64)
    filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=128)
    sha256: str | None = Field(default=None, max_length=128)
    #: The message the file arrived on, so provenance names it the way the
    #: email path names a Gmail message id.
    message_id: str | None = Field(default=None, max_length=255)
    uploaded_at: str | None = None
    extracted_at: str | None = None
    #: Untyped on purpose. This is an extractor's payload and the CRM's job is
    #: to project it, not to police its shape — and a `dict` here would turn an
    #: OCR service returning something unexpected into a 422 that refuses the
    #: whole submission, profile included. A payload the projection cannot read
    #: costs one document and is reported as such.
    result: Any = None


class WhatsAppIdentitySectionIn(BaseModel):
    """The Aadhaar and passport documents on one submission.

    Two lists rather than one, because the type is what decides which
    collection a document goes to and reading it off a field inside `result`
    would let a mislabelled payload file a passport as an Aadhaar. The key is
    the type, and it is the route that says so.
    """

    model_config = ConfigDict(extra="ignore")

    aadhaar: list[WhatsAppIdentityDocumentIn] = Field(default_factory=list)
    passport: list[WhatsAppIdentityDocumentIn] = Field(default_factory=list)


class WhatsAppIdentityFileIn(BaseModel):
    """The bytes of one identity document, for the JSON upload path."""

    filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=128)
    content_base64: str


class WhatsAppCandidateIn(BaseModel):
    source: str = "whatsapp"
    profile: WhatsAppProfileIn
    #: Stable per candidate: `whatsapp/{phone_number_id}/{wa_user_id}`. The
    #: unique index on it is what makes a retry idempotent.
    idempotency_key: str = Field(min_length=1)
    #: What the bot believes about the CV requirement. Recorded and compared,
    #: never trusted — the CRM derives its own answer from the policy table.
    cv_required_claim: bool | None = None
    resume: WhatsAppResumeIn | None = None
    #: The identity documents this candidate has sent so far, as their
    #: extractor read them. Sent on every submission, because a candidate who
    #: sends their passport on Friday has to reach the record that was created
    #: on Tuesday — and re-sending is free: each document overwrites its own
    #: row.
    identity: WhatsAppIdentitySectionIn | None = None


def _intake_error_response(exc: IntakeError, cv_required: bool | None = None) -> JSONResponse:
    """One shape for every refusal, with the code at the top level.

    `detail` is a plain string as well, because that is where an HTTP client
    looks first and a caller should not have to understand this envelope to log
    something useful.
    """
    body: dict = {"code": exc.code, "detail": exc.message}
    if exc.code == "CV_REQUIRED":
        body["cv_required"] = True
        body["cv_policy_version"] = policy_version()
    elif cv_required is not None:
        body["cv_required"] = cv_required
    return JSONResponse(status_code=exc.status_code, content=body)


@app.get("/policy/cv-required")
def cv_policy_lookup(
    destination_country: str | None = Query(default=None),
    job_category: str | None = Query(default=None),
    _service: None = Depends(require_service_key),
) -> dict:
    """Whether a CV is needed for this destination and job.

    Asked by the bot mid-conversation, so the question it puts next matches the
    rule that will be applied when it submits. It is advisory in exactly one
    sense: `POST /candidates` re-derives the answer and does not consult what
    the bot was told here, so a stale cache on the bot's side costs a wrong
    question and never a wrong record.

    An unknown combination is not an error. It resolves to the policy's default,
    which is "required" — the safe direction, because an unnecessary CV costs a
    question and a missing one costs a placement.
    """
    return {
        "destination_country": destination_country,
        "job_category": job_category,
        "cv_required": is_cv_required(destination_country, job_category),
        "policy_version": policy_version(),
    }


@app.post("/candidates", status_code=201)
def create_whatsapp_candidate(
    payload: WhatsAppCandidateIn,
    response: Response,
    _service: None = Depends(require_service_key),
):
    """Create (or refresh) one candidate submitted by the WhatsApp bot.

    WhatsApp only. Email candidates are created by the mailbox pipeline and by
    nothing else: that path already resolves a thread, an attachment and a
    résumé hash into a record, and a second door into the same collection would
    be a second set of rules to keep in step with the first.

    The order of operations lives in `intake_whatsapp_candidate`. What this
    function does is what a route should: authenticate, validate the shape,
    store the file if one came with it, and translate the outcome into HTTP.
    """
    if (payload.source or "").strip().lower() != "whatsapp":
        return JSONResponse(
            status_code=422,
            content={
                "code": "unsupported_source",
                "detail": (
                    "this endpoint creates whatsapp candidates only; email candidates "
                    "are created by the mailbox pipeline"
                ),
            },
        )

    # A typo must not sail through to the policy table and land on the default,
    # which would look like a working rule and be nothing of the sort. Absent is
    # allowed — a candidate bound for the Gulf never answers this question, and
    # an unknown category resolves to "CV required" anyway.
    #
    # Checked against the table rather than the built-in tuple, which is what
    # `known_job_ids` was written for and was never wired to. The two disagree
    # the moment an admin adds a job: the bot offers it within five minutes, a
    # candidate picks it, and the submission is refused as an unknown category —
    # so Data Management could add a row that nobody could then apply for, and
    # the failure landed on the candidate rather than on the person who made it.
    #
    # The union is deliberate. `known_job_ids` reads the table, so a job retired
    # after somebody answered it — and "other", if a deployment's table has no
    # row for it — stay acceptable. This check exists to catch a typo; no
    # narrowing of it is worth refusing a real registration over.
    category = (payload.profile.job_category or "").strip()
    accepted = set(known_job_ids()) | set(JOB_CATEGORIES)
    if category and category not in accepted:
        return JSONResponse(
            status_code=422,
            content={
                "code": "unknown_job_category",
                "detail": (
                    f"job_category {category!r} is not one of: {', '.join(sorted(accepted))}"
                ),
            },
        )

    profile = CandidateProfile(
        # Nothing here was parsed out of a résumé, and saying otherwise would
        # put a confidence score on a form somebody filled in by tapping.
        is_resume=False,
        confidence=0.0,
        **payload.profile.model_dump(exclude_none=True),
    )

    repository = repo()

    # The file, if one came with the submission. Written before the record so a
    # storage failure refuses the intake rather than leaving a candidate whose
    # résumé pointer leads nowhere.
    stored_resume = None
    if payload.resume is not None:
        try:
            raw = base64.b64decode(payload.resume.content_base64, validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse(
                status_code=422,
                content={"code": "invalid_resume", "detail": "resume content is not valid base64"},
            )
        try:
            stored_resume = store_resume(
                # Keyed on the submission rather than the candidate id, which
                # does not exist yet. Stable per candidate, so a retry overwrites
                # its own file instead of leaving a trail of orphans.
                candidate_id=_resume_owner_hint(payload.idempotency_key),
                data=raw,
                filename=payload.resume.filename,
                mime_type=payload.resume.mime_type,
            )
        except ResumeRejected as exc:
            return JSONResponse(status_code=422, content={"code": exc.code, "detail": exc.message})

    try:
        result = intake_whatsapp_candidate(
            profile=profile,
            idempotency_key=payload.idempotency_key,
            cv_required_claim=payload.cv_required_claim,
            resume=stored_resume,
            repo=repository,
        )
    except IntakeError as exc:
        return _intake_error_response(exc)
    except DuplicateKeyError:
        # The résumé hash collided: this exact file is already on file under
        # someone else. Reported rather than merged — two people who sent the
        # same document is a question for a human.
        return JSONResponse(
            status_code=409,
            content={
                "code": "duplicate_resume",
                "detail": "this resume is already on another candidate",
            },
        )

    # The Aadhaar and the passport, filed against whichever candidate the
    # intake resolved. After the intake and never before it: these belong to a
    # candidate, and on a late upload — the common case, since documents are
    # the last thing a registration collects — that candidate is one who has
    # existed since Tuesday. Filing them first would mean choosing an owner
    # before the code whose job that is had run.
    #
    # Failures here are reported, not raised. The profile is written by the
    # time this runs and an unreadable passport must not undo a registration.
    identity_filed: list[dict] = []
    if payload.identity is not None:
        identity_filed = [
            {
                "document_type": entry.document_type,
                "record_id": entry.record_id,
                "stored": entry.stored,
                **({"skipped": entry.skipped} if entry.skipped else {}),
            }
            for entry in file_identity_documents(
                candidate_id=result.candidate_id,
                section=payload.identity.model_dump(exclude_none=True),
                idempotency_key=payload.idempotency_key,
            )
        ]

    # 201 only when something was actually created. A replay of the same key,
    # and a re-registration that refreshed someone already on file, are both
    # 200: nothing new exists because of them.
    if not result.created:
        response.status_code = 200

    return {
        "success": True,
        "candidate_id": result.candidate_id,
        "created": result.created,
        "cv_required": result.cv_required,
        "cv_policy_version": result.cv_policy_version,
        "policy_overrode_claim": result.policy_overrode_claim,
        # So the bot knows which documents landed and can stop offering the
        # ones that did. An empty list is a submission that carried none.
        "identity_documents": identity_filed,
    }


def _resume_owner_hint(idempotency_key: str) -> str:
    """A short, stable, filesystem-safe stand-in for the candidate id.

    A résumé sent with a submission is stored before the candidate exists, so
    there is no id to key it on. The idempotency key is the next best thing:
    stable across retries of the same submission, unique per candidate, and
    already the identifier this whole flow turns on.
    """
    return "wa" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]


@app.post("/candidates/{candidate_id}/resume")
def upload_candidate_resume(
    candidate_id: str,
    file: UploadFile = File(...),
    _service: None = Depends(require_service_key),
):
    """Attach a résumé to a candidate who already exists.

    For the file that arrives after the person does: someone the policy exempted
    who sent a CV anyway, or who sends one later. A candidate the policy
    *requires* a CV for never reaches this endpoint, because they could not have
    been created without the file in the first place.

    WhatsApp candidates only. An email candidate's résumé is the thing they were
    created from, and replacing it here would leave the record disagreeing with
    the message it was ingested from.
    """
    repository = repo()
    record = repository.get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if record.source != "whatsapp":
        raise HTTPException(
            status_code=409,
            detail="this endpoint attaches resumes to whatsapp candidates only",
        )
    if record.resume is not None:
        # Attaching, not replacing. A recruiter may have read the résumé on file
        # and formed a view of this candidate, and swapping the document
        # underneath that view is a substitution rather than a refresh — one
        # that would happen silently. The bot treats this as an ordinary outcome
        # and keeps its own copy.
        return JSONResponse(
            status_code=409,
            content={
                "code": "resume_already_present",
                "detail": "this candidate already has a resume; it was not replaced",
            },
        )

    data = file.file.read()
    try:
        stored = store_resume(
            candidate_id=candidate_id,
            data=data,
            filename=file.filename,
            mime_type=file.content_type,
        )
    except ResumeRejected as exc:
        return JSONResponse(status_code=422, content={"code": exc.code, "detail": exc.message})

    try:
        attached = repository.attach_resume(candidate_id, stored)
    except DuplicateKeyError:
        return JSONResponse(
            status_code=409,
            content={
                "code": "duplicate_resume",
                "detail": "this resume is already on another candidate",
            },
        )
    if not attached:
        raise HTTPException(status_code=404, detail="Candidate not found")

    return {
        "success": True,
        "candidate_id": candidate_id,
        "resume": stored.model_dump(mode="json"),
    }


@app.post("/candidates/{candidate_id}/identity/{document_type}/{record_id}/file")
def attach_identity_document_file(
    candidate_id: str,
    document_type: str,
    record_id: str,
    file: UploadFile = File(...),
    _service: None = Depends(require_service_key),
):
    """The scan itself, for a document the submission already described.

    Two requests rather than one, and the split is the same one the résumé
    already makes: the record travels with every submission because it is small
    and a partial sync runs on every answered question, and the bytes travel
    once because they are not. What makes "once" work is that the bot knows the
    digest it has handed over and stops offering the same file.

    The record has to exist first — this attaches a file to a document, it does
    not create one. A 404 here means the submission carrying that document has
    not landed yet, and the bot's next sync sends both in the right order.

    Multipart rather than base64, exactly as `POST /candidates/{id}/resume`: a
    passport photograph is a couple of megabytes and base64 would make it a
    third bigger for nothing.
    """
    from app.db.identity_records import DOCUMENT_TYPES, attach_file, find_one
    from app.services import identity_files

    if document_type not in DOCUMENT_TYPES:
        raise HTTPException(
            status_code=404,
            detail=f"'{document_type}' is not an identity document type",
        )

    record = repo().get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if record.source != "whatsapp":
        # An email candidate's identity documents are pages of the bundle they
        # were ingested from, and `identity_files.load` cuts them out of it on
        # demand. Writing a second copy over the top would leave the record
        # disagreeing with the file it names.
        raise HTTPException(
            status_code=409,
            detail="this endpoint attaches scans to whatsapp candidates only",
        )

    try:
        doc = find_one(candidate_id, document_type, record_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Identity records unavailable: {exc}")
    if not doc:
        return JSONResponse(
            status_code=404,
            content={
                "code": "identity_record_not_found",
                "detail": (
                    f"no {document_type} record {record_id!r} for this candidate — "
                    "send the submission describing it first"
                ),
            },
        )

    try:
        stored = identity_files.store(
            candidate_id=candidate_id,
            document_type=document_type,
            record_id=record_id,
            data=file.file.read(),
            filename=file.filename,
            mime_type=file.content_type,
            # So a file already on this record, or already stored as this
            # candidate's résumé, is pointed at rather than written again.
            existing=doc.get("file"),
            resume=record.resume,
        )
    except identity_files.IdentityRejected as exc:
        return JSONResponse(status_code=422, content={"code": exc.code, "detail": exc.message})

    if not attach_file(document_type, record_id, candidate_id, stored):
        raise HTTPException(status_code=404, detail="Identity record not found")

    return {
        "success": True,
        "candidate_id": candidate_id,
        "document_type": document_type,
        "record_id": record_id,
        # Never the storage key. The name, type and size are what a caller can
        # do anything with.
        "file": {
            key: stored.get(key)
            for key in ("filename", "mime_type", "size", "sha256", "shared_with_resume")
            if stored.get(key) is not None
        },
    }


# --------------------------------------------------------------------------- #
#  B2B enquiries, as the bot files them
#
#  The other half of what the bot collects. Same credential as the candidate
#  intake, deliberately: it is the same system on the other end of the wire, and
#  a second key would be a second secret to rotate for no gain in what either
#  one protects.
# --------------------------------------------------------------------------- #


class B2BEnquiryIn(BaseModel):
    """What the bot may say about a manpower requirement. An allow-list.

    `extra="ignore"` for the same reason the candidate intake uses it: the bot's
    own conversation record carries far more than this system has a screen for,
    and a model that accepted whatever arrived would start storing it the first
    time a mapping bug sent it. Fields not named below are dropped at the door.

    Almost everything is optional, and that is not laxity. An agent messages
    "I need 40 welders for Qatar" and leaves; refusing that for want of a
    contact email loses the enquiry entirely, and an enquiry with three fields
    filled in is still an enquiry a recruiter can act on. What cannot be missing
    is the two things that make it *findable*: who to call back, and a key that
    stops a retry filing it twice.
    """

    model_config = ConfigDict(extra="ignore")

    #: Unique per *enquiry*, not per sender — an agent raises many, and each one
    #: is a real vacancy. A submission id, not a WhatsApp user id.
    idempotency_key: str = Field(min_length=1, max_length=200)

    party_type: str | None = None
    company_name: str | None = Field(default=None, max_length=200)
    #: The one field the screen cannot render a row without. The bot falls back
    #: to the sender's WhatsApp display name, and then to their number.
    contact_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    phone_e164: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)

    #: What they asked for, in their own words. The field a recruiter reads
    #: first, and the only one that survives every conversation that went off
    #: the script.
    requirement: str | None = Field(default=None, max_length=4000)
    job_title: str | None = Field(default=None, max_length=200)
    #: The taxonomy id, when they picked from the list the bot offered.
    job_id: str | None = Field(default=None, max_length=80)
    headcount: int | None = None
    destination_country: str | None = Field(default=None, max_length=100)
    salary_budget: str | None = Field(default=None, max_length=120)
    experience_required: str | None = Field(default=None, max_length=120)
    skills: list[str] | str = Field(default_factory=list)
    #: When they need people, in their own words — "next month", "before Eid".
    #: Free text because that is how the question gets answered.
    needed_by: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)

    wa_user_id: str | None = Field(default=None, max_length=100)


@app.post("/b2b-enquiries", status_code=201)
def create_b2b_enquiry(
    payload: B2BEnquiryIn,
    response: Response,
    _service: None = Depends(require_service_key),
) -> dict:
    """File one manpower requirement raised over WhatsApp.

    Stores what was said and nothing more. It does not create a job order, does
    not allocate anyone, and does not create a Sourcing Hub record for a company
    nobody at the agency has agreed to work with — it only *matches* against the
    ones already on file, so a known agent's enquiry arrives with their name on
    it. Every one of those is a decision, and the screen is where they are taken.

    201 when the enquiry is new, 200 on a replay of the same key. Both return
    the enquiry, so a bot that timed out and retried can tell the operator which
    reference the agency will quote back without needing to know which of the
    two calls actually stored it.
    """
    from app.db.b2b_enquiries import record_enquiry

    doc, created = record_enquiry(payload.model_dump(exclude_none=True), source="whatsapp")
    if not created:
        response.status_code = 200

    log.info(
        "B2B enquiry %s from %s (%s) — %s",
        doc.get("id"),
        doc.get("company_name") or doc.get("contact_name"),
        doc.get("party_type"),
        "created" if created else "replayed",
    )

    return {
        "success": True,
        "created": created,
        "enquiry_id": doc.get("id"),
        "status": doc.get("status"),
        # Echoed back so the bot can confirm the requirement it just read out to
        # the agent is the one the CRM stored, rather than assuming it.
        "enquiry": _enquiry_json(doc),
    }


# --------------------------------------------------------------------------- #
#  Data Management
#
#  The jobs the agency recruits for, the countries it sends people to, and the
#  questions it asks about a job. Admin-owned, and the reason they are here
#  rather than in a constant somewhere is that the person who knows a new job
#  has opened is an admin and not a programmer.
#
#  Two consumers, and only one of them is this screen: the CV policy resolves
#  `destination_country + job_id` against these rows, and the WhatsApp bot draws
#  its job and country questions from them. Adding a job here is what puts it in
#  front of candidates.
# --------------------------------------------------------------------------- #


class JobDesignationIn(BaseModel):
    """A job as the admin form submits it."""

    model_config = ConfigDict(extra="ignore")

    #: Omitted when creating — the id is derived from the title once and then
    #: never changes, because it is what the CV rules and every candidate
    #: already on file point at.
    id: str | None = None
    title: str = Field(min_length=1, max_length=80)
    active: bool = True
    bot_visible: bool = True
    bot_order: int = 100
    #: The rule when no country says otherwise.
    cv_required_default: bool = True
    #: `{"Malaysia": false}` — the exceptions. Keys are country names as a
    #: person writes them; matching is case-insensitive.
    cv_overrides: dict[str, bool] = Field(default_factory=dict)


class CountryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str = Field(min_length=1, max_length=60)
    active: bool = True
    bot_visible: bool = True
    bot_order: int = 100


class JobQuestionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    job_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=300)
    #: `text` for a typed answer, `choice` for a tap.
    kind: str = "text"
    choices: list[str] = Field(default_factory=list)
    required: bool = False
    order: int = 100
    active: bool = True


@app.get("/job-designations")
def list_job_designations(_user: dict = Depends(require_admin)) -> dict:
    from app.db.taxonomy import list_jobs

    return {"items": list_jobs()}


@app.post("/job-designations")
def save_job_designation(payload: JobDesignationIn, user: dict = Depends(require_admin)) -> dict:
    """Create a job, or edit one that exists.

    The id is generated from the title on creation and is immutable afterwards.
    That is the difference between a label and a key: a title is something a
    person rewords, and every CV rule, every candidate record and every message
    the bot has ever sent points at the id.
    """
    from app.db.taxonomy import get_job, job_doc, slugify, upsert_job

    if payload.id:
        existing = get_job(payload.id)
        if not existing:
            raise HTTPException(status_code=404, detail="Job designation not found")
        updated = dict(existing)
        updated.update(
            {
                "title": payload.title.strip(),
                "active": payload.active,
                "bot_visible": payload.bot_visible,
                "bot_order": payload.bot_order,
                "cv_required_default": payload.cv_required_default,
                "cv_overrides": {
                    (k or "").strip().casefold(): bool(v)
                    for k, v in payload.cv_overrides.items()
                    if (k or "").strip()
                },
            }
        )
        saved = upsert_job(updated)
        log.info("Job designation %s edited by %s", saved["id"], user.get("email"))
        return {"status": "ok", "item": saved}

    job_id = slugify(payload.title)
    if get_job(job_id):
        raise HTTPException(
            status_code=409,
            detail=f"A job with the id {job_id!r} already exists — edit that one instead",
        )

    saved = upsert_job(
        job_doc(
            job_id=job_id,
            title=payload.title,
            cv_required_default=payload.cv_required_default,
            cv_overrides=payload.cv_overrides,
            bot_visible=payload.bot_visible,
            bot_order=payload.bot_order,
            active=payload.active,
            created_by=user.get("email"),
        )
    )
    log.info("Job designation %s created by %s", saved["id"], user.get("email"))
    return {"status": "ok", "item": saved}


@app.delete("/job-designations/{job_id}")
def retire_job_designation(job_id: str, _user: dict = Depends(require_admin)) -> dict:
    """Retire a job. It is deactivated, never erased — candidates point at it."""
    from app.db.taxonomy import delete_job

    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job designation not found")
    return {"status": "retired", "id": job_id}


@app.get("/countries")
def list_country_rows(_user: dict = Depends(require_admin)) -> dict:
    from app.db.taxonomy import list_countries

    return {"items": list_countries()}


@app.post("/countries")
def save_country(payload: CountryIn, user: dict = Depends(require_admin)) -> dict:
    from app.db.taxonomy import country_doc, slugify, upsert_country

    doc = country_doc(
        name=payload.name,
        bot_visible=payload.bot_visible,
        bot_order=payload.bot_order,
        active=payload.active,
        created_by=user.get("email"),
    )
    if payload.id:
        doc["_id"] = doc["id"] = payload.id
    else:
        doc["_id"] = doc["id"] = slugify(payload.name)

    saved = upsert_country(doc)
    log.info("Country %s saved by %s", saved["id"], user.get("email"))
    return {"status": "ok", "item": saved}


@app.delete("/countries/{country_id}")
def retire_country(country_id: str, _user: dict = Depends(require_admin)) -> dict:
    from app.db.taxonomy import delete_country

    if not delete_country(country_id):
        raise HTTPException(status_code=404, detail="Country not found")
    return {"status": "retired", "id": country_id}


@app.get("/job-questions")
def list_all_job_questions(
    job_id: str | None = Query(default=None), _user: dict = Depends(require_admin)
) -> dict:
    from app.db.taxonomy import list_job_questions

    return {"items": list_job_questions(job_id)}


@app.post("/job-questions")
def save_job_question(payload: JobQuestionIn, user: dict = Depends(require_admin)) -> dict:
    """A question the bot asks candidates who choose this job."""
    from app.db.taxonomy import get_job, question_doc, upsert_job_question

    if not get_job(payload.job_id):
        raise HTTPException(status_code=404, detail=f"No job with id {payload.job_id!r}")

    doc = question_doc(
        job_id=payload.job_id,
        text=payload.text,
        kind=payload.kind,
        choices=payload.choices,
        required=payload.required,
        order=payload.order,
        active=payload.active,
        question_id=payload.id,
        created_by=user.get("email"),
    )
    saved = upsert_job_question(doc)
    return {"status": "ok", "item": saved}


@app.delete("/job-questions/{question_id}")
def remove_job_question(question_id: str, _user: dict = Depends(require_admin)) -> dict:
    from app.db.taxonomy import delete_job_question

    if not delete_job_question(question_id):
        raise HTTPException(status_code=404, detail="Question not found")
    return {"status": "deleted", "id": question_id}


@app.get("/job-designations/{job_id}/cv-matrix")
def job_cv_matrix(job_id: str, _user: dict = Depends(require_admin)) -> dict:
    """What this job's rules actually resolve to, country by country.

    The admin form takes a default and a handful of exceptions; what a recruiter
    wants to see is the answer for each destination they actually send people
    to. Computed through the same function the bot's request goes through, so
    the screen cannot drift from the decision.
    """
    from app.db.taxonomy import get_job, list_countries
    from app.policy.cv_policy import resolve_cv_requirement

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job designation not found")

    rows = []
    for country in list_countries(active_only=True):
        required, reason = resolve_cv_requirement(country["name"], job_id)
        rows.append(
            {
                "country": country["name"],
                "cv_required": required,
                "reason": reason,
                "is_override": (country["name"] or "").strip().casefold()
                in (job.get("cv_overrides") or {}),
            }
        )
    return {"job": job, "matrix": rows}


# --------------------------------------------------------------------------- #
#  The taxonomy, as the bot reads it
#
#  Service key, not a staff session: this is the bot asking what to put in front
#  of a candidate, and it is the same credential it submits candidates with.
#
#  Only what a candidate can be offered comes back — active, bot-visible, in the
#  order an admin put them in — because the bot's job is to ask a question, not
#  to filter an admin's spreadsheet.
# --------------------------------------------------------------------------- #
@app.get("/taxonomy")
def bot_taxonomy(_service: None = Depends(require_service_key)) -> dict:
    from app.db.taxonomy import BOT_LIST_LIMIT, list_countries, list_jobs, taxonomy_version

    jobs = list_jobs(bot_only=True)
    countries = list_countries(bot_only=True)

    return {
        "version": taxonomy_version(),
        # WhatsApp allows ten rows in a list and rejects an eleventh, so the bot
        # is told the limit rather than left to discover it. It shows the first
        # nine and an "Other" row; anything past that is reached by typing.
        "bot_list_limit": BOT_LIST_LIMIT,
        "jobs": [
            {"id": j["id"], "title": j["title"], "order": j.get("bot_order", 100)} for j in jobs
        ],
        "countries": [
            {"id": c["id"], "name": c["name"], "order": c.get("bot_order", 100)}
            for c in countries
        ],
    }


@app.get("/jobs/{job_id}/questions")
def bot_job_questions(job_id: str, _service: None = Depends(require_service_key)) -> dict:
    """The extra questions to ask a candidate who chose this job.

    Written by an admin who knows what a client asks about a welder and the bot
    does not. Returned in the admin's order; the bot asks them after the job is
    chosen and stores the answers on the candidate.
    """
    from app.db.taxonomy import list_job_questions

    return {
        "job_id": job_id,
        "questions": [
            {
                "id": q["id"],
                "text": q["text"],
                "kind": q.get("kind", "text"),
                "choices": q.get("choices", []),
                "required": bool(q.get("required")),
            }
            for q in list_job_questions(job_id, active_only=True)
        ],
    }


# --------------------------------------------------------------------------- #
#  What the bot needs to announce an allocation
#
#  Two reads, both on the service key. When a candidate is allocated the CRM
#  asks the bot to message the staff member on WhatsApp (see
#  `app.staff_whatsapp`), and that request carries two ids and nothing else.
#  These are what the bot reads back to compose the message.
#
#  Narrow on purpose. The bot is not handed the roster or the candidate record;
#  it is handed one staff member's contact details and the handful of facts that
#  appear in the message. A notification path that ships everything is a second
#  copy of the database on the other side of the wire, kept in step by nobody.
# --------------------------------------------------------------------------- #
@app.get("/staff/{staff_id}/contact")
def bot_staff_contact(staff_id: str, _service: None = Depends(require_service_key)) -> dict:
    """Where to reach one staff member, and whether they should be reached.

    `active` travels rather than being enforced here. A deactivated account can
    still own work — deleting an account is what redistributes its queue,
    deactivating one is not — so the bot is told the state and decides, keeping
    that decision beside the rest of the sending rules instead of splitting it
    across two services.
    """
    member = users.get(staff_id)
    if not member:
        raise HTTPException(status_code=404, detail="Staff account not found")

    return {
        "id": member.id,
        "name": member.name,
        "phone": member.phone,
        "role": member.role,
        "active": member.active,
    }


@app.get("/staff/admin-contacts")
def bot_admin_contacts(_service: None = Depends(require_service_key)) -> dict:
    """Everyone who should hear that work has gone unattended.

    Every admin, because that is who the SLA feed already goes to — an alert
    that reached one of three would be a rota nobody agreed to keep. Accounts
    with no number on file come back all the same: the bot skips them, and it
    logs which, which is how an admin finds out their own account is the one
    that has been silently excluded.

    Deactivated accounts do not appear — `list_admins` already excludes them,
    and that is the right rule here too: unlike a staff member, an admin holds
    no queue that outlives their account, so there is nothing a deactivated one
    still needs to be told about.
    """
    return {
        "contacts": [
            {"id": member.id, "name": member.name, "phone": member.phone}
            for member in users.list_admins()
        ]
    }


@app.get("/candidates/{candidate_id}/assignment-summary")
def bot_assignment_summary(
    candidate_id: str, _service: None = Depends(require_service_key)
) -> dict:
    """The facts the assignment message is built from, and nothing else.

    Deliberately not `GET /candidates/{id}`: that one answers a recruiter's
    screen and carries the whole profile, the stored OCR and the evaluation. The
    bot is composing a few lines of a WhatsApp message and has no use for the
    rest — and the less of a candidate that crosses this hop, the less there is
    to leak on the other side of it.

    `documents` is what is actually **on file**, not what the conversation set
    out to collect: "Documents: Passport, CV" is a claim about this record, and
    a bot that reported its own intentions would be announcing documents nobody
    can open.
    """
    record = repo().get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")

    profile = record.profile

    documents: list[str] = []
    if record.resume:
        documents.append("CV")

    try:
        from app.db.identity_records import find_for_candidate

        found = find_for_candidate(candidate_id)
    except Exception as exc:  # noqa: BLE001
        # The identity collections being unreachable is not a reason to send no
        # message at all: everything else is already in hand, and a message
        # listing one document fewer beats silence about a new allocation.
        log.warning("Identity documents unavailable for candidate %s: %s", candidate_id, exc)
        found = {}

    if found.get("passport"):
        documents.append("Passport")
    if found.get("aadhaar"):
        documents.append("Aadhaar")

    return {
        "candidate_id": record.id,
        "source": record.source,
        "full_name": profile.full_name,
        # Where they want to work, never where they live. The message is read by
        # a recruiter deciding what to do with this person, and "Country: Tamil
        # Nadu" answers a question nobody asked — `country` and
        # `destination_country` exist as two fields precisely so that this
        # cannot be got wrong by accident.
        "destination_country": profile.destination_country,
        # Through the three in this order on purpose: the title they applied for
        # is what a person reads, their own words are better than a controlled
        # value, and the controlled value is better than a blank line.
        "job": profile.job_title or profile.job_preference or profile.job_category,
        "phone": profile.phone_e164 or profile.phone,
        "documents": documents,
        # So the bot can check it is announcing the allocation that actually
        # stands. A relay that arrives after a rebalance has moved the candidate
        # on would otherwise tell the wrong person they own them.
        "assigned_staff_id": record.assigned_staff_id,
        # *When* that allocation happened, which is what tells one assignment
        # apart from a retry of the same one. The bot messages once per moment:
        # a duplicate relay reads this same timestamp and is refused, while a
        # candidate genuinely moved back to a previous owner carries a new one
        # and is announced. Without it the bot could only dedupe on the pair of
        # ids, and A -> B -> A would go unsaid.
        "assigned_at": record.assigned_at.isoformat() if record.assigned_at else None,
    }


# --------------------------------------------------------------------------- #
#  User Management
#
#  Accounts and what each of them may reach. Two things an admin does here:
#  create a user, and decide which pages that user sees.
#
#  Permissions add; they never subtract. A grant puts a page on someone's rail,
#  and it does not widen what they are allowed to see once they are on it — a
#  staff member with the Candidates page still sees only the candidates
#  allocated to them, because that restriction lives in the API's own scoping
#  and not in the menu.
# --------------------------------------------------------------------------- #


class UserIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str = Field(min_length=3)
    password: str = Field(min_length=6)
    name: str = ""
    role: str = STAFF_ROLE
    page_grants: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    phone: str = Field(default="", max_length=40)


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = None
    page_grants: list[str] | None = None
    keywords: list[str] | None = None
    phone: str | None = Field(default=None, max_length=40)


@app.get("/users")
def list_users(_user: dict = Depends(require_admin)) -> dict:
    """Every account, and the pages each one reaches."""
    from app.db.users import PAGES

    return {
        "items": [u.to_public() for u in users.list_all()],
        # The vocabulary the permission screen renders its checkboxes from, so a
        # page added to the system appears there without a frontend release.
        "pages": list(PAGES),
    }


@app.post("/users", status_code=201)
def create_user(payload: UserIn, admin: dict = Depends(require_admin)) -> dict:
    from app.db.users import ADMIN_ROLE as _ADMIN, STAFF_ROLE as _STAFF

    role = payload.role if payload.role in (_ADMIN, _STAFF) else _STAFF
    try:
        user = users.create(
            email=payload.email,
            password=payload.password,
            name=payload.name,
            role=role,
            page_grants=payload.page_grants,
            phone=payload.phone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if payload.keywords:
        users.update_user(user.id, keywords=payload.keywords)
        user = users.get(user.id)

    log.info("User %s (%s) created by %s", payload.email, role, admin.get("email"))
    return {"status": "ok", "user": user.to_public()}


@app.patch("/users/{user_id}")
def update_user(user_id: str, payload: UserPatch, admin: dict = Depends(require_admin)) -> dict:
    """Edit an account, including which pages it reaches.

    Two guards, and both exist to stop an admin locking everybody out with one
    click: the last active administrator cannot be demoted, and cannot be
    deactivated. There is no way back from either through the interface that
    would have to be used to undo it.
    """
    from app.db.users import ADMIN_ROLE as _ADMIN

    target = users.get(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    losing_an_admin = target.role == _ADMIN and (
        (payload.role is not None and payload.role != _ADMIN) or payload.active is False
    )
    if losing_an_admin and users.count_active_admins() <= 1:
        raise HTTPException(
            status_code=409,
            detail="This is the last active administrator; promote someone else first.",
        )

    updated = users.update_user(
        user_id,
        name=payload.name,
        role=payload.role,
        active=payload.active,
        password=payload.password,
        page_grants=payload.page_grants,
        keywords=payload.keywords,
        phone=payload.phone,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")

    log.info("User %s updated by %s", updated.email, admin.get("email"))
    return {"status": "ok", "user": updated.to_public()}


# Serve the static files from the Next.js export.
# This must be mounted AFTER all other routes so it acts as a fallback.
frontend_out_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "out")
)
if os.path.exists(frontend_out_dir):
    app.mount("/", StaticFiles(directory=frontend_out_dir, html=True), name="frontend")


