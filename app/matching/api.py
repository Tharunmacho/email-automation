"""Routes for matching candidates to a job order.

Everything here sits under the Job Orders grant, `require_page("job-orders")`:
the screen that shows a requisition is the screen that matches against it, and
a user without it gets the same 404 every other Job Orders route returns. The
pool is everyone not yet submitted (`pool.retrieval_query`), which Job Orders
already exposes to that grant, so matching reveals no candidate the recruiter
could not already open from the same screen.

    GET   /job-orders/{id}/matching                            counts and the normalised order
    GET   /job-orders/{id}/matching/results                    ranked, filtered, paged
    GET   /job-orders/{id}/matching/results/{candidate_id}     why this score
    PUT   /job-orders/{id}/matching/results/{candidate_id}/review

Nothing to start and nothing to wait for: the ranking is computed on request
(`matcher.rank`), from the live records, in memory.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.api.routes import require_page
from app.matching import matcher, scoring, store
from app.matching.evidence import experience_years
from app.matching.spec import normalize_job_order

router = APIRouter(prefix="/job-orders", tags=["matching"])

_job_orders = require_page("job-orders")

SCORE_LABEL = ("Matching score (0–100): how well the evidence on file meets this order. "
               "Not a probability of being hired.")


class ReviewRequest(BaseModel):
    status: Literal["pending", "shortlisted", "rejected", "manual_review"]
    note: str = Field(default="", max_length=1000)


def _order_or_404(order_id: str) -> Dict[str, Any]:
    from app.db.mongo import get_db

    order = get_db()["job_orders"].find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Job order not found")
    return order


def _inactive_reason(order: Dict[str, Any]) -> Optional[str]:
    spec = normalize_job_order(order)
    if spec.is_active:
        return None
    return (f"Job order {spec.order_id} is {spec.status.lower()}. The ranking is shown for "
            f"reference; it is not a live shortlist.")


def _eligibility(match: matcher.Match) -> str:
    outcome = match.outcome
    if outcome.failed:
        return "not_met"
    if outcome.unverified:
        return "unverified"
    if outcome.matched:
        return "met"
    return "no_mandatory"


def _row_view(match: matcher.Match, review: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    row = match.row
    profile = row.get("profile") or {}
    outcome = match.outcome
    return {
        "candidate_id": match.candidate_id,
        "candidate_code": row.get("candidate_code"),
        "name": profile.get("full_name") or row.get("candidate_code") or match.candidate_id,
        "designation": profile.get("current_designation") or profile.get("job_title") or "",
        "experience_years": experience_years(row),
        "experience_band": profile.get("total_experience_band") or "",
        "score": outcome.score,
        "category": outcome.category,
        "category_label": scoring.CATEGORY_LABELS[outcome.category],
        "matched_skills": list(match.matched_skills),
        "related_skills": list(match.related_skills),
        "skills_without_evidence": list(match.missing_skills),
        "unverified_requirements": outcome.unverified,
        "failed_requirements": outcome.failed,
        "eligibility": _eligibility(match),
        "documents": dict(match.documents),
        "review": {
            "status": (review or {}).get("status", "pending"),
            "note": (review or {}).get("note", ""),
            "by_name": (review or {}).get("by_name"),
            "at": (review or {}).get("at"),
        },
    }


@router.get("/{order_id}/matching")
def matching_state(order_id: str, fresh: bool = Query(default=False),
                   _user: dict = Depends(_job_orders)) -> dict:
    """The normalised order and the category counts. `fresh` re-reads the pool now."""
    order = _order_or_404(order_id)
    ranking = matcher.rank(order, fresh=fresh)
    counts: Dict[str, int] = {key: 0 for key in scoring.CATEGORY_RANK}
    for match in ranking.matches:
        counts[match.category] += 1
    return jsonable_encoder({
        "active": ranking.spec.is_active,
        "inactive_reason": _inactive_reason(order),
        "spec": ranking.spec.as_dict(),
        "config": ranking.config.as_dict(),
        "pool": ranking.pool,
        "counts": counts,
        "category_labels": scoring.CATEGORY_LABELS,
    })


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
    ranking = matcher.rank(order)
    reviews = store.reviews_for_order(order_id)

    def keep(match: matcher.Match) -> bool:
        if category and match.category != category:
            return False
        if eligibility and _eligibility(match) != eligibility:
            return False
        if documents and match.documents["band"] != documents:
            return False
        if review and (reviews.get(match.candidate_id) or {}).get("status", "pending") != review:
            return False
        if skill and skill.lower() not in {s.lower() for s in match.matched_skills}:
            return False
        if min_experience is not None and (experience_years(match.row) or 0) < min_experience:
            return False
        return True

    filtered: List[matcher.Match] = [match for match in ranking.matches if keep(match)]
    start = (page - 1) * page_size
    return jsonable_encoder({
        "total": len(filtered),
        "unfiltered_total": len(ranking.matches),
        "page": page,
        "page_size": page_size,
        "items": [_row_view(match, reviews.get(match.candidate_id))
                  for match in filtered[start:start + page_size]],
        "category_labels": scoring.CATEGORY_LABELS,
    })


def _match_or_404(order: Dict[str, Any], candidate_id: str) -> tuple:
    ranking = matcher.rank(order)
    match = ranking.by_id.get(candidate_id)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail="This candidate is not in the matching pool — already submitted, placed, or not found.",
        )
    return ranking, match


@router.get("/{order_id}/matching/results/{candidate_id}")
def matching_explanation(order_id: str, candidate_id: str, _user: dict = Depends(_job_orders)) -> dict:
    """Everything behind one score: components, mandatory checks, evidence."""
    order = _order_or_404(order_id)
    ranking, match = _match_or_404(order, candidate_id)
    review = store.review_for(order_id, candidate_id)
    return jsonable_encoder({
        **_row_view(match, review),
        "score_label": SCORE_LABEL,
        "components": [component.as_dict() for component in match.outcome.components],
        "requirements": [verdict.as_dict() for verdict in match.outcome.verdicts],
        "reasons": match.outcome.reasons,
        "evidence": matcher.supporting_evidence(ranking.query, match),
        "spec_requirements": [item.as_dict() for item in ranking.spec.requirements],
        "scoring_version": ranking.config.version,
        "review_history": (review or {}).get("history") or [],
    })


@router.put("/{order_id}/matching/results/{candidate_id}/review")
def review_match(order_id: str, candidate_id: str, payload: ReviewRequest, user: dict = Depends(_job_orders)) -> dict:
    """A recruiter decision. Kept apart from the score, and kept across re-ranking."""
    order = _order_or_404(order_id)
    _match_or_404(order, candidate_id)
    return jsonable_encoder(store.save_review(order_id, candidate_id, payload.status, payload.note.strip(), user))
