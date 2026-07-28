"""Contextual Email Reply Generator based on Extracted Candidate Profile.

Generates a natural, professional acknowledgment response tailored to candidate details,
properly handling general/speculative resume submissions and filtering noise.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.config import settings
from app.core.models import CandidateProfile, EmailMessage
from app.logging_config import get_logger

log = get_logger(__name__)

# Keywords that indicate student/education status rather than a specific job role
_STUDENT_DESIG_KEYWORDS = {
    "student", "undergraduate", "postgraduate", "fresher", "intern",
    "pursuing", "graduate", "candidate", "b.tech", "b.e", "b.s", "m.s",
    "btech", "degree", "engineering student", "computer science student"
}

# Keywords to filter noise out of the technical skills list
_SKILL_IGNORE_PATTERNS = re.compile(
    r"\b(degree|engineering|university|college|school|simats|saveetha|student|undergraduate|graduate|gpa|cgpa|\d{4})\b",
    re.IGNORECASE,
)


def _clean_skills(skills_list: List[str]) -> List[str]:
    cleaned = []
    seen = set()
    for item in skills_list:
        if not isinstance(item, str):
            continue
        # Strip trailing punctuation/whitespace
        s = item.strip(" \t\n\r.,;-:")
        if not s:
            continue
        # Filter out education/date noise
        if _SKILL_IGNORE_PATTERNS.search(s):
            continue
        # Skip overly long strings (likely sentence fragments or degrees)
        if len(s) > 35 or len(s.split()) > 4:
            continue

        key = s.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(s)
    return cleaned


def _is_student_or_generic_designation(designation: Optional[str]) -> bool:
    if not designation:
        return True
    d_lower = designation.lower()
    if any(kw in d_lower for kw in _STUDENT_DESIG_KEYWORDS):
        return True
    if len(designation.split()) > 5:
        return True
    return False


def generate_contextual_reply(
    profile: CandidateProfile,
    email: Optional[EmailMessage] = None,
) -> str:
    """Generate a natural, personalized, contextual reply email based on candidate details."""
    # 1. Determine candidate name
    raw_name = (profile.full_name or (email.from_name if email else None) or "Applicant").strip()
    name = raw_name.title() if raw_name.isupper() else raw_name

    # 2. Determine designation / role context
    designation = profile.current_designation
    if not designation and profile.work_experience:
        designation = profile.work_experience[0].designation

    is_generic = _is_student_or_generic_designation(designation)

    # 3. Clean and extract valid technical skills
    raw_skills = profile.skills + profile.technical_skills
    clean_skills = _clean_skills(raw_skills)

    skills_text = ""
    if clean_skills:
        top_skills = clean_skills[:4]
        if len(top_skills) > 1:
            skills_str = ", ".join(top_skills[:-1]) + f" and {top_skills[-1]}"
        else:
            skills_str = top_skills[0]
        skills_text = f"We noted your technical background in {skills_str}."

    # 4. Experience highlight
    exp_text = ""
    if profile.total_experience_years is not None and profile.total_experience_years > 0:
        years = f"{profile.total_experience_years:g}"
        exp_text = f" Your {years} years of experience stand out."

    # 5. Assemble natural, professional paragraphs
    paragraphs = [f"Dear {name},"]

    if not is_generic and designation:
        paragraphs.append(
            f"Thank you for sharing your resume with us for {designation.strip()} opportunities."
        )
    else:
        paragraphs.append(
            "Thank you for reaching out and sharing your resume with our recruitment team."
        )

    context_para = f"{skills_text}{exp_text}".strip()
    if context_para:
        paragraphs.append(
            f"{context_para} Our hiring team is currently evaluating your profile to identify suitable opportunities."
        )
    else:
        paragraphs.append(
            "Our hiring team is currently evaluating your profile to identify suitable opportunities."
        )

    paragraphs.append(
        "If your background matches an active role, we will contact you directly regarding the next steps."
    )
    paragraphs.append(f"{settings.auto_reply_signature}")

    reply_body = "\n\n".join(paragraphs)
    log.info("Generated contextual reply for candidate '%s'", name)
    return reply_body
