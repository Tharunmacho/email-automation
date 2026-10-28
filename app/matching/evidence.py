"""One candidate, read for the facts a match turns on.

Everything here comes from the record that is already in Mongo — the profile
the existing OCR and extraction pipeline wrote. Nothing re-reads a CV, nothing
re-runs OCR, and nothing writes back to a candidate document. Nothing leaves
the server either: matching is computed here, not by a third party.

The rule throughout is that *not stated* is its own answer. A blank experience
figure is not zero years, a missing passport date is not an expired passport,
and the functions below return None rather than guess.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SUMMARY_LIMIT = 1200


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _clip(value: Any, limit: int) -> str:
    text = " ".join(_text(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def strings(value: Any, limit: int = 40) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out = [_text(item) for item in value]
    return [item for item in out if item][:limit]


def profile_of(row: Dict[str, Any]) -> Dict[str, Any]:
    profile = row.get("profile")
    return profile if isinstance(profile, dict) else {}


def roles(profile: Dict[str, Any]) -> List[Dict[str, str]]:
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
            "description": _clip(role.get("description"), 400),
        }
        entry = {key: value for key, value in entry.items() if value}
        if entry:
            out.append(entry)
    return out[:8]


def licences(profile: Dict[str, Any]) -> List[Dict[str, str]]:
    """Licences without their numbers: holding one is the question, not its number."""
    out: List[Dict[str, str]] = []
    for licence in profile.get("licenses") or []:
        if not isinstance(licence, dict):
            continue
        entry = {key: _text(licence.get(key)) for key in ("name", "issuer", "expiry_date")}
        if any(entry.values()):
            out.append({key: value for key, value in entry.items() if value})
    return out[:10]


def education(profile: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for item in profile.get("education") or []:
        if isinstance(item, dict):
            text = " ".join(_text(item.get(key)) for key in ("degree", "qualification", "field", "institution"))
            if text.strip():
                out.append(text.strip())
        elif _text(item):
            out.append(_text(item))
    return out[:6]


# --------------------------------------------------------------------------- #
#  Experience
# --------------------------------------------------------------------------- #
def experience_years(row: Dict[str, Any]) -> Optional[float]:
    """Years on the record, or None.

    None is not zero. A profile with no figure has not told us the candidate is
    a fresher — the WhatsApp intake stores a band instead, and a résumé may
    simply not have said. Treating the blank as zero is how a twenty-year
    tradesman fails a three-year floor.
    """
    value = profile_of(row).get("total_experience_years")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def band_range(band: Any) -> Optional[Tuple[float, float]]:
    """(least, most) years a band like "2-5 years", "3_5" or "10+" allows.

    `inf` for an open top. None when there is no band or it cannot be read —
    a band is the candidate's own words, and a figure is never invented from it.
    """
    text = _text(band).lower()
    if not text:
        return None
    if "fresher" in text or text in {"0", "none", "no experience"}:
        return 0.0, 0.0
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return None
    if "+" in text or "above" in text or "more" in text or "over" in text:
        return min(numbers), float("inf")
    if "less" in text or "below" in text or "under" in text or "<" in text:
        return 0.0, max(numbers)
    return min(numbers), max(numbers)


_YEAR = re.compile(r"\b(19[5-9]\d|20\d\d)\b")
_ONGOING = re.compile(r"present|current|till date|to date|now|ongoing", re.I)
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:years?|yrs?)", re.I)


def history_years(row: Dict[str, Any]) -> float:
    """The most experience the dated work history could support.

    The wider of: first start year to last end year, and the stated durations
    added up. Generous on purpose — it is only used to stop a stated figure
    from being trusted when the history plainly says more.
    """
    starts: List[int] = []
    ends: List[int] = []
    durations = 0.0
    this_year = date.today().year
    for role in profile_of(row).get("work_experience") or []:
        if not isinstance(role, dict):
            continue
        start = " ".join(_text(role.get(key)) for key in ("start_date", "from"))
        end = " ".join(_text(role.get(key)) for key in ("end_date", "to"))
        span = " ".join(_text(role.get(key)) for key in ("duration", "period"))
        start_years = [int(y) for y in _YEAR.findall(start)] or [int(y) for y in _YEAR.findall(span)][:1]
        end_years = [int(y) for y in _YEAR.findall(end)] or [int(y) for y in _YEAR.findall(span)][1:]
        if _ONGOING.search(end) or _ONGOING.search(span):
            end_years.append(this_year)
        starts.extend(start_years)
        ends.extend(end_years or start_years)
        match = _DURATION.search(span)
        if match:
            durations += float(match.group(1))
    by_dates = float(max(ends) - min(starts)) if starts and ends else 0.0
    return max(by_dates, durations)


def experience_range(row: Dict[str, Any]) -> Optional[Tuple[float, float, str]]:
    """(least, most, how it was stated), or None when the record does not say.

    A stated figure is exact; a band is a range. When the dated work history
    shows more than the stated figure, the record contradicts itself and the
    range widens to cover both rather than picking one.
    """
    years = experience_years(row)
    history = history_years(row)
    if years is not None:
        if history > years:
            return years, history, f"{years:g} years stated, work history suggests up to {history:g}"
        return years, years, f"{years:g} years on record"
    band = band_range(profile_of(row).get("total_experience_band"))
    if band is not None:
        low, high = band
        if history > high:
            high = history
        return low, high, f"experience band \"{profile_of(row).get('total_experience_band')}\""
    if history > 0:
        return history, history, f"about {history:g} years from the dated work history"
    return None


# --------------------------------------------------------------------------- #
#  Documents
# --------------------------------------------------------------------------- #
def _parse_date(value: Any) -> Optional[date]:
    text = _text(value)
    if not text:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%m/%Y", "%Y-%m"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    match = _YEAR.search(text)
    return date(int(match.group(1)), 12, 31) if match else None


def documents(row: Dict[str, Any]) -> Dict[str, Any]:
    """Readiness, never identifiers: whether a passport is on file, and until when."""
    profile = profile_of(row)
    registration = row.get("registration_state")
    outstanding = strings(registration.get("outstanding_documents"), limit=12) if isinstance(registration, dict) else []
    return {
        "passport_on_file": bool(_text(profile.get("passport_number"))),
        "passport_expiry": _parse_date(profile.get("passport_expiry")),
        "outstanding_documents": outstanding,
        "registration_tracked": isinstance(registration, dict),
    }


# --------------------------------------------------------------------------- #
#  Quoting the record
# --------------------------------------------------------------------------- #
def excerpt_for(row: Dict[str, Any], terms: Iterable[str], limit: int = 180) -> Optional[Dict[str, str]]:
    """The line of the candidate record that supports a term, and its field.

    Taken from the record, so it is text that is provably on file — the thing a
    recruiter checks the score against — and naming the field says where to look.
    """
    wanted = [term.lower() for term in terms if _text(term)]
    if not wanted:
        return None
    profile = profile_of(row)

    lines: List[Tuple[str, str]] = []
    for field_name in ("skills", "technical_skills", "trade_skills", "certifications"):
        values = strings(profile.get(field_name))
        if values:
            lines.append((f"profile.{field_name}", ", ".join(values)))
    for index, licence in enumerate(licences(profile)):
        lines.append((f"profile.licenses[{index}]", " · ".join(licence.values())))
    for index, role in enumerate(roles(profile)):
        line = " · ".join(value for value in role.values() if value)
        if line:
            lines.append((f"profile.work_experience[{index}]", line))
    for field_name in ("current_designation", "job_title", "job_preference", "course_or_trade"):
        value = _text(profile.get(field_name))
        if value:
            lines.append((f"profile.{field_name}", value))
    summary = _clip(profile.get("resume_summary"), _SUMMARY_LIMIT)
    if summary:
        lines.extend(("profile.resume_summary", sentence.strip())
                     for sentence in summary.split(".") if sentence.strip())

    for field_name, line in lines:
        lowered = line.lower()
        if any(re.search(rf"(?<![a-z0-9]){re.escape(term)}", lowered) for term in wanted):
            return {"field": field_name, "excerpt": _clip(line, limit)}
    return None
