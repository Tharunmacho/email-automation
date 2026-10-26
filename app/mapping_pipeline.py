"""Mapping Candidates: the steps a candidate walks once mapped to a job order.

    resume -> interview -> selection -> offer -> visa -> travel

A candidate is *mapped* when Job Orders submits them against an order — the
candidate record carries that order's `job_order_id` while it stands. From
there each step is a question a staff member answers, and the steps are gated:
a step opens only once the one before it is done. Managers and admins may skip
a step to open the next one; staff may not.

This module is the whole rule, kept apart from the HTTP layer so the console
and the API cannot disagree about it: `build_view` computes what the console
draws, and `check_answerable` is what the route enforces. Both read the same
document shape, a list row or `CandidateRecord.model_dump()`.

What is stored, on the candidate as `mapping_pipeline`:

    {
      "job_order_id": "JO-9",
      "stages": {"resume": {"answer": "submitted", "at": ..., ...}, ...},
      "closed_at": None, "closed_stage": None, "closed_reason": None,
    }

Three of the steps — interview, selection and offer — are facts the
recruitment pipeline already records (`interview_status`, `last_outcome`,
`placement_locked`). The route writes those through the same transitions as
before, and where a step has no answer of its own here, the view reads it from
those fields instead. Recording an interview from the candidate profile
therefore shows up here, and the two screens cannot contradict each other.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

STAGES = ("resume", "interview", "selection", "offer", "visa", "travel")

STAGE_LABELS = {
    "resume": "Resume submitted",
    "interview": "Interview scheduled",
    "selection": "Selection",
    "offer": "Offer",
    "visa": "Visa process",
    "travel": "Travel",
}

#: The answers each single-question step accepts. The visa step asks two
#: questions and is listed separately below.
ANSWERS = {
    "resume": ("submitted", "not_submitted"),
    "interview": ("scheduled", "not_scheduled"),
    "selection": ("selected", "rejected"),
    "offer": ("accepted", "rejected"),
    "travel": ("travelled", "not_travelled"),
}

#: The answer that completes each single-question step and opens the next one.
PASSING_ANSWER = {
    "resume": "submitted",
    "interview": "scheduled",
    "selection": "selected",
    "offer": "accepted",
    "travel": "travelled",
}

VISA_STAMPING = ("completed", "pending")
VISA_MEDICAL = ("fit", "unfit", "pending")

#: Answers that end the mapping. The candidate goes back to the available pool
#: (the recruitment pipeline's own rule), and the mapping stays on screen as
#: closed so the job order still shows who was put forward and what happened.
CLOSING_ANSWERS = {
    ("selection", "rejected"): "rejected",
    ("offer", "rejected"): "offer_rejected",
}

#: Roles that may skip a step. A skip opens the next step without recording an
#: answer, which is a judgement about the process rather than a fact about the
#: candidate — so it is not a staff member's to make.
SKIP_ROLES = ("admin", "manager", "finance_manager")

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_FLIGHT = re.compile(r"^[A-Z0-9][A-Z0-9 -]{1,19}$")


class MappingError(Exception):
    """A refused step change, with the HTTP status the route should answer."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --------------------------------------------------------------------------- #
#  Reading
# --------------------------------------------------------------------------- #
def mapped_job_order(doc: dict) -> Optional[str]:
    """The job order this candidate is mapped to right now, if any.

    The same rule as `_mapped_job_order` in the routes: a `job_order_id` counts
    while the candidate is out with the client, and stops counting once they
    are back in the pool.
    """
    job_order_id = doc.get("job_order_id")
    if job_order_id and (doc.get("recruitment_status") or "available") != "available":
        return job_order_id
    return None


def row_job_order(doc: dict) -> Optional[str]:
    """The job order a Mapping Candidates row is about.

    The live mapping when there is one; otherwise the order a closed mapping
    was against, so a rejected candidate still appears under the order they
    were rejected from.
    """
    live = mapped_job_order(doc)
    if live:
        return live
    pipeline = doc.get("mapping_pipeline") or {}
    if pipeline.get("closed_at") and pipeline.get("job_order_id"):
        return pipeline["job_order_id"]
    return None


def current_pipeline(doc: dict) -> dict:
    """The stored pipeline, if it belongs to the order this row is about.

    A pipeline left over from an earlier order is somebody else's answers, so
    it reads as empty rather than leaking "resume submitted" into a new order.
    """
    pipeline = doc.get("mapping_pipeline") or {}
    job_order_id = row_job_order(doc)
    if job_order_id and pipeline.get("job_order_id") == job_order_id:
        return pipeline
    return {}


