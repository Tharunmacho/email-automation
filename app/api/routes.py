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


def current_user(authorization: str | None = Header(default=None)) -> dict:
    """Resolve `Authorization: Bearer <token>` into a user, or 401."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    subject = read_token(token, settings.auth_secret)
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
    if not record:
        raise HTTPException(status_code=404, detail="Candidate not found")
    data = get_storage_backend().load(record.resume.storage_key)
    
    original_name = record.resume.original_filename or "resume.pdf"
    safe_filename = original_name.replace('"', '').replace("'", "")
    encoded_filename = urllib.parse.quote(safe_filename)
    
    return Response(
        content=data,
        media_type=record.resume.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"; filename*=UTF-8\'\'{encoded_filename}'
        },
    )


@app.delete("/api/v1/candidates/{candidate_id}")
def delete_candidate(candidate_id: str, _user: dict = Depends(current_user)) -> dict:
    rec = repo.get(candidate_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if rec.resume and rec.resume.storage_key:
        try:
            storage.delete(rec.resume.storage_key)
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

    success = repo.delete(candidate_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete candidate")

    return {
        "status": "success",
        "message": f"Candidate {candidate_id} deleted permanently",
        "suppressed_entries": suppressed,
    }


@app.post("/ingest/poll")
def trigger_poll(query: str | None = None, _user: dict = Depends(current_user)) -> dict:
    """Manually trigger one Gmail poll cycle (handy for testing / on-demand runs)."""
    from app.ingestion.runner import IngestionRunner

    summary = IngestionRunner().run_once(query=query)
    return {
        "fetched": summary.fetched,
        "processed": summary.processed,
        "skipped": summary.skipped,
        "errors": summary.errors,
        "ingested_candidates": summary.ingested_candidates,
        "results": [
            {
                "message_id": r.message_id,
                "status": r.status,
                "reason": r.reason,
                "attachments": [
                    {
                        "filename": a.filename,
                        "status": a.status,
                        "candidate_id": a.candidate_id,
                        "detail": a.detail,
                    }
                    for a in r.attachments
                ],
            }
            for r in summary.results
        ],
    }


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

