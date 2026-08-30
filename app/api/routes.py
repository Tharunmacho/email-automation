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
import time
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import settings
from app.core.models import (
    EVALUATION_STATUSES,
    CandidateProfile,
    JobSection,
    RegistrationState,
)
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
from app.db.dedup import normalize_email, normalize_phone
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
    remove_legacy_demo_staff,
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
    # Response headers are hidden from JavaScript unless they are named here,
    # and `Content-Disposition` is where the server puts the filename. Without
    # it every download is saved under the caller's fallback name — so a
    # passport cut out of a bundle arrives as "resume.pdf".
    expose_headers=["Content-Disposition"],
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
    except Exception:  # noqa: BLE001
        pass

    # Older releases recreated a "Staff Reviewer" demo account on every boot.
    # Remove that exact legacy account and place any unread work with the real
    # roster; reviewed work stays orphaned so its history is not destroyed.
    try:
        removed_staff_id = remove_legacy_demo_staff()
        if removed_staff_id:
            redistribute_from_staff(removed_staff_id, repo=repo(), users=users)
    except Exception:  # noqa: BLE001 — account cleanup must not stop the API
        pass

    # The account the login screen advertises. Separate from the operator's own
    # admin above, and create-only: changing a demo password must survive a
    # restart.
    if settings.demo_accounts_enabled:
        try:
            ensure_demo_accounts(
                settings.demo_admin_email,
                settings.demo_admin_password,
            )
        except Exception:  # noqa: BLE001
            pass


def _under_test() -> bool:
    """True inside pytest.

    The test suite builds a `TestClient`, which runs this startup — and a
    background poller started there would reach the real mailboxes and the real
    Veris account from a unit test. The check is explicit rather than clever
    because the consequence of getting it wrong is billed work.
    """
    import sys

    return "pytest" in sys.modules


@app.on_event("startup")
async def _startup() -> None:
    import asyncio
    import logging

    logger = logging.getLogger("uvicorn.error")

    # Run ensure_indexes() in a daemon thread so the port opens immediately.
    # The database is remote, and this costs ~10 round-trips — one per index
    # across several collections — which under --reload was enough to push
    # startup past 30 s, holding ERR_CONNECTION_REFUSED open the whole time.
    # A warning on failure is still emitted; startup itself never blocks.
    def _run_indexes() -> None:
        try:
            ensure_indexes()
        except Exception as exc:
            logger.warning("MongoDB index creation deferred: %s", exc)

    t = threading.Thread(target=_run_indexes, daemon=True, name="index-bootstrap")
    t.start()

    # Ingestion runs on worker threads and, when a worker is up, in a separate
    # process. Neither has an event loop of its own, so the loop that owns the
    # WebSockets has to be handed over here or nothing can ever be pushed to a
    # browser — which is why an ingested candidate used to need a page reload.
    from app.api import websocket as ws

    ws.set_publisher_loop(asyncio.get_running_loop())
    asyncio.create_task(ws.relay_redis_events())

    # Nothing polled the mailboxes on a timer, so mail was only ever fetched
    # when somebody pressed Sync. Beat now has a poll task for deployments with
    # a worker; this covers the ones without.
    if settings.mail_autopoll_enabled and not _under_test():
        from app.ingestion import autopoll

        asyncio.create_task(autopoll.run_forever())

    try:
        from app.tasks.locks import get_redis
        client = get_redis()
        client.ping()
        logger.info("Redis connected successfully (%s)", settings.redis_url)
    except Exception as err:
        logger.info("Redis status: local direct execution mode active (Redis lock fallback: %s)", err)





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


def _has_page(user: dict, *pages: str) -> bool:
    """Whether this session may reach at least one named application page."""
    if user.get("role") == ADMIN_ROLE:
        return True
    declared = user.get("pages")
    if declared is None:
        # Compatibility for service/tests and tokens created before `pages`
        # was embedded in the public user shape: derive the role defaults.
        from app.db.users import pages_for

        declared = pages_for(user.get("role", STAFF_ROLE), user.get("page_grants") or [])
    allowed = set(declared)
    # Sessions issued before My Candidates and Candidates became one page may
    # still carry the old id until their next refresh.
    if "my-queue" in allowed:
        allowed.add("candidates")
    return any(page in allowed for page in pages)


def require_page(*pages: str):
    """FastAPI dependency for a page-level permission.

    Missing access is deliberately a 404. A user who was not granted a section
    must not be able to distinguish its API from an endpoint that does not
    exist, which matches the navigation removing the section altogether.
    """
    def dependency(user: dict = Depends(current_user)) -> dict:
        if not _has_page(user, *pages):
            raise HTTPException(status_code=404, detail="Not found")
        return user

    return dependency


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
    user: dict = Depends(require_page("candidates")),
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
def get_candidate(candidate_id: str, user: dict = Depends(require_page("candidates"))) -> dict:
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


def _fetch_resume_from_email(record: CandidateRecord) -> bytes | None:
    """Last resort: go back to the mailbox for a file storage has lost.

    Addressed by the RFC822 ``Message-ID`` rather than the UID recorded at
    ingestion, because that UID stopped meaning anything the moment the mail was
    filed into `Resumes/Processed` — re-fetching it either failed or, worse,
    returned somebody else's message.

    Whatever comes back is written to storage on the way out, so the next
    download does not have to repeat this.
    """
    source = record.source_email
    if not source or not source.message_id:
        return None

    mid = source.message_id
    # `thread_id` is where the IMAP client keeps the Message-ID header.
    rfc_id = getattr(source, "thread_id", "") or ""
    wanted_hash = (record.resume.sha256 if record.resume else "") or ""

    try:
        from app.db.dedup import sha256_hex
        from app.email_client.factory import get_all_email_clients

        for client in get_all_email_clients():
            try:
                msg = None
                finder = getattr(client, "get_message_by_rfc_id", None)
                if rfc_id and callable(finder):
                    msg = finder(rfc_id)
                if msg is None:
                    msg = client.get_message(mid)
                if not msg or not msg.attachments:
                    continue

                loaded = [a for a in msg.attachments if a.data]
                # The bundle can hold several files; the hash says which one
                # became this candidate. Without it, a covering letter attached
                # alongside the CV would be served as the résumé.
                for att in loaded:
                    if wanted_hash and sha256_hex(att.data) != wanted_hash:
                        continue
                    _restore_to_storage(record, att.data, att.mime_type)
                    return att.data
                if not wanted_hash and loaded:
                    att = loaded[0]
                    _restore_to_storage(record, att.data, att.mime_type)
                    return att.data
            except Exception as err:  # noqa: BLE001 — try the next account
                log.debug("Could not re-fetch %s from %s: %s",
                          rfc_id or mid, getattr(client, "imap_username", "client"), err)
                continue
    except Exception as err:  # noqa: BLE001
        log.warning("Live email fallback download failed for message %s: %s", mid, err)
    return None


