"""One candidate, reduced to the evidence a match turns on.

Everything here comes from the record that is already in Mongo — the profile
the existing OCR and extraction pipeline wrote. Nothing re-reads a CV, nothing
re-runs OCR, and nothing writes back to a candidate document.

Two things this module is responsible for:

**Sending the minimum.** The identifiers a recruitment record accumulates —
passport number, phone numbers, email, address — have no bearing on whether
somebody can weld, and a third-party API is not the place for them. The state
built here carries trades, experience, qualifications, preferences and
document readiness, and `FORBIDDEN_FIELDS` is asserted over the result so a
field added to the profile later cannot quietly start travelling. A passport
is represented as "on file, expires 2029-04" — readiness without the number.

**Keeping the field names.** The state is a dict, not a paragraph, so an
answer about "qualifications" is an answer about `qualifications`, and an
explanation can name the field it rests on. `excerpt_for` then pulls the
supporting line out of the record itself rather than asking the model to quote
one, which is the difference between evidence and a plausible sentence.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

#: Field names that must never appear anywhere in a state sent to Jev.
#: Asserted by `assert_no_identifiers`, which the test suite runs over real
#: fixtures — a cheap guard that survives someone adding a field upstream.
FORBIDDEN_FIELDS = frozenset({
    "passport_number", "aadhaar", "aadhaar_number", "pan", "pan_number",
    "phone", "phone_e164", "phone_numbers", "mobile", "whatsapp_number",
    "email", "from_addr", "to_addr", "linkedin_url", "github_url",
    "portfolio_url", "address", "street", "raw_ocr", "storage_key",
})

#: Long free text is trimmed before it is sent. The documented per-question
#: context is 32k tokens and a CV summary is nowhere near it, but a résumé that
#: OCR'd into twelve pages of description is, and the first part of it is where
#: the trade is named.
_SUMMARY_LIMIT = 1200
_DESCRIPTION_LIMIT = 400
_MAX_ROLES = 6


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _clip(value: Any, limit: int) -> str:
    text = " ".join(_text(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strings(value: Any, limit: int = 40) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out = [_text(item) for item in value]
    return [item for item in out if item][:limit]


def _profile_of(row: Dict[str, Any]) -> Dict[str, Any]:
    profile = row.get("profile")
    return profile if isinstance(profile, dict) else {}


def experience_years(row: Dict[str, Any]) -> Optional[float]:
    """Years on the record, or None.

    None is not zero. A profile with no figure has not told us the candidate is
    a fresher — the WhatsApp intake stores a band instead, and a résumé may
    simply not have said. Treating the blank as zero is how a twenty-year
    tradesman fails a three-year floor.
    """
    profile = _profile_of(row)
    value = profile.get("total_experience_years")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _licences(profile: Dict[str, Any]) -> List[Dict[str, str]]:
    """Licences without their numbers.

    Whether somebody holds a valid forklift licence is the question; the number
    on the card answers nothing and is an identifier.
    """
    out: List[Dict[str, str]] = []
    for licence in profile.get("licenses") or []:
        if not isinstance(licence, dict):
            continue
        entry = {key: _text(licence.get(key)) for key in ("name", "issuer", "expiry_date")}
        if any(entry.values()):
            out.append({key: value for key, value in entry.items() if value})
    return out[:10]


def _roles(profile: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for role in profile.get("work_experience") or []:
        if not isinstance(role, dict):
            continue
        entry = {
            "designation": _text(role.get("designation") or role.get("title")),
            "company": _text(role.get("company")),
            "duration": _text(role.get("duration") or role.get("period")),
            "start": _text(role.get("start_date")),
            "end": _text(role.get("end_date")),
            "description": _clip(role.get("description"), _DESCRIPTION_LIMIT),
        }
        entry = {key: value for key, value in entry.items() if value}
        if entry:
            out.append(entry)
    return out[:_MAX_ROLES]


def _education(profile: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in profile.get("education") or []:
        if not isinstance(item, dict):
            continue
        entry = {
            "qualification": _text(item.get("degree") or item.get("qualification")),
            "institution": _text(item.get("institution") or item.get("school")),
            "year": _text(item.get("year") or item.get("end_year")),
        }
        entry = {key: value for key, value in entry.items() if value}
        if entry:
            out.append(entry)
    return out[:6]


def _documents(row: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """What is on file, as readiness rather than as identifiers."""
    registration = row.get("registration_state")
    outstanding = []
    if isinstance(registration, dict):
        outstanding = _strings(registration.get("outstanding_documents"), limit=12)
    return {
        "resume_on_file": bool(row.get("resume_available") or row.get("resume")),
        "passport_on_file": bool(_text(profile.get("passport_number"))),
        # The date, never the number: a passport expiring inside the
        # deployment window is a real blocker on an overseas placement.
        "passport_expiry": _text(profile.get("passport_expiry")),
        "outstanding_documents": outstanding,
    }


def candidate_state(row: Dict[str, Any]) -> Dict[str, Any]:
    """The evidence for one candidate, as the state Jev reasons over.

    Keys are stable and are what the questions refer to, so an answer can be
    traced back to the field it was drawn from.
    """
    profile = _profile_of(row)
    skills = _strings(profile.get("skills"))
    technical = _strings(profile.get("technical_skills"))
    trade = _strings(profile.get("trade_skills"))

    state: Dict[str, Any] = {
        "current_designation": _text(profile.get("current_designation")),
        "current_company": _text(profile.get("current_company")),
        "applied_for": _text(profile.get("job_title") or profile.get("job_preference")),
        "job_category": _text(profile.get("job_category")),
        "course_or_trade": _text(profile.get("course_or_trade")),
        "total_experience_years": experience_years(row),
        # Kept beside the number rather than folded into it: "2-5 years" is
        # what the candidate said, and turning it into 3.5 would be inventing
        # a figure nobody gave.
        "total_experience_band": _text(profile.get("total_experience_band")),
        "skills": skills,
        "technical_skills": technical,
        "trade_skills": trade,
        "languages": _strings(profile.get("languages"), limit=12),
        "certifications": _strings(profile.get("certifications"), limit=15),
        "licences": _licences(profile),
        "education": _education(profile),
        "work_experience": _roles(profile),
        "resume_summary": _clip(profile.get("resume_summary"), _SUMMARY_LIMIT),
        "residence_country": _text(profile.get("country")),
        "preferred_destination_country": _text(profile.get("destination_country")),
        "other_destination_countries": _strings(profile.get("destination_countries"), limit=6),
        "available_from": _text(profile.get("available_from")),
        "documents": _documents(row, profile),
    }
    # An empty key is noise in a 32k-token budget and reads to a model as a
    # field that was asked and answered with nothing. Absent is the honest
    # shape for "the record does not say".
    return {key: value for key, value in state.items() if value not in (None, "", [], {})}


def assert_no_identifiers(state: Dict[str, Any]) -> None:
    """Raise if a state carries a field that must not leave this system.

    Called on every request in `runner`, not only in tests: the cost of the
    check is a dictionary walk, and the cost of missing it is a passport number
    in somebody else's logs.
    """
    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in FORBIDDEN_FIELDS:
                    raise ValueError(f"{path}{key} must not be sent to an external API")
                walk(value, f"{path}{key}.")
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, path)

    walk(state, "")


def evidence_fingerprint(row: Dict[str, Any]) -> str:
    """A digest of the evidence, so a changed candidate is re-evaluated.

    Over the state rather than over `updated_at`: a recruiter correcting a
    phone number has not changed anything a match turns on, and recomputing the
    shortlist for it would be money spent to produce the same answer.
    """
    payload = json.dumps(candidate_state(row), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def excerpt_for(row: Dict[str, Any], terms: Iterable[str], limit: int = 180) -> Optional[Dict[str, str]]:
    """The line of the candidate record that supports a term, and its field.

    Taken from the record, never from the model: an excerpt is meant to be the
    thing a recruiter checks the decision against, so it has to be text that is
    provably on file — and naming the field says where to look.
    """
    wanted = [term.lower() for term in terms if _text(term)]
    if not wanted:
        return None
    profile = _profile_of(row)

    lines: List[tuple[str, str]] = []
    for field_name in ("skills", "technical_skills", "trade_skills", "certifications"):
        values = _strings(profile.get(field_name))
        if values:
            lines.append((f"profile.{field_name}", ", ".join(values)))
    for index, role in enumerate(_roles(profile)):
        line = " · ".join(value for value in role.values() if value)
        if line:
            lines.append((f"profile.work_experience[{index}]", line))
    for field_name in ("current_designation", "course_or_trade", "job_title"):
        value = _text(profile.get(field_name))
        if value:
            lines.append((f"profile.{field_name}", value))
    summary = _clip(profile.get("resume_summary"), _SUMMARY_LIMIT)
    if summary:
        lines.extend(("profile.resume_summary", sentence.strip())
                     for sentence in summary.split(".") if sentence.strip())

    for field_name, line in lines:
        lowered = line.lower()
        if any(term in lowered for term in wanted):
            return {"field": field_name, "excerpt": _clip(line, limit)}
    return None
