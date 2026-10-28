"""Which candidates are worth paying Jev to read, and the rule-based baseline.

Two stages, both cheap, both before any network call:

**Retrieval** is one indexed Mongo query over the candidate collection with a
projection — the same `list_summaries` path Job Orders already uses for its
pool, plus the handful of fields matching reads that a list row does not
carry. The query removes only what is excluded by an explicit rule:

    placement_locked      already placed; the CRM will not move them
    is_resume == False    the extraction pipeline judged it not a CV
    duplicate_of          a second copy of somebody already in the pool
    mapped elsewhere      out with a client on a different order right now

Nothing is excluded for using different words. That is the next stage's job,
and it does not exclude either: it ranks.

**The baseline** is a transparent keyword score — role terms and skills found
in the candidate's own evidence, plus whether a stated experience floor is
met. It decides the *order* in which candidates are sent and, under the
per-run cap, which ones are sent at all. It is also the comparison point the
brief asks for: before Jev's ranking is trusted to drive anything, it has to
beat this.

No vector index is added. At the pool sizes this CRM holds — low thousands —
a projected scan and an in-process score take well under a second, and Jev
reads every candidate the cap admits regardless. A vector store would be a new
moving part bought to shave time off the cheapest stage of the pipeline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from app.matching.evidence import experience_years
from app.matching.spec import MatchSpec

#: What matching reads beyond the job-order matcher projection. Passed as
#: `extra_projection`, so the shared list row stays as narrow as it is for
#: every other caller.
EVIDENCE_PROJECTION = {
    "profile.trade_skills": 1,
    "profile.certifications": 1,
    "profile.licenses.name": 1,
    "profile.licenses.issuer": 1,
    "profile.licenses.expiry_date": 1,
    "profile.education": 1,
    "profile.course_or_trade": 1,
    "profile.country": 1,
    "profile.destination_country": 1,
    "profile.destination_countries": 1,
    "profile.available_from": 1,
    "profile.total_experience_band": 1,
    "profile.passport_expiry": 1,
    # Projected only so readiness can say "passport on file". The number is
    # dropped by `evidence.candidate_state` and never leaves the process.
    "profile.passport_number": 1,
    "registration_state.outstanding_documents": 1,
    "profile.is_resume": 1,
}

#: Scanning is paged so a large collection is never one cursor held open for
#: the length of a run.
PAGE_SIZE = 1000
#: A hard ceiling on what one run will scan. Far above any pool this CRM has;
#: here so a runaway query has an end.
MAX_SCAN = 50_000

_STOP = frozenset({
    "and", "the", "for", "with", "of", "in", "a", "an", "to", "senior", "junior",
    "assistant", "helper", "staff", "worker", "general", "cum", "or",
})
_TOKEN = re.compile(r"[a-z0-9+#]+")


def _tokens(text: str) -> Set[str]:
    return {token for token in _TOKEN.findall(text.lower()) if len(token) > 2 and token not in _STOP}


def retrieval_query(spec: MatchSpec) -> Dict[str, Any]:
    """The Mongo filter for candidates who may be considered at all."""
    return {
        "placement_locked": {"$ne": True},
        "profile.is_resume": {"$ne": False},
        "duplicate_of": {"$in": [None, ""]},
        "$or": [
            # In the pool, or already mapped to *this* order — re-scoring the
            # people already submitted is how the screen shows their fit too.
            {"recruitment_status": {"$in": [None, "available"]}},
            {"job_order_id": {"$in": [None, "", spec.order_id]}},
        ],
    }


def retrieve(repository: Any, spec: MatchSpec) -> List[Dict[str, Any]]:
    """Every candidate the explicit rules admit, as projected rows."""
    from app.db.repository import MATCH_PROJECTION

    rows: List[Dict[str, Any]] = []
    skip = 0
    query = retrieval_query(spec)
    while skip < MAX_SCAN:
        page = repository.list_summaries(
            limit=PAGE_SIZE, skip=skip, staff_id=None, query=query,
            projection=MATCH_PROJECTION, extra_projection=EVIDENCE_PROJECTION,
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return rows


def _evidence_text(row: Dict[str, Any]) -> str:
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    parts: List[str] = []
    for key in ("current_designation", "job_title", "job_preference", "job_category",
                "course_or_trade", "resume_summary"):
        if profile.get(key):
            parts.append(str(profile[key]))
    for key in ("skills", "technical_skills", "trade_skills", "certifications"):
        values = profile.get(key)
        if isinstance(values, list):
            parts.extend(str(value) for value in values if value)
    for role in profile.get("work_experience") or []:
        if isinstance(role, dict):
            parts.extend(str(role.get(key) or "") for key in ("designation", "title", "description"))
    return " ".join(parts)


@dataclass(frozen=True)
class Baseline:
    """The rule-based reading of one candidate against one order."""

    candidate_id: str
    score: float           # 0-100
    role_hit: bool
    skills_found: List[str]
    skills_missing: List[str]
    experience_met: Optional[bool]  # None: no floor, or no figure on file

    def as_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score, "role_hit": self.role_hit,
            "skills_found": list(self.skills_found), "skills_missing": list(self.skills_missing),
            "experience_met": self.experience_met,
        }


def baseline(spec: MatchSpec, row: Dict[str, Any]) -> Baseline:
    """Score one candidate on keywords alone.

    The weights mirror the browser-side matcher in `JobOrders.tsx` (skills 45,
    role 35, experience 20) so the two baselines a recruiter could compare
    agree in spirit, but this one does not zero a candidate for a role
    mismatch — terminology is what Jev is for, and the baseline only orders
    the queue.
    """
    evidence = _tokens(_evidence_text(row))

    role_tokens: Set[str] = set()
    for term in spec.role_terms:
        role_tokens |= _tokens(term)
    role_hit = bool(role_tokens & evidence)

    found: List[str] = []
    missing: List[str] = []
    for skill in spec.required_skills:
        skill_tokens = _tokens(skill)
        if skill_tokens and skill_tokens <= evidence:
            found.append(skill)
        elif skill_tokens and len(skill_tokens & evidence) * 2 >= len(skill_tokens):
            found.append(skill)
        else:
            missing.append(skill)
    skill_ratio = len(found) / len(spec.required_skills) if spec.required_skills else (1.0 if role_hit else 0.0)

    years = experience_years(row)
    experience_met: Optional[bool] = None
    experience_part = 0.5  # no floor or no figure: neither credit nor penalty
    if spec.min_experience_years is not None and years is not None:
        experience_met = years >= spec.min_experience_years
        experience_part = 1.0 if experience_met else max(0.0, years / spec.min_experience_years)
    elif spec.min_experience_years is None and years is not None:
        experience_part = 1.0

    value = 45.0 * skill_ratio + 35.0 * (1.0 if role_hit else 0.0) + 20.0 * experience_part
    return Baseline(
        candidate_id=str(row.get("id") or ""),
        score=round(value, 1),
        role_hit=role_hit,
        skills_found=found,
        skills_missing=missing,
        experience_met=experience_met,
    )


def shortlist_for_jev(
    spec: MatchSpec, rows: Iterable[Dict[str, Any]], cap: int,
) -> List[tuple[Dict[str, Any], Baseline]]:
    """The candidates to send, best baseline first, at most `cap` of them.

    Ties broken by candidate id so two runs over the same pool send the same
    people in the same order — which is what makes a cancelled run resumable.
    """
    scored = [(row, baseline(spec, row)) for row in rows if row.get("id")]
    scored.sort(key=lambda pair: (-pair[1].score, str(pair[0].get("id"))))
    return scored[: max(0, cap)]