def _derived_entry(stage: str, doc: dict) -> Optional[dict]:
    """A step's answer as the recruitment pipeline already records it."""
    if not mapped_job_order(doc):
        return None
    if stage == "interview" and doc.get("interview_status") in ("scheduled", "completed"):
        return {"answer": "scheduled", "interview_at": doc.get("interview_at"), "derived": True}
    if stage == "selection" and doc.get("recruitment_status") in (
        "selected", "offer_issued", "offer_accepted",
    ):
        return {"answer": "selected", "derived": True}
    if stage == "offer" and (doc.get("placement_locked") or doc.get("offer_status") == "accepted"):
        return {"answer": "accepted", "derived": True}
    return None


def stage_passed(stage: str, entry: Optional[dict]) -> bool:
    """Whether this answer completes the step, opening the next one."""
    if not entry:
        return False
    if entry.get("skipped"):
        return True
    if stage == "visa":
        return entry.get("stamping") == "completed" and entry.get("medical") == "fit"
    return entry.get("answer") == PASSING_ANSWER[stage]


def _entries(doc: dict) -> tuple[dict, dict]:
    """(explicit answers, effective answers) for every step."""
    explicit = dict(current_pipeline(doc).get("stages") or {})
    effective = {}
    for stage in STAGES:
        entry = explicit.get(stage) or _derived_entry(stage, doc)
        if entry:
            effective[stage] = entry
    return explicit, effective


def _gate_refusal(doc: dict, stage: str) -> Optional[str]:
    """Why `stage` cannot be answered right now, or None if it can."""
    pipeline = current_pipeline(doc)
    if pipeline.get("closed_at"):
        closed = STAGE_LABELS.get(pipeline.get("closed_stage") or "", "an earlier step")
        return (
            f"This mapping was closed at {closed}. Map the candidate to a job order "
            "again to start a new pipeline."
        )
    if not mapped_job_order(doc):
        return "This candidate is not mapped to a job order."
    explicit, effective = _entries(doc)
    index = STAGES.index(stage)
    for earlier in STAGES[:index]:
        if not stage_passed(earlier, effective.get(earlier)):
            return (
                f"Complete “{STAGE_LABELS[earlier]}” first — each step opens once the "
                "one before it is done."
            )
    for later in STAGES[index + 1:]:
        if later in explicit:
            return (
                f"“{STAGE_LABELS[later]}” has already been recorded, so "
                f"“{STAGE_LABELS[stage]}” can no longer be changed."
            )
    return None


def build_view(doc: dict) -> dict:
    """Everything the console draws for one mapped candidate.

    `state` per step is one of:

        passed   answered with the answer that opens the next step
        skipped  opened past by a manager or admin without an answer
        current  the step being worked; may hold a partial answer
                 ("not submitted yet", "medically unfit")
        failed   the answer that closed the mapping
        locked   waiting on an earlier step
    """
    pipeline = current_pipeline(doc)
    explicit, effective = _entries(doc)
    closed_stage = pipeline.get("closed_stage") if pipeline.get("closed_at") else None

    current = next((s for s in STAGES if not stage_passed(s, effective.get(s))), None)
    stages = []
    for stage in STAGES:
        entry = effective.get(stage)
        if closed_stage == stage:
            state = "failed"
        elif stage_passed(stage, entry):
            state = "skipped" if entry.get("skipped") else "passed"
        elif stage == current and not closed_stage:
            state = "current"
        else:
            state = "locked"
        stages.append({
            "key": stage,
            "label": STAGE_LABELS[stage],
            "state": state,
            "entry": entry,
            "answerable": _gate_refusal(doc, stage) is None,
        })

    if closed_stage:
        status = "closed"
    elif current is None:
        status = "completed"
    else:
        status = "in_progress"

    return {
        "job_order_id": row_job_order(doc),
        "status": status,
        "current_stage": None if status != "in_progress" else current,
        "closed": (
            {
                "stage": closed_stage,
                "reason": pipeline.get("closed_reason"),
                "at": pipeline.get("closed_at"),
                "by": pipeline.get("closed_by"),
            }
            if closed_stage else None
        ),
        "stages": stages,
    }


