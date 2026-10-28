"""From the matcher's readings to a matching score and a category.

The score is a weighted mean of the components that apply to the order, 0-100:

    score = 100 * sum(weight_c * value_c) / sum(weight_c)    over applicable c

and it is a **matching score** — how well the evidence on file answers this
order — not a probability that anyone gets hired.

Three rules sit on top of the arithmetic, and each exists because the
arithmetic alone would get a case wrong:

**Applicability is the order's call, not the candidate's.** A component the
order does not ask about is dropped and the rest are reweighted: an order with
no experience minimum has no experience component. A component the order
*does* ask about and the record cannot answer is kept and scores zero — the
requirement exists and the evidence for it does not.

**Mandatory requirements are not weights.** They are checked separately and
gate the category. A verified failure makes the candidate `ineligible` however
high the score, and the ranking sorts by category before score, so no number
can lift an ineligible candidate above an eligible one.

**Unknown goes to a person.** A mandatory requirement the record cannot answer
puts a plausible candidate — a good score, or a title that fits — in
`manual_review`. So does a record too thin to score skills on when the title
fits: the WhatsApp intake often leaves only a job title and a band.

Weights and thresholds are starting defaults, not validated against hiring
outcomes. `python -m app.matching.evaluation` measures them against recruiter
decisions; a job order can override them (`matchingWeights` /
`matchingThresholds`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCORING_VERSION = "match-2"

DEFAULT_WEIGHTS: Dict[str, float] = {
    "role": 0.30,            # the job title, or the trade it belongs to
    "skills": 0.30,          # the order's skills, found on the record
    "experience": 0.15,      # years against the order's minimum
    "relevance": 0.10,       # how much of the order the whole CV covers (BM25)
    "qualifications": 0.10,  # licences and certifications the order names
    "destination": 0.05,     # willing to work where the job is
    "documents": 0.05,       # passport and paperwork readiness
}

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "strong": 75.0,
    "potential": 50.0,
    #: A role reading at or above this makes a candidate plausible enough that
    #: an unanswered mandatory item, or a thin record, is a reason to phone
    #: them rather than to rank them low.
    "review_role": 0.75,
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
    INELIGIBLE: "Ineligible — mandatory requirement not met",
}

SATISFIED, NOT_SATISFIED, UNKNOWN = "satisfied", "not_satisfied", "unknown"


@dataclass(frozen=True)
class ScoringConfig:
    weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    thresholds: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))

    @property
    def version(self) -> str:
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
    """The defaults, with whatever the order overrides."""
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
    value: Optional[float]   # None: the order does not ask about this
    unknown: bool = False    # asked, but the record does not say
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "weight": self.weight,
                "value": None if self.value is None else round(self.value, 4),
                "applicable": self.value is not None, "unknown": self.unknown, "detail": self.detail}


@dataclass(frozen=True)
class Verdict:
    """One mandatory requirement, checked against the record."""

    key: str
    label: str
    status: str  # SATISFIED | NOT_SATISFIED | UNKNOWN
    source: str = ""
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "status": self.status,
                "source": self.source, "detail": self.detail}


@dataclass(frozen=True)
class Outcome:
    score: float
    category: str
    components: List[Component]
    verdicts: List[Verdict]
    reasons: List[str]

    @property
    def matched(self) -> List[str]:
        return [v.label for v in self.verdicts if v.status == SATISFIED]

    @property
    def unverified(self) -> List[str]:
        return [v.label for v in self.verdicts if v.status == UNKNOWN]

    @property
    def failed(self) -> List[str]:
        return [v.label for v in self.verdicts if v.status == NOT_SATISFIED]


def combine(
    components: List[Component],
    verdicts: List[Verdict],
    *,
    role: float,
    sparse: bool,
    config: Optional[ScoringConfig] = None,
    reasons: Optional[List[str]] = None,
) -> Outcome:
    """Weighted score, then the category rules in the module docstring."""
    config = config or ScoringConfig()
    thresholds = config.thresholds
    applicable = [c for c in components if c.value is not None and c.weight > 0]
    total = sum(c.weight for c in applicable)
    value = round(100.0 * sum(c.weight * (c.value or 0.0) for c in applicable) / total, 1) if total else 0.0

    reasons = list(reasons or [])
    failed = [v.label for v in verdicts if v.status == NOT_SATISFIED]
    unverified = [v.label for v in verdicts if v.status == UNKNOWN]
    plausible = value >= thresholds["potential"] or role >= thresholds["review_role"]

    if failed:
        category = INELIGIBLE
        reasons.insert(0, "Mandatory requirement not met: " + "; ".join(failed))
    elif unverified and plausible:
        category = REVIEW
        reasons.insert(0, "Mandatory requirement not on record — verify: " + "; ".join(unverified))
    elif sparse and role >= thresholds["review_role"]:
        category = REVIEW
        reasons.insert(0, "The title fits but the record is too thin to check skills — worth a call")
    elif value >= thresholds["strong"]:
        category = STRONG
    elif value >= thresholds["potential"]:
        category = POTENTIAL
    else:
        category = LOW
    return Outcome(score=value, category=category, components=components, verdicts=verdicts, reasons=reasons)


def rank_key(category: Optional[str], score_value: Optional[float]) -> tuple:
    """Category first, then score. The one sort every shortlist uses."""
    return (CATEGORY_RANK.get(category or "", 99), -float(score_value or 0.0))
