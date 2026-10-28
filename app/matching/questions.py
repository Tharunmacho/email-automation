"""The decision schema: what Jev is asked about one candidate and one order.

Versioned, because a question is part of how a score was arrived at. Change
the wording of a rubric and yesterday's 82 is not today's 82, so
`DECISION_SCHEMA_VERSION` is bumped, it goes into the cache key, and every
stored result says which schema produced it.

Three question types, used for three different jobs:

    score   the graded dimensions — role relevance, skills, experience,
            qualifications, document readiness, evidence quality. An ordered
            rubric whose levels are written so that the bottom one is always
            "the record does not say", never "no".
    choice  the categorical ones — salary, destination, availability — each
            with an explicit `not_stated` option, so "we never asked" has
            somewhere to go that is not "incompatible".
    noul    the two true/false signals: whether the candidate's own job titles
            are an equivalent trade, and whether the record contradicts itself.

And one `choice` per mandatory requirement, always with the same three
options: `satisfied`, `not_satisfied`, `unknown`. That triple is the whole
eligibility rule — a requirement the record cannot answer comes back `unknown`
and sends the candidate to manual review. It never passes.

Every instruction says, in the words the vendor's docs use for rubrics, that
the answer must rest on what the record states. A model cannot be prevented
from being wrong, but it can be denied the invitation to fill a gap: that is
what the bottom rubric level and the `unknown` option are for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.matching.spec import MANDATORY, MatchSpec

#: Bumped whenever a question, rubric or option changes. Part of the cache key.
DECISION_SCHEMA_VERSION = "jev-match-1"

#: Mandatory-requirement question ids are prefixed, so reading the answers back
#: does not depend on the order the requirements happened to be in.
REQUIREMENT_PREFIX = "requirement__"

SATISFIED, NOT_SATISFIED, UNKNOWN = "satisfied", "not_satisfied", "unknown"

#: How sure a `satisfied` has to be before it counts as verified. Below it the
#: answer is treated as `unknown` — a low-confidence pass on a licence is not
#: evidence that somebody holds one. Configurable here rather than in settings
#: because it belongs to the schema it was calibrated against.
MIN_VERIFICATION_CONFIDENCE = 0.6

_NOT_STATED = "not_stated"

#: The five-level rubric the graded dimensions share. One shape, so a score of
#: 0.5 means the same thing everywhere, and `_normalise` can be one function.
_LEVELS = 5


def _score_question(instructions: str, levels: List[str]) -> Dict[str, Any]:
    return {"type": "score", "instructions": instructions, "criteria": levels}


def _choice_question(instructions: str, criteria: Dict[str, str]) -> Dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def _noul_question(instructions: str, criteria: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    question: Dict[str, Any] = {"type": "noul", "instructions": instructions}
    if criteria:
        question["criteria"] = criteria
    return question


def build_state(spec: MatchSpec, candidate: Dict[str, Any]) -> Dict[str, Any]:
    """The one state both the order and the candidate live in.

    Structured rather than prose: the questions below name `job_order.skills`
    and `candidate.work_experience`, and a field name is only worth anything if
    it survives into the thing being read.
    """
    return {
        "job_order": {
            "role": spec.designation or spec.title,
            "title": spec.title,
            "industry": spec.industry,
            "destination_country": spec.destination_country,
            "required_skills": list(spec.required_skills),
            "preferred_skills": list(spec.preferred_skills),
            "minimum_experience_years": spec.min_experience_years,
            "minimum_experience_text": spec.min_experience_text,
            "salary_offered": spec.salary_text,
            "currency": spec.currency,
            "required_licences": list(spec.required_licences),
            "required_certifications": list(spec.required_certifications),
            "required_documents": list(spec.required_documents),
            "work_eligibility": spec.work_eligibility,
            "availability_needed_by": spec.availability_by or spec.due_date,
            "description": spec.description,
        },
        "candidate": candidate,
    }


def build_questions(spec: MatchSpec) -> Dict[str, Any]:
    """Every question asked about this pair, keyed by the id answers come back under."""
    questions: Dict[str, Any] = {
        "role_relevance": _score_question(
            "How relevant the candidate's occupational history in `candidate` is to "
            "`job_order.role`. Judge the work actually described, not the wording of a "
            "job title: a fabricator who welds all day is relevant to a welder vacancy. "
            "Use only what the record states.",
            [
                "The record does not describe the candidate's occupation",
                "A different trade with no overlapping work",
                "A related trade with some overlapping work",
                "The same broad trade, a different specialisation",
                "The same occupation as the role",
            ],
        ),
        "skills_match": _score_question(
            "How well the candidate's skills, trade skills and equipment experience cover "
            "`job_order.required_skills`. Count a skill as covered when the record shows the "
            "candidate has done that work, under whatever name. Do not credit a skill the "
            "record does not mention.",
            [
                "The record lists no skills to compare",
                "None of the required skills appear",
                "A minority of the required skills appear",
                "Most of the required skills appear",
                "Every required skill appears",
            ],
        ),
        "experience_match": _score_question(
            "How well the candidate's length and depth of relevant experience meets "
            "`job_order.minimum_experience_years`. Relevant means in the trade the order is "
            "for. If the record gives no figure and no datable history, say so with the "
            "lowest level rather than estimating.",
            [
                "The record does not establish how much relevant experience the candidate has",
                "Clearly short of the requirement",
                "Slightly short of the requirement",
                "Meets the requirement",
                "Comfortably exceeds the requirement",
            ],
        ),
        "qualifications_match": _score_question(
            "How well the candidate's education, course or trade, certifications and licences "
            "meet `job_order.required_certifications` and `job_order.required_licences`. A "
            "licence the record does not list is not held; do not infer one from the job title.",
            [
                "The record lists no qualifications",
                "None of the required qualifications are evidenced",
                "Some are evidenced, the rest are missing or unverified",
                "All required qualifications are evidenced, some weakly",
                "All required qualifications are clearly evidenced",
            ],
        ),
        "document_readiness": _score_question(
            "How ready the candidate's paperwork is for deployment, from "
            "`candidate.documents`. A passport on file with a distant expiry is ready; "
            "outstanding documents are not.",
            [
                "Nothing on file and nothing recorded about documents",
                "Key documents outstanding",
                "Some documents on file, some outstanding",
                "The documents needed are on file",
                "All documents on file and valid well beyond the deployment window",
            ],
        ),
        "evidence_quality": _score_question(
            "How complete and internally consistent the candidate record is as evidence for "
            "this decision. This is about the record, not the person: a strong candidate with "
            "a two-line profile scores low here.",
            [
                "Almost nothing to go on",
                "Sparse: the main fields are empty",
                "Partial: enough for some dimensions, not others",
                "Good: the fields this role turns on are filled",
                "Full: trade, history, qualifications and documents all recorded",
            ],
        ),
        "salary_fit": _choice_question(
            "Whether `job_order.salary_offered` is compatible with what the candidate record "
            "says about pay or expectations. Choose `not_stated` unless the record actually "
            "carries a figure or expectation — most do not.",
            {
                "compatible": "The offer is at or above what the record indicates the candidate expects",
                "below_expectation": "The offer is below what the record indicates, but not by much",
                "incompatible": "The offer is clearly below what the record indicates",
                _NOT_STATED: "The record says nothing about pay, or the order states no salary",
            },
        ),
        "destination_fit": _choice_question(
            "Whether the candidate is willing to work in `job_order.destination_country`, "
            "from `candidate.preferred_destination_country` and "
            "`candidate.other_destination_countries`.",
            {
                "matches": "The destination is the candidate's stated preference",
                "acceptable": "The destination is among the countries the candidate listed",
                "mismatch": "The candidate stated other destinations and not this one",
                _NOT_STATED: "The record does not say where the candidate will work",
            },
        ),
        "availability_fit": _choice_question(
            "Whether the candidate can start by `job_order.availability_needed_by`, from "
            "`candidate.available_from`.",
            {
                "available": "Available now or by the date the order needs",
                "later": "Available, but after the date the order needs",
                _NOT_STATED: "The record does not say when the candidate can start",
            },
        ),
        "equivalent_title": _noul_question(
            "The candidate's own job titles name the same trade as `job_order.role`, allowing "
            "for different wording between employers and countries.",
            {
                "true": "The titles mean the same occupation, even if worded differently",
                "false": "The titles name a different occupation",
            },
        ),
        "conflicting_evidence": _noul_question(
            "The candidate record contains statements that contradict each other on the points "
            "this role turns on, such as a stated experience figure that the dated work history "
            "does not support.",
        ),
    }

    for index, requirement in enumerate(spec.mandatory_requirements):
        questions[f"{REQUIREMENT_PREFIX}{index}"] = _choice_question(
            f"Does the candidate record satisfy this mandatory requirement of the job order: "
            f"{requirement.label}? Answer `{UNKNOWN}` whenever the record does not contain the "
            f"evidence either way. Do not infer the requirement from the candidate's job title, "
            f"their employer, or how experienced they appear.",
            {
                SATISFIED: "The record directly evidences that this requirement is met",
                NOT_SATISFIED: "The record directly evidences that this requirement is not met",
                UNKNOWN: "The record does not say, or the evidence is indirect",
            },
        )
    return questions


# --------------------------------------------------------------------------- #
#  Reading the answers back
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Dimension:
    """One graded answer, normalised to 0..1 with the model's own confidence.

    `stated` is False when the answer landed on the bottom rubric level or on a
    `not_stated` option — the dimension has no evidence behind it, and scoring
    treats that differently from a low score that does.
    """

    key: str
    value: float
    confidence: float
    label: str = ""
    stated: bool = True


@dataclass(frozen=True)
class RequirementVerdict:
    """What Jev made of one mandatory requirement."""

    key: str
    label: str
    status: str  # SATISFIED | NOT_SATISFIED | UNKNOWN
    confidence: float
    source: str = ""


@dataclass(frozen=True)
class Evaluation:
    """Everything one Jev response says about one candidate and one order."""

    dimensions: Dict[str, Dimension] = field(default_factory=dict)
    verdicts: List[RequirementVerdict] = field(default_factory=list)
    equivalent_title: Optional[float] = None
    conflicting_evidence: Optional[float] = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def value(self, key: str, default: float = 0.0) -> float:
        dimension = self.dimensions.get(key)
        return dimension.value if dimension else default

    def as_dict(self) -> Dict[str, Any]:
        return {
            "dimensions": {
                key: {"value": round(dimension.value, 4), "confidence": round(dimension.confidence, 4),
                      "label": dimension.label, "stated": dimension.stated}
                for key, dimension in self.dimensions.items()
            },
            "requirements": [
                {"key": verdict.key, "label": verdict.label, "status": verdict.status,
                 "confidence": round(verdict.confidence, 4), "source": verdict.source}
                for verdict in self.verdicts
            ],
            "equivalent_title": self.equivalent_title,
            "conflicting_evidence": self.conflicting_evidence,
            "model": self.model,
            "schema_version": DECISION_SCHEMA_VERSION,
        }


#: Choice dimensions, and what each option is worth once normalised. `None`
#: means the option carries no evidence, which `Dimension.stated` records and
#: scoring then reweights around rather than scoring as zero.
_CHOICE_VALUES: Dict[str, Dict[str, Optional[float]]] = {
    "salary_fit": {"compatible": 1.0, "below_expectation": 0.5, "incompatible": 0.0, _NOT_STATED: None},
    "destination_fit": {"matches": 1.0, "acceptable": 0.8, "mismatch": 0.0, _NOT_STATED: None},
    "availability_fit": {"available": 1.0, "later": 0.4, _NOT_STATED: None},
}

_SCORE_KEYS = (
    "role_relevance", "skills_match", "experience_match",
    "qualifications_match", "document_readiness", "evidence_quality",
)


def _graded(answer: Dict[str, Any]) -> tuple[float, bool]:
    """A score answer as (value 0..1, whether the record stated anything).

    Every graded rubric puts "the record does not say" at level 0, and that
    level is evidence of nothing — it must not be scored as a low mark. So the
    value is computed over levels 1..n only, from the documented per-level
    `probabilities`, renormalised once level 0 is set aside:

        stated  = P(level 0) < 0.5
        value   = sum(P(k) * (k-1)/(n-2) for k >= 1) / sum(P(k) for k >= 1)

    Level 1 ("none of the skills", "clearly short") is therefore 0.0, not the
    0.25 a straight division of the position would give it.

    Falls back to the probability-weighted `score` when a response carries no
    probabilities, treating a position below 0.5 as unstated.
    """
    probabilities = answer.get("probabilities")
    levels = len(answer.get("legend") or {}) or len(probabilities or {}) or _LEVELS
    span = max(1, levels - 2)

    if isinstance(probabilities, dict) and probabilities:
        mass: Dict[int, float] = {}
        for level, probability in probabilities.items():
            try:
                mass[int(level)] = float(probability)
            except (TypeError, ValueError):
                continue
        unstated = mass.get(0, 0.0)
        stated_mass = sum(p for level, p in mass.items() if level >= 1)
        if stated_mass <= 0:
            return 0.0, False
        value = sum(p * (level - 1) / span for level, p in mass.items() if level >= 1) / stated_mass
        return max(0.0, min(1.0, value)), unstated < 0.5

    try:
        position = float(answer.get("score"))
    except (TypeError, ValueError):
        return 0.0, False
    if position < 0.5:
        return 0.0, False
    return max(0.0, min(1.0, (position - 1) / span)), True


def read_answers(answers: Dict[str, Any], spec: MatchSpec, *, model: str = "") -> Evaluation:
    """Turn one Jev response into the evaluation scoring works from.

    Tolerant of a missing answer — an absent question is a dimension with no
    evidence, not a zero — and strict about the shape of one that is present:
    an answer whose type is not what was asked is dropped rather than coerced.
    """
    dimensions: Dict[str, Dimension] = {}

    for key in _SCORE_KEYS:
        answer = answers.get(key)
        if not isinstance(answer, dict) or answer.get("type") != "score":
            continue
        value, stated = _graded(answer)
        legend = answer.get("legend") or {}
        # The legend maps level -> text; the nearest level names the band the
        # fractional score landed in, which is what an explanation shows.
        try:
            nearest = str(int(round(float(answer.get("score") or 0))))
        except (TypeError, ValueError):
            nearest = "0"
        dimensions[key] = Dimension(
            key=key,
            value=value,
            confidence=float(answer.get("confidence") or 0.0),
            label=str(legend.get(nearest, "")),
            stated=stated,
        )

    for key, values in _CHOICE_VALUES.items():
        answer = answers.get(key)
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            continue
        chosen = str(answer.get("choice") or "")
        mapped = values.get(chosen)
        dimensions[key] = Dimension(
            key=key,
            value=0.0 if mapped is None else mapped,
            confidence=float(answer.get("confidence") or 0.0),
            label=chosen,
            stated=mapped is not None,
        )

    verdicts: List[RequirementVerdict] = []
    for index, requirement in enumerate(spec.mandatory_requirements):
        answer = answers.get(f"{REQUIREMENT_PREFIX}{index}")
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            # No answer is not a pass. A requirement nobody answered is a
            # requirement nobody verified.
            verdicts.append(RequirementVerdict(
                key=requirement.key, label=requirement.label, status=UNKNOWN,
                confidence=0.0, source=requirement.source,
            ))
            continue
        status = str(answer.get("choice") or UNKNOWN)
        confidence = float(answer.get("confidence") or 0.0)
        if status == SATISFIED and confidence < MIN_VERIFICATION_CONFIDENCE:
            # A hesitant yes on a mandatory requirement is not a yes.
            status = UNKNOWN
        if status not in (SATISFIED, NOT_SATISFIED, UNKNOWN):
            status = UNKNOWN
        verdicts.append(RequirementVerdict(
            key=requirement.key, label=requirement.label, status=status,
            confidence=confidence, source=requirement.source,
        ))

    def noul_of(key: str) -> Optional[float]:
        answer = answers.get(key)
        if isinstance(answer, dict) and answer.get("type") == "noul":
            try:
                return float(answer.get("noul"))
            except (TypeError, ValueError):
                return None
        return None

    return Evaluation(
        dimensions=dimensions,
        verdicts=verdicts,
        equivalent_title=noul_of("equivalent_title"),
        conflicting_evidence=noul_of("conflicting_evidence"),
        model=model,
    )