# --------------------------------------------------------------------------- #
#  Writing
# --------------------------------------------------------------------------- #
def check_answerable(doc: dict, stage: str, *, skip: bool, role: Optional[str]) -> None:
    """Raise `MappingError` unless this account may record `stage` now."""
    if stage not in STAGES:
        raise MappingError(404, f"Unknown step '{stage}'.")
    if skip and role not in SKIP_ROLES:
        raise MappingError(403, "Only managers and admins can skip a step.")
    refusal = _gate_refusal(doc, stage)
    if refusal:
        raise MappingError(409, refusal)
    if skip and stage_passed(stage, _entries(doc)[1].get(stage)):
        raise MappingError(409, f"“{STAGE_LABELS[stage]}” is already complete.")


def _valid_date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value or "")).isoformat()
    except ValueError:
        raise MappingError(400, "Choose the travel date.") from None


def build_entry(stage: str, payload: dict) -> dict:
    """The stored answer for `stage`, validated; actor and time added by the caller.

    `payload` is the request body as a plain dict. Only the fields that belong
    to the step are kept, so a travel date posted against the interview step is
    dropped rather than stored somewhere nobody reads it.
    """
    notes = (payload.get("notes") or "").strip()
    if stage == "visa":
        stamping, medical = payload.get("stamping"), payload.get("medical")
        if stamping not in VISA_STAMPING:
            raise MappingError(400, "Say whether visa stamping is completed.")
        if medical not in VISA_MEDICAL:
            raise MappingError(400, "Choose the medical result: fit, unfit or pending.")
        return {"stamping": stamping, "medical": medical, "notes": notes}

    answer = payload.get("answer")
    if answer not in ANSWERS[stage]:
        raise MappingError(400, f"Choose an answer for “{STAGE_LABELS[stage]}”.")
    entry: dict = {"answer": answer, "notes": notes}

    if stage == "interview" and answer == "scheduled":
        interview_at = payload.get("interview_at")
        if not interview_at:
            raise MappingError(400, "A scheduled interview needs its date and time.")
        entry["interview_at"] = interview_at

    if stage == "travel" and answer == "travelled":
        travel_time = str(payload.get("travel_time") or "")
        if not _TIME.match(travel_time):
            raise MappingError(400, "Choose the flight's departure time.")
        flight = re.sub(r"\s+", " ", str(payload.get("flight_number") or "")).strip().upper()
        if not _FLIGHT.match(flight):
            raise MappingError(400, "Enter the flight number, for example EK 543.")
        entry.update(
            travel_date=_valid_date(payload.get("travel_date")),
            travel_time=travel_time,
            flight_number=flight,
            airline=(payload.get("airline") or "").strip()[:80],
        )
    return entry


def skip_entry(notes: str) -> dict:
    return {"skipped": True, "notes": (notes or "").strip()}


def updated_pipeline(
    doc: dict, stage: str, entry: dict, *, at: datetime, actor: dict,
) -> dict:
    """The pipeline with `entry` recorded, closed if the answer ends it."""
    job_order_id = mapped_job_order(doc)
    pipeline = current_pipeline(doc)
    stages = dict(pipeline.get("stages") or {})
    stages[stage] = {**entry, "at": at, **actor}
    result = {
        "job_order_id": job_order_id,
        "stages": stages,
        "closed_at": None,
        "closed_stage": None,
        "closed_reason": None,
        "closed_by": None,
    }
    reason = CLOSING_ANSWERS.get((stage, entry.get("answer")))
    if reason:
        result.update(
            closed_at=at, closed_stage=stage, closed_reason=reason,
            closed_by=actor.get("actor_name"),
        )
    return result


def closed_by_pipeline_release(
    doc: dict, *, stage: str, reason: str, at: datetime, actor: dict,
) -> Optional[dict]:
    """The pipeline to store when the candidate is released some other way.

    The candidate profile can record a rejection or a declined offer too. When
    it does, the mapping it ended is kept as closed — exactly as if the answer
    had been given here — so the row does not silently vanish from Mapping
    Candidates. None when the candidate was not mapped.
    """
    job_order_id = mapped_job_order(doc)
    if not job_order_id:
        return None
    pipeline = doc.get("mapping_pipeline") or {}
    stages = pipeline.get("stages") if pipeline.get("job_order_id") == job_order_id else None
    return {
        "job_order_id": job_order_id,
        "stages": dict(stages or {}),
        "closed_at": at,
        "closed_stage": stage,
        "closed_reason": reason,
        "closed_by": actor.get("actor_name"),
    }