def _restore_to_storage(record: CandidateRecord, data: bytes, mime_type: str | None) -> None:
    """Put a recovered file back where it should have been all along."""
    if not (record.resume and record.resume.storage_key):
        return
    try:
        backend = get_storage_backend()
        backend.save(record.resume.storage_key, data, content_type=mime_type)
        repo().set_storage_backend(record.id, backend.name)
        log.info("Restored the résumé for %s into %s storage", record.id, backend.name)
    except Exception as err:  # noqa: BLE001 — the download itself still succeeds
        log.warning("Could not restore the résumé for %s: %s", record.id, err)


@app.get("/candidates/{candidate_id}/resume")
def download_resume(candidate_id: str, user: dict = Depends(require_page("candidates"))) -> Response:
    record = _owned_or_404(candidate_id, user)
    if not record.resume or not record.resume.storage_key:
        raise HTTPException(status_code=404, detail="Candidate resume attachment not found")
    
    backend_name = record.resume.storage_backend or settings.storage_backend
    data = None
    try:
        data = get_storage_backend(backend_name).load(record.resume.storage_key)
    except Exception as e1:
        # Fallback check: if record backend failed, try alternate storage backend (local vs gridfs)
        try:
            alt_backend = "local" if backend_name == "gridfs" else "gridfs"
            data = get_storage_backend(alt_backend).load(record.resume.storage_key)
        except Exception as e2:
            # Fallback 2: dynamically download attachment straight from the email mailbox
            data = _fetch_resume_from_email(record)
            if not data:
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


def _post_delete_cleanup(
    storage_key: str | None,
    message_ids: list[str],
    identity: dict | None = None,
    storage_backend: str | None = None,
    linked_storage: list[dict] | None = None,
) -> None:
    """Slow, best-effort cleanup for a deleted candidate.

    Runs after the response is sent: building a Gmail service refreshes the
    OAuth token and then costs more round trips per message, so several seconds
    of network time would otherwise be charged to the caller. Neither step
    changes what the API returned, and both are safe to lose.
    """
    storage_refs = list(linked_storage or [])
    if storage_key:
        storage_refs.append({
            "storage_key": storage_key,
            "storage_backend": storage_backend,
            "shared_with_resume": False,
        })

    deleted_keys: set[tuple[str, str]] = set()
    for item in storage_refs:
        key = item.get("storage_key")
        backend_name = item.get("storage_backend") or settings.storage_backend
        if not key or item.get("shared_with_resume"):
            continue
        identity_key = (backend_name, key)
        if identity_key in deleted_keys:
            continue
        try:
            get_storage_backend(backend_name).delete(key)
            deleted_keys.add(identity_key)
        except Exception as err:
            log.warning("Could not delete stored candidate file %s: %s", key, err)

    if message_ids:
        try:
            from app.email_client import get_all_email_clients

            clients = get_all_email_clients()
            # Where the message can still be found once it has been filed: its
            # UID died with the first move, so the Message-ID header (and the
            # subject/sender pair behind it) is what locates it now.
            where = {k: v for k, v in (identity or {}).items() if isinstance(v, str) and v.strip()}

            filed = 0
            for message_id in message_ids:
                for client in clients:
                    try:
                        # Deleted first: on a folder-based account that single
                        # move is both halves of the change — it takes the mail
                        # out of Processed and puts it in Deleted at once, so it
                        # can never be seen carrying both labels or neither.
                        if settings.gmail_deleted_label:
                            if client.apply_label(
                                message_id, settings.gmail_deleted_label, **where
                            ):
                                filed += 1
                        if settings.gmail_processed_label:
                            client.remove_label(message_id, settings.gmail_processed_label, **where)
                    except Exception as err:  # noqa: BLE001
                        log.warning(
                            "Error re-labeling message %s on client %s: %s",
                            message_id, getattr(client, "imap_username", "client"), err,
                        )

            # The same résumé is usually delivered to every mailbox but ingested
            # from one, so `filed` is normally lower than the message count.
            # Zero is the case worth seeing: nothing was re-filed anywhere, and
            # a copy is still sitting in Processed.
            if filed:
                log.info(
                    "Filed %d message copy/copies as '%s' after candidate deletion",
                    filed, settings.gmail_deleted_label,
                )
            else:
                log.warning(
                    "Deleted a candidate but found none of its %d message(s) on any "
                    "configured account: %s",
                    len(message_ids), where.get("rfc_message_id") or message_ids,
                )
        except Exception as err:
            log.warning("Could not re-label messages %s: %s", message_ids, err)


