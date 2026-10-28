"""A job order, normalised into something that can be matched against.

Two rules shape everything here, and both come from the same place: a
requisition is written by a recruiter in a hurry, and most fields are blank.

    A requirement exists only if the order states it. Nothing in this module
    invents a licence, a certification or a years-of-experience floor that the
    recruiter did not type. An order with an empty `skills` list requires no
    skills — it does not require "the usual ones".

    A requirement the order states and the candidate record cannot answer is
    *unknown*, never satisfied. Unknown is a third outcome all the way through
    this feature, and it is what routes a candidate to manual review instead of
    onto a shortlist the evidence does not support.

The order document is free-form — `POST /job-orders` stores what the screen
sends — so every field is read defensively and the optional ones are read only
when present. The fields the CRM actually writes today are: `title`, `client`,
`destinationCountry`, `headcount`, `salary`, `skills`, `description`,
`dueDate`, `status`, `minExperience`, `minAge`, `maxAge`, `industry`,
`designation`. The handful of `required*` fields below are read if a
deployment adds them and are simply absent otherwise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Bumped whenever this normalisation changes in a way that could change a
#: score. It is part of the cache key, so bumping it recomputes everything.
SPEC_VERSION = "spec-1"

#: Orders a new shortlist may be generated for. `FILLED` and `CLOSED` are
#: history: they can be re-read and re-analysed, but only through the explicit
#: rematch path, never by a recruiter pressing the button by accident.
ACTIVE_STATUSES = frozenset({"OPEN", "IN PROGRESS"})

MANDATORY = "mandatory"
PREFERRED = "preferred"


@dataclass(frozen=True)
class Requirement:
    """One thing the order asks for, and where it was read from.

    `source` is the job-order field name, kept so the UI can say *why* a
    requirement is being applied, and so a recruiter who disagrees knows which
    field to edit.
    """

    key: str
    label: str
    kind: str  # MANDATORY | PREFERRED
    value: str
    source: str

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "kind": self.kind,
                "value": self.value, "source": self.source}


@dataclass(frozen=True)
class MatchSpec:
    """Everything about one job order that bears on matching."""

    order_id: str
    title: str
    client: str
    status: str
    designation: str = ""
    industry: str = ""
    destination_country: str = ""
    headcount: int = 0
    salary_text: str = ""
    currency: str = ""
    due_date: str = ""
    description: str = ""
    required_skills: List[str] = field(default_factory=list)
    preferred_skills: List[str] = field(default_factory=list)
    min_experience_years: Optional[float] = None
    min_experience_text: str = ""
    min_age: Optional[int] = None
    max_age: Optional[int] = None
    required_licences: List[str] = field(default_factory=list)
    required_certifications: List[str] = field(default_factory=list)
    required_documents: List[str] = field(default_factory=list)
    work_eligibility: str = ""
    availability_by: str = ""
    requirements: List[Requirement] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status.upper() in ACTIVE_STATUSES

    @property
    def role_terms(self) -> List[str]:
        """The words the order uses for the role.

        All of them, deliberately: a welder order may be titled "Welder",
        designated "TIG Welder" and filed under "Fabrication". Matching on any
        one of them alone is how a CV that says "Fabricator" gets thrown away
        for using a different word, which is exactly what this feature exists
        not to do.
        """
        return [term for term in (self.designation, self.title, self.industry) if term]

    @property
    def mandatory_requirements(self) -> List[Requirement]:
        return [item for item in self.requirements if item.kind == MANDATORY]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "title": self.title,
            "client": self.client,
            "status": self.status,
            "designation": self.designation,
            "industry": self.industry,
            "destination_country": self.destination_country,
            "headcount": self.headcount,
            "salary_text": self.salary_text,
            "currency": self.currency,
            "due_date": self.due_date,
            "required_skills": list(self.required_skills),
            "preferred_skills": list(self.preferred_skills),
            "min_experience_years": self.min_experience_years,
            "min_experience_text": self.min_experience_text,
            "min_age": self.min_age,
            "max_age": self.max_age,
            "required_licences": list(self.required_licences),
            "required_certifications": list(self.required_certifications),
            "required_documents": list(self.required_documents),
            "work_eligibility": self.work_eligibility,
            "availability_by": self.availability_by,
            "requirements": [item.as_dict() for item in self.requirements],
            "spec_version": SPEC_VERSION,
        }


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _string_list(value: Any) -> List[str]:
    """A list of non-empty strings, from a list or a comma-separated string."""
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [_text(part) for part in value]
    else:
        return []
    return [part for part in parts if part]


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_min_experience_years(text: Any) -> Optional[float]:
    """Years from the free text a recruiter typed, or None when it says nothing.

    `None` and `0` are different answers and the difference matters: "Any" and
    an empty field mean the order sets no floor, so there is no mandatory
    experience requirement to fail. A number means there is one.

    The browser-side matcher (`parseMinExperienceYears` in `JobOrders.tsx`)
    collapses both onto `0`; here they stay apart, because `0` would otherwise
    read as a requirement that every candidate trivially satisfies and would
    put a satisfied mandatory requirement on every explanation.
    """
    raw = _text(text)
    if not raw:
        return None
    lowered = raw.lower()
    if "fresher" in lowered or lowered in {"any", "none", "n/a", "na", "-"}:
        return None
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return None
    years = float(match.group())
    return years if years > 0 else None


def normalize_job_order(order: Dict[str, Any]) -> MatchSpec:
    """Read a stored job order into a `MatchSpec`.

    Only states a requirement the document actually carries. Everything absent
    stays absent, so a downstream dimension can report "the order does not say"
    rather than scoring against a requirement nobody wrote.
    """
    order = order or {}
    requirements: List[Requirement] = []

    designation = _text(order.get("designation"))
    title = _text(order.get("title"))
    industry = _text(order.get("industry"))
    destination = _text(order.get("destinationCountry") or order.get("destination_country"))

    # Skills on the requisition are what the client asked for, and the screen
    # labels them "Required Skills" — but they are matched on terminology, and
    # terminology is exactly what a CV varies. They are therefore scored
    # heavily and never used as a hard gate; a hard gate needs a field that
    # says so, which is `mandatorySkills` below.
    required_skills = _string_list(order.get("skills"))
    preferred_skills = _string_list(order.get("preferredSkills") or order.get("preferred_skills"))
    for skill in required_skills:
        requirements.append(Requirement(
            key=f"skill:{skill.lower()}", label=skill, kind=PREFERRED,
            value=skill, source="skills",
        ))
    for skill in preferred_skills:
        requirements.append(Requirement(
            key=f"preferred-skill:{skill.lower()}", label=skill, kind=PREFERRED,
            value=skill, source="preferredSkills",
        ))

    for skill in _string_list(order.get("mandatorySkills") or order.get("mandatory_skills")):
        requirements.append(Requirement(
            key=f"mandatory-skill:{skill.lower()}", label=f"{skill} (mandatory)",
            kind=MANDATORY, value=skill, source="mandatorySkills",
        ))

    min_experience_text = _text(order.get("minExperience") or order.get("min_experience"))
    min_experience_years = parse_min_experience_years(min_experience_text)
    if min_experience_years is not None:
        requirements.append(Requirement(
            key="min-experience",
            label=f"At least {min_experience_years:g} years of relevant experience",
            kind=MANDATORY, value=f"{min_experience_years:g}", source="minExperience",
        ))

    licences = _string_list(order.get("requiredLicences") or order.get("requiredLicenses")
                            or order.get("required_licences"))
    for licence in licences:
        requirements.append(Requirement(
            key=f"licence:{licence.lower()}", label=f"Licence: {licence}",
            kind=MANDATORY, value=licence, source="requiredLicences",
        ))

    certifications = _string_list(order.get("requiredCertifications")
                                  or order.get("required_certifications"))
    for certification in certifications:
        requirements.append(Requirement(
            key=f"certification:{certification.lower()}",
            label=f"Certification: {certification}",
            kind=MANDATORY, value=certification, source="requiredCertifications",
        ))

    documents = _string_list(order.get("requiredDocuments") or order.get("required_documents"))
    for document in documents:
        # Document readiness is its own scored dimension. A passport that has
        # not been collected yet is a reason to chase the candidate, not a
        # reason to call them ineligible for the job.
        requirements.append(Requirement(
            key=f"document:{document.lower()}", label=f"Document: {document}",
            kind=PREFERRED, value=document, source="requiredDocuments",
        ))

    work_eligibility = _text(order.get("workEligibility") or order.get("work_eligibility"))
    if work_eligibility:
        requirements.append(Requirement(
            key="work-eligibility", label=f"Work eligibility: {work_eligibility}",
            kind=MANDATORY, value=work_eligibility, source="workEligibility",
        ))

    min_age, max_age = _int(order.get("minAge")), _int(order.get("maxAge"))
    if min_age or max_age:
        # Stated by the order and unanswerable by the record: no date of birth
        # is stored on a candidate profile. It is kept as a mandatory
        # requirement precisely so it surfaces as unverified and sends the
        # candidate to manual review, rather than quietly passing.
        band = f"{min_age or '?'}–{max_age or '?'}"
        requirements.append(Requirement(
            key="age-band", label=f"Age {band}", kind=MANDATORY,
            value=band, source="minAge/maxAge",
        ))

    return MatchSpec(
        order_id=_text(order.get("id")),
        title=title,
        client=_text(order.get("client")),
        status=_text(order.get("status")).upper() or "OPEN",
        designation=designation,
        industry=industry,
        destination_country=destination,
        headcount=_int(order.get("headcount")) or 0,
        salary_text=_text(order.get("salary")),
        currency=_text(order.get("currency")),
        due_date=_text(order.get("dueDate") or order.get("due_date")),
        description=_text(order.get("description")),
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        min_experience_years=min_experience_years,
        min_experience_text=min_experience_text,
        min_age=min_age,
        max_age=max_age,
        required_licences=licences,
        required_certifications=certifications,
        required_documents=documents,
        work_eligibility=work_eligibility,
        availability_by=_text(order.get("availabilityBy") or order.get("availability_by")),
        requirements=requirements,
    )


def spec_fingerprint(spec: MatchSpec) -> str:
    """A short, stable digest of everything in the spec that can change a score.

    Part of the cache key. Editing an order's skills or its experience floor
    changes this and invalidates its results; renaming the client does not.
    """
    import hashlib
    import json

    payload = {
        "designation": spec.designation,
        "title": spec.title,
        "industry": spec.industry,
        "destination_country": spec.destination_country,
        "salary_text": spec.salary_text,
        "currency": spec.currency,
        "description": spec.description,
        "availability_by": spec.availability_by,
        "requirements": sorted(item.key for item in spec.requirements),
        "min_experience_years": spec.min_experience_years,
        "spec_version": SPEC_VERSION,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]
