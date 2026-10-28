"""Routes for matching candidates to a job order.

Everything here sits under the Job Orders grant, `require_page("job-orders")`:
the screen that shows a requisition is the screen that matches against it, and
a user without it gets the same 404 every other Job Orders route returns. The
candidate pool behind it is the one Job Orders already exposes to that grant
(`GET /job-orders/candidate-pool`), so matching reveals no candidate the
recruiter could not already open from the same screen.

    GET   /job-orders/matching/health                          admin: counters, cost
    GET   /job-orders/{id}/matching                            state of matching for an order
    POST  /job-orders/{id}/matching/preload                    run only what is not current yet
    GET   /job-orders/{id}/matching/latest-run                 the newest run, for a screen left open
    POST  /job-orders/{id}/matching/runs                       start a run
    GET   /job-orders/{id}/matching/runs/{run_id}              progress
    POST  /job-orders/{id}/matching/runs/{run_id}/cancel
    POST  /job-orders/{id}/matching/runs/{run_id}/retry
    GET   /job-orders/{id}/matching/results                    ranked, filtered, paged
    GET   /job-orders/{id}/matching/results/{candidate_id}     why this score
    POST  /job-orders/{id}/matching/results/{candidate_id}/refresh
    PUT   /job-orders/{id}/matching/results/{candidate_id}/review
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.api.routes import repo, require_admin, require_page
from app.matching import autorun, jev, runner, scoring, store
from app.matching.evidence import evidence_fingerprint, experience_years
from app.matching.prefilter import EVIDENCE_PROJECTION, retrieval_query
from app.matching.prefilter import id_values as _id_values
from app.matching.spec import normalize_job_order, spec_fingerprint

router = APIRouter(prefix="/job-orders", tags=["matching"])

_job_orders = require_page("job-orders")


class StartRunRequest(BaseModel):
    #: `rematch` is the explicit route for a closed or filled order: it runs,
    #: and every result it stores is marked historical.
    mode: Literal["match", "rematch"] = "match"
    #: Re-ask Jev even where a stored result is still valid. Costs money; the
    #: default reuses every result whose inputs have not changed.
    force: bool = False
    #: Let the free check skip clear non-matches. False is "Check with Jev":
    #: candidates it skipped are sent too.
    screen: bool = True


class ReviewRequest(BaseModel):
    status: Literal["pending", "shortlisted", "rejected", "manual_review"]
    note: str = Field(default="", max_length=1000)


def _ids_in_pool(candidate_ids: List[str]) -> set:
    """Which of these candidates are in the pool right now.

    Asked of Mongo with `retrieval_query` itself, so the screen and the runner
    share one definition of "not yet submitted" rather than two that could
    drift. A candidate scored last week and submitted this morning drops out
    of the list, the counts and the coverage at once.
    """
    if not candidate_ids:
        return set()
    rows = repo().list_summaries(
        limit=len(candidate_ids), staff_id=None, minimal=True,
        query={**retrieval_query(), "_id": {"$in": _id_values(candidate_ids)}},
    )
    return {str(row["id"]) for row in rows}


def _order_or_404(order_id: str) -> Dict[str, Any]:
    try:
        return runner.load_order(order_id)
    except runner.OrderNotFound:
        raise HTTPException(status_code=404, detail="Job order not found")


def _start(order: Dict[str, Any], user: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    try:
        run = runner.start_run(order, user, **kwargs)
    except jev.JevNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except runner.OrderInactive as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except store.RunConflict as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "run": jsonable_encoder(_public_run(exc.run) or {}),
        })
    return jsonable_encoder(run)


def _public_run(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if run:
        run = {key: value for key, value in run.items() if key not in ("_id", "active_lock")}
    return run


def _run_or_404(order_id: str, run_id: str) -> Dict[str, Any]:
    run = store.get_run(run_id)
    if not run or run.get("job_order_id") != order_id:
        raise HTTPException(status_code=404, detail="Matching run not found")
    return run


@router.get("/matching/health")
def matching_health(_user: dict = Depends(require_admin)) -> dict:
    """Jev call counts, latency and spend since this process started."""
    return jev.snapshot()


@router.get("/{order_id}/matching")
def matching_state(order_id: str, _user: dict = Depends(_job_orders)) -> dict:
    """Whether matching can run, the normalised order, and the latest run."""
    order = _order_or_404(order_id)
    spec = normalize_job_order(order)
    counts: Dict[str, int] = {key: 0 for key in scoring.CATEGORY_RANK}
    failed = 0
    entries = store.results_for_order(order_id, store.COUNT_FIELDS)
    # Counted over the pool only. Somebody scored and then submitted is no
    # longer a match to act on, and must not keep inflating "Strong matches".
    pool_ids = _ids_in_pool(store.candidate_ids(entries))
    evaluated = screened = 0
    for entry in entries:
        if entry["candidate_id"] not in pool_ids:
            continue
        result = entry["result"]
        if result:
            counts[result.get("category", scoring.LOW)] = counts.get(result.get("category"), 0) + 1
            evaluated += 1
            if result.get("status") == store.SCREENED:
                screened += 1
        elif entry["failure"]:
            failed += 1

    # "Jev checked X of Y". Y is everyone not yet submitted, as of now, and X
    # counts only candidates still among them. A result for somebody submitted
    # since does not stand in for a newcomer nobody has checked — otherwise the
    # two cancel out and the screen says "all checked" while one person was
    # never read.
    pool = repo().count(retrieval_query())
    full_run = store.latest_full_run(order_id)
    coverage = {
        "pool": pool,
        "evaluated": evaluated,
        # Of `evaluated`, decided by the free check without a Jev call.
        "screened": screened,
        "failed": failed,
        "not_evaluated": max(0, pool - evaluated - failed),
        # What a configured cap left out of the last full run. Zero by default.
        "skipped_by_cap": int((full_run or {}).get("skipped") or 0),
        "cap": (full_run or {}).get("cap"),
        "last_full_run_at": (full_run or {}).get("finished_at") or (full_run or {}).get("created_at"),
    }
    return jsonable_encoder({
        "configured": jev.is_configured(),
        "active": spec.is_active,
        "inactive_reason": None if spec.is_active else runner.inactive_reason(spec),
        "spec": spec.as_dict(),
        "config": scoring.config_for(order).as_dict(),
        "latest_run": store.latest_run(order_id),
        "counts": counts,
        "failed": failed,
        "coverage": coverage,
        "category_labels": scoring.CATEGORY_LABELS,
    })


@router.post("/{order_id}/matching/preload")
def preload_matching(order_id: str, user: dict = Depends(_job_orders)) -> dict:
    """Bring this order up to date, if it is not already.

    Called by the panel as it opens, so the recruiter never has to ask: it
    starts a run only when something in the pool is not current — a new
    candidate, a changed CV, an edited order — and sends only that. Usually
    the background sweep got there first and this returns `run: null`.
    A run already in progress is returned as it is, not refused.
    """
    order = _order_or_404(order_id)
    # Opening an order is what keeps it in the background sweep.
    store.touch(order_id, store.VIEWED)
    try:
        outcome = autorun.catch_up(order, user=user, trigger=runner.TRIGGER_OPEN)
    except jev.JevNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except store.RunConflict as exc:
        return jsonable_encoder({"pending": None, "run": _public_run(exc.run)})
    return jsonable_encoder(outcome)


@router.get("/{order_id}/matching/latest-run")
def latest_matching_run(order_id: str, _user: dict = Depends(_job_orders)) -> dict:
    """The newest run on this order, so a screen left open notices a background one."""
    _order_or_404(order_id)
    return jsonable_encoder({"run": store.latest_run(order_id)})


@router.post("/{order_id}/matching/runs", status_code=202)
def start_matching(order_id: str, payload: StartRunRequest, user: dict = Depends(_job_orders)) -> dict:
    order = _order_or_404(order_id)
    return _start(order, user, mode=payload.mode, force=payload.force, screen=payload.screen)


@router.get("/{order_id}/matching/runs/{run_id}")
def matching_run(order_id: str, run_id: str, _user: dict = Depends(_job_orders)) -> dict:
    return jsonable_encoder(_run_or_404(order_id, run_id))


@router.post("/{order_id}/matching/runs/{run_id}/cancel")
def cancel_matching(order_id: str, run_id: str, _user: dict = Depends(_job_orders)) -> dict:
    _run_or_404(order_id, run_id)
    if not store.request_cancel(run_id):
        raise HTTPException(status_code=409, detail="This run has already finished")
    return jsonable_encoder(store.get_run(run_id))


@router.post("/{order_id}/matching/runs/{run_id}/retry", status_code=202)
def retry_matching(order_id: str, run_id: str, user: dict = Depends(_job_orders)) -> dict:
    """Start again. Candidates the earlier run scored are reused, not re-sent."""
    previous = _run_or_404(order_id, run_id)
    if previous.get("status") in store.ACTIVE_RUN_STATES:
        raise HTTPException(status_code=409, detail="This run is still in progress")
    order = _order_or_404(order_id)
    return _start(order, user, mode=previous.get("mode") or runner.MODE_MATCH,
                  candidate_ids=previous.get("candidate_ids"), screen=previous.get("screen", True))


# --------------------------------------------------------------------------- #
#  Results
# --------------------------------------------------------------------------- #
def _candidate_rows(candidate_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """The live candidate rows behind a page of results, by id."""
    if not candidate_ids:
        return {}
    from app.db.repository import MATCH_PROJECTION

    # `$in` over both id forms rather than an `$or` of per-id filters: with
    # every candidate analysed, a page can be backed by thousands of results.
    rows = repo().list_summaries(
        limit=len(candidate_ids), staff_id=None,
        query={"_id": {"$in": _id_values(candidate_ids)}},
        projection=MATCH_PROJECTION, extra_projection=EVIDENCE_PROJECTION,
    )
    return {str(row.get("id")): row for row in rows}


def _eligibility(result: Optional[Dict[str, Any]]) -> str:
    if not result:
        return "not_evaluated"
    if result.get("failed_requirements"):
        return "not_met"
    if result.get("unverified_requirements"):
        return "unverified"
    if result.get("matched_requirements"):
        return "met"
    return "no_mandatory"


def _documents(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    for component in (result or {}).get("components") or []:
        if component.get("key") == "documents":
            value = component.get("value")
            if component.get("unknown") or value is None:
                band = "unknown"
            elif value >= 0.75:
                band = "ready"
            elif value >= 0.4:
                band = "partial"
            else:
                band = "not_ready"
            label = (component.get("parts") or {}).get("document_readiness") or {}
            return {"band": band, "value": value, "label": label.get("label", "")}
    return {"band": "unknown", "value": None, "label": ""}


def _row_view(
    entry: Dict[str, Any],
    candidate: Optional[Dict[str, Any]],
    review: Optional[Dict[str, Any]],
    spec_fp: str,
    required_skills: List[str],
) -> Dict[str, Any]:
    result = entry["result"]
    failure = entry["failure"]
    profile = (candidate or {}).get("profile") or {}
    evidence = (result or {}).get("evidence") or []
    supported = {item.get("requirement") for item in evidence}
    matched_skills = [skill for skill in required_skills if skill in supported]
    no_evidence = [skill for skill in required_skills if skill not in supported]

    return {
        "candidate_id": entry["candidate_id"],
        "candidate_code": (candidate or {}).get("candidate_code"),
        "name": profile.get("full_name") or (candidate or {}).get("candidate_code") or entry["candidate_id"],
        "designation": profile.get("current_designation") or profile.get("job_title") or "",
        "experience_years": experience_years(candidate or {}),
        "experience_band": profile.get("total_experience_band") or "",
        "candidate_missing": candidate is None,
        "status": (result.get("status") or store.SCORED) if result else "failed",
        "screen": (result or {}).get("screen"),
        "score": (result or {}).get("score"),
        "category": (result or {}).get("category"),
        "category_label": scoring.CATEGORY_LABELS.get((result or {}).get("category"), "Not evaluated"),
        "matched_skills": matched_skills,
        "skills_without_evidence": no_evidence if result else [],
        "unverified_requirements": (result or {}).get("unverified_requirements") or [],
        "failed_requirements": (result or {}).get("failed_requirements") or [],
        "eligibility": _eligibility(result),
        "documents": _documents(result),
        "review": {
            "status": (review or {}).get("status", "pending"),
            "note": (review or {}).get("note", ""),
            "by_name": (review or {}).get("by_name"),
            "at": (review or {}).get("at"),
        },
        "failure": None if not failure else {
            "message": (failure.get("error") or {}).get("message"),
            "retryable": (failure.get("error") or {}).get("retryable"),
            "at": failure.get("updated_at"),
        },
        "stale": {
            "order_changed": bool(result) and result.get("spec_fingerprint") != spec_fp,
            "candidate_changed": bool(result) and candidate is not None
            and result.get("evidence_fingerprint") != evidence_fingerprint(candidate),
        },
        "historical": bool((result or {}).get("historical")),
        "evaluated_at": (result or {}).get("evaluated_at"),
        "model": (result or {}).get("model"),
    }


@router.get("/{order_id}/matching/results")
def matching_results(
    order_id: str,
    category: Optional[str] = Query(default=None),
    eligibility: Optional[str] = Query(default=None),
    documents: Optional[str] = Query(default=None),
    review: Optional[str] = Query(default=None),
    skill: Optional[str] = Query(default=None),
    min_experience: Optional[float] = Query(default=None, ge=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    _user: dict = Depends(_job_orders),
) -> dict:
    """The ranked shortlist. Category first, then score — never score alone."""
    order = _order_or_404(order_id)
    spec = normalize_job_order(order)
    spec_fp = spec_fingerprint(spec)
    entries = store.results_for_order(order_id, store.LIST_FIELDS)
    # The shortlist is of people who can still be submitted. Anyone submitted
    # since they were scored leaves it; the order's own submissions are where
    # the keyword section's "Submitted" filter already shows them.
    pool_ids = _ids_in_pool(store.candidate_ids(entries))
    entries = [entry for entry in entries if entry["candidate_id"] in pool_ids]
    candidates = _candidate_rows(store.candidate_ids(entries))
    reviews = store.reviews_for_order(order_id)

    items = [
        _row_view(entry, candidates.get(entry["candidate_id"]), reviews.get(entry["candidate_id"]),
                  spec_fp, spec.required_skills)
        for entry in entries
    ]

    def keep(item: Dict[str, Any]) -> bool:
        if category and item["category"] != category:
            return False
        if eligibility and item["eligibility"] != eligibility:
            return False
        if documents and item["documents"]["band"] != documents:
            return False
        if review and item["review"]["status"] != review:
            return False
        if skill and skill.lower() not in {s.lower() for s in item["matched_skills"]}:
            return False
        if min_experience is not None and (item["experience_years"] or 0) < min_experience:
            return False
        return True

    filtered = [item for item in items if keep(item)]
    filtered.sort(key=lambda item: (
        scoring.rank_key(item["category"], item["score"] or 0) if item["status"] == "scored" else (98, 0),
        str(item["name"]).lower(),
    ))
    start = (page - 1) * page_size
    return jsonable_encoder({
        "total": len(filtered),
        "unfiltered_total": len(items),
        "page": page,
        "page_size": page_size,
        "items": filtered[start:start + page_size],
        "category_labels": scoring.CATEGORY_LABELS,
    })


@router.get("/{order_id}/matching/results/{candidate_id}")
def matching_explanation(order_id: str, candidate_id: str, _user: dict = Depends(_job_orders)) -> dict:
    """Everything behind one score: components, verdicts, excerpts, baseline."""
    order = _order_or_404(order_id)
    spec = normalize_job_order(order)
    entries = store.results_for_order(order_id, candidate_id=candidate_id)
    if not entries:
        raise HTTPException(status_code=404, detail="This candidate has not been matched to this order")
    entry = entries[0]
    candidate = _candidate_rows([candidate_id]).get(candidate_id)
    review = store.reviews_for_order(order_id).get(candidate_id)
    view = _row_view(entry, candidate, review, spec_fingerprint(spec), spec.required_skills)
    result = entry["result"] or {}
    return jsonable_encoder({
        **view,
        "score_label": "Matching score (0–100): how well the evidence on file meets this order. "
                       "Not a probability of being hired.",
        "components": result.get("components") or [],
        "requirements": (result.get("evaluation") or {}).get("requirements") or [],
        "dimensions": (result.get("evaluation") or {}).get("dimensions") or {},
        "signals": {
            "equivalent_title": (result.get("evaluation") or {}).get("equivalent_title"),
            "conflicting_evidence": (result.get("evaluation") or {}).get("conflicting_evidence"),
        },
        "reasons": result.get("reasons") or [],
        "evidence": result.get("evidence") or [],
        "baseline": result.get("baseline") or (entry["failure"] or {}).get("baseline"),
        "spec_requirements": [item.as_dict() for item in spec.requirements],
        "versions": {
            "scoring": result.get("scoring_version"),
            "schema": result.get("schema_version"),
            "model": result.get("model"),
            "model_requested": result.get("model_requested"),
        },
        "usage": result.get("usage"),
        "review_history": (review or {}).get("history") or [],
    })


@router.post("/{order_id}/matching/results/{candidate_id}/refresh", status_code=202)
def refresh_match(
    order_id: str,
    candidate_id: str,
    force: bool = Query(default=False),
    user: dict = Depends(_job_orders),
) -> dict:
    """Re-evaluate one candidate. A no-op cache hit if nothing it depends on changed.

    `force` re-asks Jev, and also sends a candidate the free check skipped.
    """
    order = _order_or_404(order_id)
    if candidate_id not in _ids_in_pool([candidate_id]):
        raise HTTPException(
            status_code=409,
            detail="This candidate has already been submitted, so they are not analysed again.",
        )
    spec = normalize_job_order(order)
    return _start(order, user, mode=runner.MODE_REMATCH if not spec.is_active else runner.MODE_REFRESH,
                  candidate_ids=[candidate_id], force=force, screen=not force)


@router.put("/{order_id}/matching/results/{candidate_id}/review")
def review_match(order_id: str, candidate_id: str, payload: ReviewRequest, user: dict = Depends(_job_orders)) -> dict:
    """A recruiter decision. Kept apart from the score, and kept across re-scoring."""
    _order_or_404(order_id)
    if not store.result_for(order_id, candidate_id) and not any(
        entry["candidate_id"] == candidate_id
        for entry in store.results_for_order(order_id, store.COUNT_FIELDS, candidate_id=candidate_id)
    ):
        raise HTTPException(status_code=404, detail="This candidate has not been matched to this order")
    return jsonable_encoder(store.save_review(order_id, candidate_id, payload.status, payload.note.strip(), user))