@app.delete("/candidates/{candidate_id}")
@app.delete("/api/v1/candidates/{candidate_id}")
def delete_candidate(
    candidate_id: str,
    background: BackgroundTasks,
    _user: dict = Depends(require_admin),
) -> dict:
    repository = repo()
    rec = repository.get(candidate_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Candidate not found")

    msg_id = rec.source_email.message_id if rec.source_email else None
    res_hash = rec.resume.sha256 if (rec.resume and rec.resume.sha256) else rec.resume_hash

    # Record privacy-safe source fingerprints first. The full candidate is still
    # hard-deleted; this tiny tombstone is what prevents Gmail or the WhatsApp
    # bot from recreating the person on their next retry.
    record_deletion = getattr(repository, "record_deletion", None)
    if callable(record_deletion):
        try:
            record_deletion(rec)
        except Exception as err:
            log.exception("Could not record deletion guard for candidate %s", candidate_id)
            raise HTTPException(
                status_code=500,
                detail=f"Candidate was not deleted because suppression could not be recorded: {err}",
            ) from err

    try:
        removed = repository.delete(candidate_id)
    except Exception as err:
        discard_deletion = getattr(repository, "discard_deletion", None)
        if callable(discard_deletion):
            try:
                discard_deletion(candidate_id)
            except Exception:  # noqa: BLE001 - retain the original delete error
                log.exception("Could not roll back candidate deletion guard %s", candidate_id)
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

    related = {
        "identity_records": 0,
        "notifications": 0,
        "sla_alerts": 0,
        "ingestion_state": 0,
        "storage_refs": [],
    }
    delete_related = getattr(repository, "delete_related", None)
    if callable(delete_related):
        try:
            related = delete_related(candidate_id)
        except Exception as err:
            log.exception("Candidate %s was deleted but related-row cleanup failed", candidate_id)
            raise HTTPException(
                status_code=500,
                detail=f"Candidate deleted, but related database cleanup failed: {err}",
            ) from err

    storage_key = rec.resume.storage_key if rec.resume else None
    storage_backend = rec.resume.storage_backend if rec.resume else None
    source = rec.source_email
    identity = {
        # `thread_id` is where the IMAP client stores the RFC822 Message-ID.
        "rfc_message_id": getattr(source, "thread_id", "") or "",
        "subject": getattr(source, "subject", "") or "",
        "from_addr": getattr(source, "from_addr", "") or "",
    } if source else {}
    background.add_task(
        _post_delete_cleanup,
        storage_key,
        message_ids,
        identity,
        storage_backend,
        related.get("storage_refs") or [],
    )

    return {
        "status": "success",
        "message": f"Candidate {candidate_id} deleted permanently",
        "cleared_entries": cleared,
        "deleted_related": {
            key: related.get(key, 0)
            for key in ("identity_records", "notifications", "sla_alerts", "ingestion_state")
        },
    }


# Single-flight guard for the inline poll. The Celery path takes a Redis lock;
# this path had none, so two overlapping requests each ran a full batch over the
# same messages. Both would miss the dedup check, one would ingest, and the
# other — finishing later — reported the candidate as an existing duplicate. The
# UI showed that second summary: "Ingested=0" for a poll that had just added a
# profile.
#
# The guard is Redis-backed, because a `threading.Lock` only ever covered one
# process: two API containers behind a load balancer both drained the same
# mailbox and both paid for the same extraction. It falls back to an in-process
# lock when Redis does not answer — see `locks.claim_inline_poll`.

# Inline cycles, run on a thread and tracked here so the request that starts one
# can hand back a task id instead of holding the socket open.
#
# Without a worker, "sync" ran the whole batch inside the POST: IMAP login, the
# attachment download, local OCR of every page, two Veris round trips and the
# LLM — nearly three minutes on one real bundle, with the browser blocked on a
# single request for all of it. The work takes as long as it takes; what it must
# not do is take that long *in front of the user*. The frontend already knows
# how to wait on a task id, so this reuses that path exactly.
#
# Bounded, because a session that syncs all afternoon must not grow it forever.
_inline_tasks: "OrderedDict[str, dict]" = OrderedDict()
_inline_tasks_lock = threading.Lock()
_INLINE_TASK_HISTORY = 32


def _inline_task_set(task_id: str, payload: dict) -> None:
    with _inline_tasks_lock:
        _inline_tasks[task_id] = payload
        _inline_tasks.move_to_end(task_id)
        while len(_inline_tasks) > _INLINE_TASK_HISTORY:
            _inline_tasks.popitem(last=False)


def _inline_task_get(task_id: str) -> dict | None:
    with _inline_tasks_lock:
        found = _inline_tasks.get(task_id)
        return dict(found) if found else None


def _collect_pending_identity_jobs() -> None:
    """Finish the extractions the batch could not wait out.

    An identity job that outlives `identity_job_wait_seconds` is not lost: it is
    left "pending" with its job id recorded, and the beat reconciler collects it
    on the next sweep. That is the whole design — except that beat runs on a
    Celery worker, and this code path exists precisely because there is no
    worker. So nothing ever swept, and a passport whose extraction had succeeded
    at the service was never written to the record.

    A real bundle showed it exactly: an eighteen-page passport submitted at
    12:45:19, given up on at 12:46:02 as "still running", and then never
    collected by anything.

    So the inline path runs the sweep itself, with a budget of its own and
    widening gaps between passes — the job is already running, and asking more
    often does not make it finish sooner.
    """
    from app.tasks.reconciler import reconcile_once

    deadline = time.monotonic() + settings.inline_reconcile_budget_seconds
    wait = max(0.0, settings.inline_reconcile_interval_seconds)
    while True:
        # Swept first, waited after. Nothing is pending on the great majority of
        # cycles, and those must not be charged a delay to discover it.
        try:
            report = reconcile_once()
        except Exception as exc:  # noqa: BLE001 — a failed sweep is not a failed batch
            log.warning("Inline reconciler sweep failed: %s", exc)
            return
        if report.get("completed") or report.get("failed") or report.get("abandoned"):
            log.info("Inline reconciler collected: %s", {
                k: v for k, v in report.items() if k != "details" and v
            })
        if not report.get("still_running"):
            return
        if time.monotonic() + wait >= deadline:
            break
        time.sleep(wait)
        wait = min(wait * 1.5, 30.0)

    log.info(
        "Identity job(s) still running after %.0fs; they keep their job id and "
        "will be collected by the next sweep",
        settings.inline_reconcile_budget_seconds,
    )


def _start_inline_poll(query: str | None) -> dict:
    """Run one cycle on a background thread and return its task id at once."""
    from app.ingestion.runner import IngestionRunner
    from app.tasks.jobs import summary_to_dict

    task_id = f"inline-{uuid.uuid4().hex}"

    from app.tasks.locks import claim_inline_poll

    claim = claim_inline_poll()
    if claim is None:
        # Already running, here or on another server. Reported as a finished
        # cycle that did nothing rather than as a failure: the frontend retries
        # a FAILURE by running the batch again inline, which is the one thing
        # that must not happen while a batch is in flight over the same messages.
        log.info("Inline poll declined: another cycle is already running")
        _inline_task_set(task_id, {
            "task_id": task_id, "state": "SUCCESS", "ready": True, "mode": "inline",
            "result": {
                "fetched": 0, "processed": 0, "skipped": 0, "suppressed": 0,
                "errors": 0, "ingested_candidates": 0, "results": [],
                "skipped_reason": "Another poll cycle is already running.",
            },
        })
        return _inline_task_get(task_id) or {}

    _inline_task_set(task_id, {
        "task_id": task_id, "state": "PENDING", "ready": False, "mode": "inline",
    })

    def _run() -> None:
        try:
            summary = summary_to_dict(IngestionRunner().run_once(query=query))
            # Before reporting the cycle done: with no worker there is no beat,
            # so this is the only thing that will ever collect an identity job
            # the batch had to leave running.
            _collect_pending_identity_jobs()
            _inline_task_set(task_id, {
                "task_id": task_id, "state": "SUCCESS", "ready": True,
                "mode": "inline", "result": summary,
            })
        except Exception as exc:  # noqa: BLE001 — reported, never raised into the thread
            log.exception("Inline poll cycle failed")
            _inline_task_set(task_id, {
                "task_id": task_id, "state": "FAILURE", "ready": True,
                "mode": "inline", "error": str(exc),
            })
        finally:
            claim.release()

    threading.Thread(target=_run, name=f"inline-poll-{task_id[-8:]}", daemon=True).start()
    return {"task_id": task_id, "state": "PENDING", "mode": "inline"}


@app.post("/ingest/poll")
def trigger_poll(query: str | None = None, _user: dict = Depends(require_admin)) -> dict:
    """Run one Gmail poll cycle inline and return its summary.

    Blocks for the whole batch (OCR + LLM per attachment), so it only suits
    small inboxes and local testing. Prefer `/ingest/poll/async` when a worker
    is running; this stays as the no-worker fallback.
    """
    from app.ingestion.runner import IngestionRunner
    from app.tasks.jobs import summary_to_dict

    from app.tasks.locks import claim_inline_poll

    claim = claim_inline_poll()
    if claim is None:
        log.info("Inline poll declined: another cycle is already running")
        return {
            "fetched": 0, "processed": 0, "skipped": 0, "suppressed": 0,
            "errors": 0, "ingested_candidates": 0, "results": [],
            "skipped_reason": "Another poll cycle is already running.",
        }

    try:
        return summary_to_dict(IngestionRunner().run_once(query=query))
    finally:
        claim.release()


def _local_ocr_report() -> dict:
    """Which OCR engines this host has, without letting a missing one 500 the page."""
    try:
        from app.extraction import local_ocr

        return dict(local_ocr.engine_report())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@app.get("/ingest/rules")
def ingest_rules(_user: dict = Depends(require_admin)) -> dict:
    """The pipeline configuration visible to administrators in Settings.

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
        # Whether anything reads the mailboxes without being asked. Worth
        # stating on the rules screen: "no résumé has appeared" means something
        # very different when the answer is "nothing has looked".
        "polling": {
            "automatic": settings.mail_autopoll_enabled,
            "interval_seconds": (
                settings.mail_poll_interval_seconds if settings.mail_autopoll_enabled else None
            ),
            "trigger": "timer" if settings.mail_autopoll_enabled else "manual sync only",
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
            "provider": "Veris",
            "min_text_chars": settings.ocr_min_text_chars,
            "max_pages": settings.ocr_max_pages,
            # Not a setting: every page is read, always. Stopping at the first
            # résumé is what left the identity documents behind it unread.
            "full_document": True,
            "provider_configured": bool(settings.veris_ocr_api_key),
            "refine_resume_pages": settings.veris_refine_resume_pages,
            # What this host can actually read with. The single most useful
            # thing to check when a scanned résumé comes back empty.
            "local": _local_ocr_report(),
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
def ocr_state(_user: dict = Depends(require_admin)) -> dict:
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

    from app.extraction import ocr_gateway

    return {
        "rows": counts,
        "in_flight": counts["received"] + counts["submitting"] + counts["running"],
        "needs_review": counts["abandoned"],
        "ocr_queue": queue,
        # This process's own view: how many jobs it has in flight, how long
        # submissions waited for a slot, and the p50/p95 of a whole extraction.
        # `queue_wait_ms` near zero with a high `total_ms` means Veris is the
        # bottleneck and raising `veris_max_inflight_jobs` will not help.
        "throughput": ocr_gateway.snapshot(),
        "config": {
            "async_jobs": settings.ocr_async_jobs_enabled,
            "multipass": settings.multipass_extraction_enabled,
            "max_attempts": settings.ocr_job_max_attempts,
            "stuck_after_seconds": settings.reconciler_stuck_after_seconds,
            "max_inflight_jobs": settings.veris_max_inflight_jobs,
            "ingestion_workers": settings.ingestion_max_workers,
            "fast_poll_seconds": settings.ocr_job_fast_poll_seconds,
        },
    }


@app.get("/ingest/ocr-state/review")
def ocr_review_queue(limit: int = 100, _user: dict = Depends(require_admin)) -> dict:
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
def candidate_identity_documents(candidate_id: str, user: dict = Depends(require_page("candidates"))) -> dict:
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
    user: dict = Depends(require_page("candidates")),
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
def ingest_workers(_user: dict = Depends(require_admin)) -> dict:
    """Lets the frontend pick the async path only when it will actually work."""
    from app.tasks.health import workers_online

    return {"available": workers_online()}


@app.post("/ingest/poll/async")
def trigger_poll_async(query: str | None = None, _user: dict = Depends(require_admin)) -> dict:
    """Queue a poll cycle on a worker, or run it here when there is no worker.

    Two shapes come back, and the caller tells them apart by which fields are
    present:

    Either way the answer is ``{"task_id": ..., "state": "PENDING"}`` and the
    caller asks ``GET /ingest/tasks/{task_id}`` until it is ready. With a worker
    the cycle runs there; without one it runs on a thread in this process, and
    ``mode: "inline"`` says which — but the client does not have to care.

    It used to run the whole batch inside this request when there was no worker
    and return the finished summary. That is why pressing Sync appeared to hang:
    the browser held one request open through IMAP, OCR, Veris and the LLM —
    close to three minutes on a thirty-page bundle. The work still takes as long
    as it takes; it no longer takes that long in front of the user.
    """
    from app.tasks.health import reset_cache, workers_online

    if not workers_online():
        log.info("No ingestion worker is running; polling inline on a background thread")
        return _start_inline_poll(query)

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
def ingest_task_status(task_id: str, _user: dict = Depends(require_admin)) -> dict:
    """Poll a queued cycle. `result` is the batch summary once state is SUCCESS.

    Inline cycles are answered from this process and never reach Celery — which
    is the point: with no worker there is no result backend to ask, and that is
    exactly when the inline path is in use.
    """
    inline = _inline_task_get(task_id)
    if inline is not None:
        return inline
    if task_id.startswith("inline-"):
        # The process that ran it has been restarted, or it aged out of the
        # ring. Either way there is no answer coming, and saying so beats
        # letting the client wait out its ten-minute deadline.
        raise HTTPException(
            status_code=404,
            detail="That poll ran in a server process that has since restarted. "
                   "Refresh to see what was ingested.",
        )

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
def update_candidate_profile(candidate_id: str, profile: CandidateProfile, _user: dict = Depends(require_admin)) -> dict:
    """Update a candidate's structured profile (e.g. to correct fields during verification)."""
    repository = repo()
    record = repository.get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    repository.update_profile(candidate_id, profile)
    updated_record = repository.get(candidate_id)
    return updated_record.model_dump(mode="json")


@app.post("/candidates/upload", status_code=201)
def create_candidate_from_uploads(
    resume: UploadFile | None = File(default=None),
    aadhaar: UploadFile | None = File(default=None),
    passport: UploadFile | None = File(default=None),
    full_name: str = Form(default=""),
    email: str = Form(default=""),
    phone: str = Form(default=""),
    job_id: str = Form(default=""),
    destination_country: str = Form(default=""),
    uploader: dict = Depends(require_page("candidate-entry")),
) -> dict:
    """Create a candidate manually, with optional VeriIS document extraction.

    Recruiter-entered job and country preferences are resolved against active
    taxonomy rows. Uploaded files remain optional and the response contains
    only curated projections, never VeriIS' raw payload.
    """
    from app.services.candidate_upload_intake import (
        CandidateUploadError,
        UploadedDocument,
        intake_uploaded_candidate,
    )

    def uploaded(file: UploadFile, fallback: str) -> UploadedDocument:
        return UploadedDocument(
            data=file.file.read(),
            filename=file.filename or fallback,
            mime_type=file.content_type or "application/octet-stream",
        )

    from app.db.taxonomy import get_job, list_countries

    chosen_job = None
    if job_id.strip():
        chosen_job = get_job(job_id.strip())
        if not chosen_job or not chosen_job.get("active", True):
            raise HTTPException(status_code=422, detail="Select an active job preference.")

    chosen_country = None
    if destination_country.strip():
        requested_country = destination_country.strip().casefold()
        chosen_country = next(
            (
                country
                for country in list_countries(active_only=True)
                if requested_country
                in {
                    str(country.get("id") or "").casefold(),
                    str(country.get("name") or "").casefold(),
                }
            ),
            None,
        )
        if not chosen_country:
            raise HTTPException(status_code=422, detail="Select an active country preference.")

    repository = repo()
    try:
        result = intake_uploaded_candidate(
            resume=uploaded(resume, "resume.pdf") if resume else None,
            aadhaar=uploaded(aadhaar, "aadhaar.jpg") if aadhaar else None,
            passport=uploaded(passport, "passport.jpg") if passport else None,
            repository=repository,
            uploader_id=str(uploader.get("id") or ""),
            full_name=full_name,
            email=email,
            phone=phone,
            job_id=str(chosen_job.get("id") or "") if chosen_job else None,
            job_title=str(chosen_job.get("title") or "") if chosen_job else None,
            destination_country=(
                str(chosen_country.get("name") or "") if chosen_country else None
            ),
        )
    except CandidateUploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    # A staff member entering a candidate owns that review. Admin uploads join
    # the normal least-loaded queue. Allocation failure cannot invalidate
    # completed OCR/storage.
    try:
        assigned_staff_id = ""
        assigned_staff_name = ""
        if uploader.get("role") == STAFF_ROLE:
            assigned_staff_id = str(uploader.get("id") or "")
            assigned_staff_name = str(
                uploader.get("name") or uploader.get("email") or "Staff"
            )
            repository.assign(
                result.candidate.id,
                assigned_staff_id,
                assigned_staff_name,
            )
        else:
            assignment = assign_candidate(
                result.candidate.id, result.candidate.profile, repo=repository
            )
            if assignment.assigned:
                assigned_staff_id = assignment.staff_id or ""
                assigned_staff_name = assignment.staff_name or ""
        if assigned_staff_id:
            notify_candidate_assigned(
                assigned_staff_id,
                {
                    "id": result.candidate.id,
                    "full_name": result.candidate.profile.full_name,
                    "email": result.candidate.profile.email,
                },
                staff_name=assigned_staff_name,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not allocate uploaded candidate %s: %s", result.candidate.id, exc)

    stored = repository.get(result.candidate.id) or result.candidate
    candidate = stored.model_dump(mode="json", exclude={"raw_ocr"})
    if isinstance(candidate.get("profile"), dict):
        candidate["profile"].pop("raw_ocr", None)
        candidate["profile"].pop("additional_info", None)
    return {
        "candidate": candidate,
        "identity": result.identity,
        "processed": [
            *(["resume"] if resume else []),
            *(["aadhaar"] if aadhaar else []),
            *(["passport"] if passport else []),
        ],
        "ocr_provider": "VeriIS" if (resume or aadhaar or passport) else "manual",
    }


@app.post("/candidates/import", status_code=201)
def import_existing_candidate(
    record_json: str = Form(...),
    resume_file: UploadFile | None = File(default=None),
    allow_missing_resume: bool = Form(default=False),
    _user: dict = Depends(require_admin),
) -> dict:
    """Import an existing candidate without paying for a second extraction.

    This is the database-migration path, not the ordinary candidate-entry
    path. The complete, already-validated record travels as JSON and the
    original resume travels as multipart bytes. The hash stored on the record
    must match those bytes, so an import cannot quietly attach the wrong CV.

    A legacy record whose file was already lost can still be preserved when an
    administrator explicitly sets ``allow_missing_resume``. Its metadata stays
    on the record and downloads correctly report the missing file; inventing a
    replacement document would be worse than an honest 404.
    """
    try:
        record = CandidateRecord.model_validate_json(record_json)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid candidate record: {exc}") from exc

    repository = repo()
    if repository.get(record.id):
        return JSONResponse(
            status_code=409,
            content={
                "detail": "A candidate with this id already exists.",
                "candidate_id": record.id,
            },
        )

    existing_hash = repository.find_by_resume_hash(record.resume_hash)
    if existing_hash:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "This resume is already attached to another candidate.",
                "candidate_id": existing_hash.id,
            },
        )

    if resume_file is not None and record.resume is None:
        raise HTTPException(status_code=422, detail="The record has no resume metadata.")
    if record.resume is not None and resume_file is None and not allow_missing_resume:
        raise HTTPException(
            status_code=422,
            detail="The original resume file is required unless allow_missing_resume is true.",
        )

    backend = None
    stored_key = None
    resume_stored = False
    if record.resume is not None:
        backend = get_storage_backend()
        filename = os.path.basename(
            (record.resume.original_filename or "resume.bin").replace("\\", "/")
        )
        stored_key = f"imports/{record.id}/{record.resume.sha256[:16]}_{filename}"
        record.resume.storage_backend = backend.name
        record.resume.storage_key = stored_key

        if resume_file is not None:
            payload = resume_file.file.read()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != record.resume.sha256 or digest != record.resume_hash:
                raise HTTPException(
                    status_code=422,
                    detail="The uploaded resume does not match the record's SHA-256 hash.",
                )
            record.resume.size = len(payload)
            backend.save(stored_key, payload, content_type=record.resume.mime_type)
            resume_stored = True

    try:
        inserted_id = repository.insert(record)
        if inserted_id != record.id:
            raise DuplicateKeyError("candidate import resolved to an existing record")
    except DuplicateKeyError as exc:
        if resume_stored and backend is not None and stored_key is not None:
            try:
                backend.delete(stored_key)
            except Exception:  # noqa: BLE001 - preserve the conflict response
                log.exception("Could not roll back imported resume %s", stored_key)
        raise HTTPException(status_code=409, detail="The candidate conflicts with an existing record.") from exc

    return {
        "status": "imported",
        "candidate_id": record.id,
        "resume_stored": resume_stored,
    }


