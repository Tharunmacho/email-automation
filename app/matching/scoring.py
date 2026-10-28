"""From Jev's answers to a matching score and a category.

The score is a weighted sum of normalised dimensions, 0-100:

    score = 100 * sum(weight_d * value_d) / sum(weight_d)    over applicable d

and it is a **matching score** — how well the evidence on file answers this
order — not a probability that anyone gets hired. Jev's own probabilities are
inputs to it, never the output.

Three rules sit on top of the arithmetic, and each one exists because the
arithmetic alone would get a case wrong:

**Applicability is the order's call, not the candidate's.** A dimension the
order does not ask about is dropped and the rest are reweighted: an order with
no salary has no salary dimension, so nobody loses ten points to it. A
dimension the order *does* ask about and the candidate record cannot answer is
kept and scores zero — the requirement exists and the evidence for it does
not. Dropping it instead would reward a sparse record for being sparse.

**Mandatory requirements are not weights.** They are evaluated separately and
gate the category. A verified failure makes the candidate `ineligible` however
high the weighted score is, and the ranking sorts by category before score, so
no number can lift an ineligible candidate above an eligible one.

**Unknown goes to a person.** A mandatory requirement Jev could not verify, or
a record that contradicts itself, puts the candidate in `manual_review`. The
score is still shown — it is useful — but the category says the evidence is
not enough to act on.

The weights and thresholds below are starting defaults, written down in the
brief and not validated against hiring outcomes. They are configurable per
order (`matchingWeights` / `matchingThresholds` on the job-order document), and
the config's own digest is part of the cache key, so changing either rescores.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.matching.questions import NOT_SATISFIED, SATISFIED, UNKNOWN, Evaluation
from app.matching.spec import MatchSpec

SCORING_VERSION = "score-1"

#: Default weights, as given in the brief. Keys are components, not Jev
#: questions: `skills` is built from two answers and `mobility` from two.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "skills": 0.30,         # technical skills and equipment relevance
    "experience": 0.25,     # relevant experience
    "qualifications": 0.15, # qualifications, certifications, licences
    "salary": 0.10,         # salary compatibility
    "mobility": 0.10,       # destination and availability
    "documents": 0.10,      # document readiness
}

#: Category thresholds on the 0-100 score. Documented defaults, not calibrated:
#: the offline evaluation in `app.matching.evaluation` is what should move them.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "strong": 75.0,
    "potential": 50.0,
    #: Below this the record is too thin to rank on, whatever the score says.
    "min_evidence_quality": 0.25,
    #: A `conflicting_evidence` noul at or above this sends the candidate to a
    #: person: a record that disagrees with itself cannot be scored with
    #: confidence in either direction.
    "conflict_review": 0.5,
}

STRONG = "strong_match"
POTENTIAL = "potential_match"
REVIEW = "manual_review"
LOW = "low_match"
INELIGIBLE = "ineligible"

#: How the shortlist sorts. Category first, always: this ordering is the
#: mechanism that stops a score from overriding a failed requirement.
CATEGORY_RANK = {STRONG: 0, POTENTIAL: 1, REVIEW: 2, LOW: 3, INELIGIBLE: 4}

CATEGORY_LABELS = {
    STRONG: "Strong match",
    POTENTIAL: "Potential match",
    REVIEW: "Manual review required",
    LOW: "Low match",
    INELIGIBLE: "Ineligible — verified mandatory requirement not met",
}


@dataclass(frozen=True)
class ScoringConfig:
    weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    thresholds: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))

    @property
    def version(self) -> str:
        """A digest of the config, so a changed weight is a cache miss."""
        payload = json.dumps(
            {"weights": self.weights, "thresholds": self.thresholds, "v": SCORING_VERSION},
            sort_keys=True,
        )
        return f"{SCORING_VERSION}-{hashlib.sha256(payload.encode()).hexdigest()[:10]}"

    def as_dict(self) -> Dict[str, Any]:
        return {"weights": dict(self.weights), "thresholds": dict(self.thresholds),
                "version": self.version}


def _clean_numbers(raw: Any, allowed: Dict[str, float]) -> Dict[str, float]:
    """Only known keys, only finite non-negative numbers. Anything else is ignored."""
    out: Dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if key not in allowed:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0 and number == number and number != float("inf"):
            out[key] = number
    return out


def config_for(order: Optional[Dict[str, Any]] = None) -> ScoringConfig:
    """The defaults, with whatever the order overrides.

    Per order rather than per "job type" because the order is the one record
    that exists today: an `industry` value is free text typed into a form, and a
    table keyed on it would be keyed on spelling. An order that wants different
    weights carries them.
    """
    order = order or {}
    weights = {**DEFAULT_WEIGHTS, **_clean_numbers(order.get("matchingWeights"), DEFAULT_WEIGHTS)}
    if sum(weights.values()) <= 0:
        weights = dict(DEFAULT_WEIGHTS)
    thresholds = {**DEFAULT_THRESHOLDS,
                  **_clean_numbers(order.get("matchingThresholds"), DEFAULT_THRESHOLDS)}
    return ScoringConfig(weights=weights, thresholds=thresholds)


@dataclass(frozen=True)
class Component:
    key: str
    weight: float
    value: Optional[float]   # None: not applicable to this order
    unknown: bool = False    # applicable, but the record did not say
    parts: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchOutcome:
    score: float
    category: str
    components: List[Component]
    matched: List[str]
    unverified: List[str]
    failed: List[str]
    reasons: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "category": self.category,
            "category_label": CATEGORY_LABELS[self.category],
            "components": [
                {"key": c.key, "weight": c.weight,
                 "value": None if c.value is None else round(c.value, 4),
                 "applicable": c.value is not None, "unknown": c.unknown, "parts": c.parts}
                for c in self.components
            ],
            "matched_requirements": list(self.matched),
            "unverified_requirements": list(self.unverified),
            "failed_requirements": list(self.failed),
            "reasons": list(self.reasons),
        }


def _mean_stated(evaluation: Evaluation, keys: List[str]) -> tuple[float, bool, Dict[str, Any]]:
    """Mean over the answers that carry evidence; (0, unknown) when none do."""
    parts: Dict[str, Any] = {}
    stated: List[float] = []
    for key in keys:
        dimension = evaluation.dimensions.get(key)
        if dimension is None:
            parts[key] = None
            continue
        parts[key] = {"value": round(dimension.value, 4), "stated": dimension.stated,
                      "label": dimension.label}
        if dimension.stated:
            stated.append(dimension.value)
    if not stated:
        return 0.0, True, parts
    return sum(stated) / len(stated), False, parts


def components_for(spec: MatchSpec, evaluation: Evaluation, config: ScoringConfig) -> List[Component]:
    weights = config.weights
    out: List[Component] = []

    skill_keys = ["role_relevance"] + (["skills_match"] if spec.required_skills else [])
    value, unknown, parts = _mean_stated(evaluation, skill_keys)
    out.append(Component("skills", weights["skills"], value, unknown, parts))

    value, unknown, parts = _mean_stated(evaluation, ["experience_match"])
    out.append(Component("experience", weights["experience"], value, unknown, parts))

    if spec.required_licences or spec.required_certifications:
        value, unknown, parts = _mean_stated(evaluation, ["qualifications_match"])
        out.append(Component("qualifications", weights["qualifications"], value, unknown, parts))
    else:
        out.append(Component("qualifications", weights["qualifications"], None,
                             parts={"reason": "The order names no licence or certification"}))

    if spec.salary_text:
        value, unknown, parts = _mean_stated(evaluation, ["salary_fit"])
        out.append(Component("salary", weights["salary"], value, unknown, parts))
    else:
        out.append(Component("salary", weights["salary"], None,
                             parts={"reason": "The order states no salary"}))

    mobility_keys = (["destination_fit"] if spec.destination_country else []) + (
        ["availability_fit"] if (spec.availability_by or spec.due_date) else [])
    if mobility_keys:
        value, unknown, parts = _mean_stated(evaluation, mobility_keys)
        out.append(Component("mobility", weights["mobility"], value, unknown, parts))
    else:
        out.append(Component("mobility", weights["mobility"], None,
                             parts={"reason": "The order states no destination or start date"}))

    value, unknown, parts = _mean_stated(evaluation, ["document_readiness"])
    out.append(Component("documents", weights["documents"], value, unknown, parts))
    return out


def score(spec: MatchSpec, evaluation: Evaluation, config: Optional[ScoringConfig] = None) -> MatchOutcome:
    config = config or ScoringConfig()
    thresholds = config.thresholds
    components = components_for(spec, evaluation, config)

    applicable = [c for c in components if c.value is not None and c.weight > 0]
    total_weight = sum(c.weight for c in applicable)
    weighted = sum(c.weight * (c.value or 0.0) for c in applicable)
    value = round(100.0 * weighted / total_weight, 1) if total_weight else 0.0

    matched = [v.label for v in evaluation.verdicts if v.status == SATISFIED]
    unverified = [v.label for v in evaluation.verdicts if v.status == UNKNOWN]
    failed = [v.label for v in evaluation.verdicts if v.status == NOT_SATISFIED]
    reasons: List[str] = []

    evidence = evaluation.dimensions.get("evidence_quality")
    thin_record = evidence is not None and evidence.value < thresholds["min_evidence_quality"]
    conflicted = (evaluation.conflicting_evidence or 0.0) >= thresholds["conflict_review"]

    if failed:
        category = INELIGIBLE
        reasons.append("A mandatory requirement is evidenced as not met: " + "; ".join(failed))
    elif unverified:
        category = REVIEW
        reasons.append("Mandatory requirement could not be verified from the record: "
                       + "; ".join(unverified))
    elif conflicted:
        category = REVIEW
        reasons.append("The candidate record contradicts itself on points this role turns on")
    elif thin_record:
        category = REVIEW
        reasons.append("The candidate record is too sparse to rank with confidence")
    elif value >= thresholds["strong"]:
        category = STRONG
    elif value >= thresholds["potential"]:
        category = POTENTIAL
    else:
        category = LOW

    for component in components:
        if component.value is not None and component.unknown:
            reasons.append(f"No evidence on file for {component.key}")
    return MatchOutcome(
        score=value, category=category, components=components,
        matched=matched, unverified=unverified, failed=failed, reasons=reasons,
    )


def rank_key(category: str, score_value: float) -> tuple:
    """Category first, then score. The one sort every shortlist uses."""
    return (CATEGORY_RANK.get(category, 99), -float(score_value or 0.0))
