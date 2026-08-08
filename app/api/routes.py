"""FastAPI application — REST seam for the future recruiter dashboard / frontend.

Deliberately minimal for now: health, candidate list/detail, resume download, and
a manual poll trigger. Search, filtering, ranking, JD-matching, scoring, and auth
all slot in here later without touching the ingestion pipeline.

Run:
    uvicorn app.api.routes:app --reload --port 8000
"""
from __future__ import annotations

import os
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from app.config import settings
from app.core.models import CandidateProfile
from app.core.security import create_token, read_token
from app.db.mongo import ensure_indexes
from app.db.repository import CandidateRepository
from app.db.users import UserRepository, ensure_seed_user
from app.storage.factory import get_storage_backend

from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
from fastapi.requests import Request
from fastapi.responses import JSONResponse

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
    _user: dict = Depends(current_user),
) -> dict:
    records = repo().list_candidates(limit=limit, skip=skip)
    return {
        "total": repo().count(),
        "count": len(records),
        "items": [r.model_dump(mode="json") for r in records],
    }


@app.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, _user: dict = Depends(current_user)) -> dict:
    record = repo().get(candidate_id)
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return record.model_dump(mode="json")


@app.get("/candidates/{candidate_id}/resume")
def download_resume(candidate_id: str, _user: dict = Depends(current_user)) -> Response:
    import urllib.parse
    record = repo().get(candidate_id)
    if not record or not record.resume or not record.resume.storage_key:
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


@app.delete("/api/v1/candidates/{candidate_id}")
def delete_candidate(candidate_id: str, _user: dict = Depends(current_user)) -> dict:
    rec = repo().get(candidate_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if rec.resume and rec.resume.storage_key:
        try:
            get_storage_backend().delete(rec.resume.storage_key)
        except Exception:
            pass

    # Tombstone BEFORE deleting. If we removed the candidate first and the
    # suppression then failed, the next Gmail poll would silently re-ingest the
    # profile the user just deleted.
    from app.db.ledger import IngestLedger

    ledger = IngestLedger()
    suppressed = ledger.suppress_candidate(candidate_id)
    if suppressed == 0 and rec.resume_hash:
        # Ingested before the ledger existed — suppress by file hash instead.
        ledger.suppress_hash(rec.resume_hash)
        suppressed = 1

    success = repo().delete(candidate_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete candidate")

    return {
        "status": "success",
        "message": f"Candidate {candidate_id} deleted permanently",
        "suppressed_entries": suppressed,
    }


@app.post("/ingest/poll")
def trigger_poll(query: str | None = None, _user: dict = Depends(current_user)) -> dict:
    """Run one Gmail poll cycle inline and return its summary.

    Blocks for the whole batch (OCR + LLM per attachment), so it only suits
    small inboxes and local testing. Prefer `/ingest/poll/async` when a worker
    is running; this stays as the no-worker fallback.
    """
    from app.ingestion.runner import IngestionRunner
    from app.tasks.jobs import summary_to_dict

    return summary_to_dict(IngestionRunner().run_once(query=query))


# ---- Background ingestion ------------------------------------------------- #
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
                   "celery -A app.tasks.celery_app worker --loglevel=INFO --pool=solo",
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
            err_str = f"Task {err_str} is not registered on Celery worker. Restart worker with: celery -A app.tasks.celery_app worker --loglevel=INFO --pool=solo"
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


# Serve the static files from the Next.js export.
# This must be mounted AFTER all other routes so it acts as a fallback.
frontend_out_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "out")
)
if os.path.exists(frontend_out_dir):
    app.mount("/", StaticFiles(directory=frontend_out_dir, html=True), name="frontend")