@app.post("/admin/database/consolidate-adira")
def consolidate_legacy_database(
    confirm: str = Form(...),
    _user: dict = Depends(require_admin),
) -> dict:
    """Move the legacy ``Adira`` database into canonical ``resume_ats``.

    The confirmation phrase is deliberately specific because a successful
    verified merge drops the legacy database. The operation is idempotent: if
    a request is retried before the drop, same-id rows are replaced and unique
    rows are merged rather than duplicated.
    """
    if confirm != "MOVE_ADIRA_TO_RESUME_ATS":
        raise HTTPException(status_code=400, detail="Invalid database consolidation confirmation.")

    from app.db.consolidation import consolidate_adira_into_resume_ats
    from app.db.mongo import get_client

    result = consolidate_adira_into_resume_ats(get_client(), drop_legacy=True)
    if not result["verified"] or not result["legacy_dropped"]:
        raise HTTPException(status_code=500, detail={"message": "Database consolidation incomplete", **result})
    return result


@app.post("/candidates/{candidate_id}/verify")
def verify_candidate(candidate_id: str, _user: dict = Depends(require_admin)) -> dict:
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
def list_sourcing_clients(_user: dict = Depends(require_page("sourcing"))) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["sourcing_clients"]
    items = list(coll.find({}, {"_id": 0}))
    return {"items": items}


@app.post("/sourcing-clients")
def create_sourcing_client(client_data: dict, _user: dict = Depends(require_page("sourcing"))) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["sourcing_clients"]
    client_id = client_data.get("id")
    if client_id:
        coll.replace_one({"id": client_id}, client_data, upsert=True)
    else:
        coll.insert_one(client_data)
    return {"status": "ok", "record": client_data}


@app.delete("/sourcing-clients/{client_id}")
def delete_sourcing_client(client_id: str, _user: dict = Depends(require_page("sourcing"))) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["sourcing_clients"]
    coll.delete_one({"id": client_id})
    return {"status": "deleted", "id": client_id}


# ---- Job Orders DB Endpoints --------------------------------------------- #
@app.get("/job-orders")
def list_job_orders(_user: dict = Depends(require_page("job-orders"))) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["job_orders"]
    items = list(coll.find({}, {"_id": 0}))
    return {"items": items}


@app.post("/job-orders")
def create_job_order(order_data: dict, _user: dict = Depends(require_page("job-orders"))) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["job_orders"]
    order_id = order_data.get("id")
    if order_id:
        coll.replace_one({"id": order_id}, order_data, upsert=True)
    else:
        coll.insert_one(order_data)
    return {"status": "ok", "record": order_data}


@app.put("/job-orders/{order_id}")
def update_job_order(order_id: str, order_data: dict, _user: dict = Depends(require_page("job-orders"))) -> dict:
    from app.db.mongo import get_db
    coll = get_db()["job_orders"]
    coll.replace_one({"id": order_id}, order_data, upsert=True)
    return {"status": "updated", "record": order_data}


@app.delete("/job-orders/{order_id}")
def delete_job_order(order_id: str, _user: dict = Depends(require_page("job-orders"))) -> dict:
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
#  Visible only to accounts explicitly granted the B2B Enquiries page. An
#  enquiry carries a company's contact details and its hiring plans, so a user
#  without that page receives the same 404 as an endpoint that does not exist.
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
    _user: dict = Depends(require_page("b2b-enquiries")),
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
def create_manual_b2b_enquiry(payload: EnquiryIn, user: dict = Depends(require_page("b2b-enquiries"))) -> dict:
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
def get_b2b_enquiry(enquiry_id: str, _user: dict = Depends(require_page("b2b-enquiries"))) -> dict:
    from app.db.b2b_enquiries import get_enquiry

    doc = get_enquiry(enquiry_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    return _enquiry_json(doc)


@app.patch("/b2b-enquiries/{enquiry_id}")
def update_b2b_enquiry(
    enquiry_id: str, payload: EnquiryPatch, user: dict = Depends(require_page("b2b-enquiries"))
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
    enquiry_id: str, payload: ConvertEnquiryIn, user: dict = Depends(require_page("b2b-enquiries"))
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
def delete_b2b_enquiry(enquiry_id: str, user: dict = Depends(require_page("b2b-enquiries"))) -> dict:
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
    _admin: dict = Depends(require_page("staff")),
) -> dict:
    staff_items = users.list_staff(include_inactive=include_inactive)
    return {"count": len(staff_items), "items": [u.to_public() for u in staff_items]}


@app.get("/staff/workload")
def staff_workload(_admin: dict = Depends(require_page("staff"))) -> dict:
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
    payload: CreateStaffRequest, _admin: dict = Depends(require_page("staff"))
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
    staff_id: str, payload: UpdateStaffRequest, _admin: dict = Depends(require_page("staff"))
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
    _admin: dict = Depends(require_page("staff")),
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
    candidate_id: str, payload: AssignRequest, _admin: dict = Depends(require_page("staff"))
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
def auto_assign_candidate(candidate_id: str, _admin: dict = Depends(require_page("staff"))) -> dict:
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
def rebalance_candidates(_admin: dict = Depends(require_page("staff"))) -> dict:
    """Level untouched profiles across the roster. Reviewed work stays put."""
    result = rebalance_all()
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("detail"))
    return result


@app.post("/candidates/rehome-orphans")
def rehome_orphaned_candidates(_admin: dict = Depends(require_page("staff"))) -> dict:
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
def mark_candidate_viewed(candidate_id: str, user: dict = Depends(require_page("candidates"))) -> dict:
    _owned_or_404(candidate_id, user)
    stamped = repo().mark_viewed(candidate_id, staff_id=_staff_scope(user))
    return {"status": "ok", "candidate_id": candidate_id, "first_view": stamped}


@app.post("/candidates/{candidate_id}/evaluate")
def evaluate_candidate(
    candidate_id: str, payload: EvaluationRequest, user: dict = Depends(require_page("candidates"))
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
    _admin: dict = Depends(require_page("staff")),
) -> dict:
    items = sla_checker.list_alerts(status=None if status == "all" else status, limit=limit)
    return {"count": len(items), "items": items, "threshold_hours": settings.sla_threshold_hours}


@app.get("/sla/breaches")
def current_sla_breaches(_admin: dict = Depends(require_page("staff"))) -> dict:
    items = sla_checker.find_breaches()
    return {"count": len(items), "items": items, "threshold_hours": settings.sla_threshold_hours}


@app.post("/sla/scan")
def run_sla_scan(_admin: dict = Depends(require_page("staff"))) -> dict:
    return sla_checker.scan()


# ---- Background ingestion ------------------------------------------------- #
@app.get("/ingest/workers")
def ingest_workers(_user: dict = Depends(require_admin)) -> dict:
    from app.tasks.health import workers_online
    return {"available": workers_online()}


@app.post("/ingest/poll/async")
def trigger_poll_async(query: str | None = None, _user: dict = Depends(require_admin)) -> dict:
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
def poll_task_status(task_id: str, _user: dict = Depends(require_admin)) -> dict:
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


class WhatsAppCvIn(BaseModel):
    """The candidate profile fields extracted from an optional CV."""

    model_config = ConfigDict(extra="ignore")

    filename: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    uploaded_at: str | None = None
    extracted_at: str | None = None
    confidence: float | None = None
    needs_review: bool | None = None
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    phone_numbers: list[str] = Field(default_factory=list)
    location: str | None = None
    current_company: str | None = None
    current_designation: str | None = None
    industry: str | None = None
    resume_summary: str | None = None
    skills: list[str] = Field(default_factory=list)
    technical_skills: list[str] = Field(default_factory=list)
    trade_skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    work_experience: list[dict] = Field(default_factory=list)
    education: list[dict] = Field(default_factory=list)
    licenses: list[dict] = Field(default_factory=list)
    projects: list[dict] = Field(default_factory=list)
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    total_experience_years: float | None = None
    total_experience_band: str | None = None
    additional_info: dict | None = None
    raw_ocr: dict | None = None


class WhatsAppIdentityDocumentIn(BaseModel):
    """One extractor result; malformed documents are skipped, not fatal."""

    model_config = ConfigDict(extra="ignore")

    record_id: str = Field(default="", max_length=128)
    slot: str | None = Field(default=None, max_length=64)
    filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=128)
    sha256: str | None = Field(default=None, max_length=128)
    message_id: str | None = Field(default=None, max_length=255)
    uploaded_at: str | None = None
    extracted_at: str | None = None
    result: Any = None


class WhatsAppIdentitySectionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    aadhaar: list[WhatsAppIdentityDocumentIn] = Field(default_factory=list)
    passport: list[WhatsAppIdentityDocumentIn] = Field(default_factory=list)


class WhatsAppIdentityFileIn(BaseModel):
    filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=128)
    content_base64: str


class WhatsAppCandidateIn(BaseModel):
    source: str = "whatsapp"
    profile: WhatsAppProfileIn
    #: Stable per candidate: `whatsapp/{phone_number_id}/{wa_user_id}`. The
    #: unique index on it is what makes a retry idempotent — and, now that a
    #: registration is delivered while it is still being answered, what makes
    #: the tenth delivery fill the same record in rather than create a tenth.
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
    #: How far through the conversation the candidate is. Absent means finished:
    #: every submission that predates mid-conversation delivery was one.
    registration: RegistrationState | None = None
    #: The CV as the extractor read it. Merged into the profile, so a WhatsApp
    #: candidate's résumé shows the way an emailed one does.
    cv: WhatsAppCvIn | None = None
    #: The Aadhaar and the passport, filed in their own collections and never on
    #: the candidate document — see `_store_identity_documents`.
    identity: WhatsAppIdentitySectionIn | None = None
    #: What the conversation established about the work.
    job: JobSection | None = None


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

    # The profile the candidate answered, and then the CV they sent, in that
    # order.
    #
    # The order is the point. Both describe the same person and they disagree
    # constantly — a CV two years old names an employer the candidate has since
    # left. What the candidate typed into the bot is what they say about
    # themselves *today*, and a résumé is a document about their past, so an
    # answer is never overwritten by an extraction. What the CV supplies is
    # everything the questions never asked for: the employment history, the
    # education, the certificates.
    #
    # The overlay drops empty values as well as absent ones, and that is not a
    # nicety. `skills`, `certifications`, `languages` and `trade_skills` all
    # default to `[]` on the profile model, so an unconditional overlay would
    # have every CV's certificate list wiped out by the empty list the profile
    # carries — the data would arrive, be parsed, and be overwritten with
    # nothing in the same request.
    fields: dict = {}
    if payload.cv is not None:
        fields.update(_cv_profile_fields(payload.cv))

    for key, value in payload.profile.model_dump(exclude_none=True).items():
        if value in (None, "", [], {}):
            continue
        fields[key] = value

    profile = CandidateProfile(
        # True only when a résumé was actually read. A profile assembled from
        # tapped answers is not a parsed résumé, and saying otherwise would put
        # a confidence score on a form somebody filled in by tapping.
        is_resume=payload.cv is not None,
        confidence=payload.cv.confidence or 0.0 if payload.cv is not None else 0.0,
        **fields,
    )

    repository = repo()
    legacy_conversation = (
        payload.registration is not None or payload.job is not None or payload.cv is not None
    )

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
            registration=payload.registration,
            job=payload.job,
            identity=(
                payload.identity.model_dump(mode="python")
                if payload.identity and legacy_conversation
                else None
            ),
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
    if payload.identity is not None and not legacy_conversation:
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


def _cv_profile_fields(cv: "WhatsAppCvIn") -> dict:
    """The parts of a read CV that belong on the profile.

    A rename layer and nothing more: the bot already sends these under this
    system's own names, so what this does is drop the fields that describe the
    *file* — its name, its digest, when it was read — from the fields that
    describe the *person*. Those belong in `additional_info`, where a recruiter
    can see which document a value came off without them cluttering the profile.

    Empty values are dropped rather than sent as blanks. A CV that named no
    employers must not overwrite an employer the candidate typed in, and on this
    path an empty list would do exactly that.
    """
    ABOUT_THE_FILE = {
        "filename",
        "mime_type",
        "sha256",
        "uploaded_at",
        "extracted_at",
        "confidence",
        "needs_review",
        "industry",
        "additional_info",
    }

    values = cv.model_dump(exclude_none=True)
    fields = {
        key: value
        for key, value in values.items()
        if key not in ABOUT_THE_FILE and value not in (None, "", [], {})
    }

    # Where the CV came from, kept with whatever else had no field of its own.
    # A recruiter looking at an employment history needs to be able to ask
    # "off which document?", and this is the answer.
    extra = dict(cv.additional_info or {})
    for key in ("filename", "sha256", "uploaded_at", "extracted_at", "industry"):
        value = values.get(key)
        if value:
            extra[f"cv_{key}" if key != "industry" else "industry"] = value
    if cv.needs_review is not None:
        extra["cv_needs_review"] = cv.needs_review
    if extra:
        fields["additional_info"] = extra

    return fields


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
def list_job_designations(
    _user: dict = Depends(require_page("data-management", "candidate-entry")),
) -> dict:
    from app.db.taxonomy import list_jobs

    return {"items": list_jobs()}


@app.post("/job-designations")
def save_job_designation(payload: JobDesignationIn, user: dict = Depends(require_page("data-management"))) -> dict:
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
def retire_job_designation(job_id: str, _user: dict = Depends(require_page("data-management"))) -> dict:
    """Retire a job. It is deactivated, never erased — candidates point at it."""
    from app.db.taxonomy import delete_job

    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job designation not found")
    return {"status": "retired", "id": job_id}


@app.get("/countries")
def list_country_rows(
    _user: dict = Depends(require_page("data-management", "candidate-entry")),
) -> dict:
    from app.db.taxonomy import list_countries

    return {"items": list_countries()}


@app.post("/countries")
def save_country(payload: CountryIn, user: dict = Depends(require_page("data-management"))) -> dict:
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
def retire_country(country_id: str, _user: dict = Depends(require_page("data-management"))) -> dict:
    from app.db.taxonomy import delete_country

    if not delete_country(country_id):
        raise HTTPException(status_code=404, detail="Country not found")
    return {"status": "retired", "id": country_id}


@app.get("/job-questions")
def list_all_job_questions(
    job_id: str | None = Query(default=None), _user: dict = Depends(require_page("data-management"))
) -> dict:
    from app.db.taxonomy import list_job_questions

    return {"items": list_job_questions(job_id)}


@app.post("/job-questions")
def save_job_question(payload: JobQuestionIn, user: dict = Depends(require_page("data-management"))) -> dict:
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
def remove_job_question(question_id: str, _user: dict = Depends(require_page("data-management"))) -> dict:
    from app.db.taxonomy import delete_job_question

    if not delete_job_question(question_id):
        raise HTTPException(status_code=404, detail="Question not found")
    return {"status": "deleted", "id": question_id}


@app.get("/job-designations/{job_id}/cv-matrix")
def job_cv_matrix(job_id: str, _user: dict = Depends(require_page("data-management"))) -> dict:
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
#  A grant puts a page on someone's rail and unlocks that page's own API. It
#  does not weaken record-level isolation: Candidates remains scoped to the
#  staff member's allocated profiles.
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
def list_users(_user: dict = Depends(require_page("users"))) -> dict:
    """Every account, and the pages each one reaches."""
    from app.db.users import PAGES

    return {
        "items": [u.to_public() for u in users.list_all()],
        # The vocabulary the permission screen renders its checkboxes from, so a
        # page added to the system appears there without a frontend release.
        "pages": list(PAGES),
    }


@app.post("/users", status_code=201)
def create_user(payload: UserIn, admin: dict = Depends(require_page("users"))) -> dict:
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
def update_user(user_id: str, payload: UserPatch, admin: dict = Depends(require_page("users"))) -> dict:
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


@app.delete("/users/{user_id}")
def delete_user(user_id: str, admin: dict = Depends(require_page("users"))) -> dict:
    """Permanently remove an account from MongoDB.

    Existing bearer tokens stop working immediately because every authenticated
    request resolves its subject against the ``users`` collection. Staff-owned
    queues are redistributed using the same rules as the Staff screen.
    """
    target = users.get(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == admin.get("id"):
        raise HTTPException(status_code=409, detail="You cannot delete your own signed-in account.")
    if target.role == ADMIN_ROLE and target.active and users.count_active_admins() <= 1:
        raise HTTPException(
            status_code=409,
            detail="This is the last active administrator; promote someone else first.",
        )

    deleted = users.delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")

    outcome = (
        redistribute_from_staff(user_id, repo=repo(), users=users)
        if target.role == STAFF_ROLE
        else {"reallocated": 0, "orphaned": 0}
    )
    log.info("User %s permanently deleted by %s", target.email, admin.get("email"))
    return {
        "status": "deleted",
        "id": user_id,
        "reallocated": outcome.get("reallocated", 0),
        "orphaned": outcome.get("orphaned", 0),
    }


# Serve the static files from the Next.js export, when the build produced any.
#
# Two frontend layouts are supported and which one is in play is decided here,
# by whether the directory exists:
#
#   output: "export"      -> frontend/out, served from this process, same-origin
#   output: "standalone"  -> a Node server the frontend container runs itself,
#                            and nothing for FastAPI to serve
#
# The mount must come after every other route, so it acts as a fallback rather
# than shadowing the API.
frontend_out_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "out")
)

if os.path.exists(frontend_out_dir):
    app.mount("/", StaticFiles(directory=frontend_out_dir, html=True), name="frontend")
else:
    @app.get("/", include_in_schema=False)
    def service_root() -> dict:
        """What this process is, answered without touching anything that can be down.

        `/` was a bare 404 whenever the UI was not built into the image — which,
        under `output: "standalone"`, is always. A 404 says nothing about whether
        the API is up, where the UI went, or why ingestion is running inline, so
        each of those had to be dug out of container logs instead.

        Deliberately dependency-free: no database call, no auth, no request body.
        This is the endpoint that has to answer when the database is the thing
        that is down — `/health` counts candidates and so fails with it.
        """
        from app.tasks.health import workers_online

        try:
            # Memoised for 10s (60s when the answer is "no"), so this stays
            # cheap even if something polls it.
            queued = workers_online()
        except Exception:  # noqa: BLE001 — a broker that will not answer *is* the answer
            queued = False

        return {
            "service": app.title,
            "version": app.version,
            "status": "ok",
            "ui": "not served here — the frontend runs as its own service",
            "ingestion": (
                "queued on a Celery worker" if queued
                else "inline, in this process — no Celery worker is reachable"
            ),
            "health": "/health",
            "docs": "/docs",
        }


