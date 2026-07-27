"""JSON schema for the resume-extraction tool.

We force Claude to call a single tool whose ``input_schema`` mirrors
``CandidateProfile``. Tool use guarantees valid JSON matching this shape, so we
never parse free-form model text. Keep this in sync with core.models.CandidateProfile.
"""
from __future__ import annotations

RESUME_TOOL_NAME = "record_candidate_profile"

RESUME_TOOL_SCHEMA = {
    "name": RESUME_TOOL_NAME,
    "description": (
        "Record the structured candidate profile extracted from resume text. "
        "Call this exactly once. If the provided text is clearly NOT a resume "
        "(e.g. an invoice, newsletter, receipt, OTP, or unrelated document), set "
        "is_resume=false and confidence accordingly, and leave profile fields empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_resume": {
                "type": "boolean",
                "description": "True only if this text is a candidate's resume/CV.",
            },
            "confidence": {
                "type": "number",
                "description": "0.0–1.0 confidence that this is a genuine resume.",
            },
            "full_name": {"type": ["string", "null"]},
            "email": {"type": ["string", "null"]},
            "phone": {"type": ["string", "null"]},
            "location": {"type": ["string", "null"]},
            "skills": {"type": "array", "items": {"type": "string"}},
            "technical_skills": {"type": "array", "items": {"type": "string"}},
            "languages": {"type": "array", "items": {"type": "string"}},
            "work_experience": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": ["string", "null"]},
                        "designation": {"type": ["string", "null"]},
                        "start_date": {"type": ["string", "null"]},
                        "end_date": {"type": ["string", "null"]},
                        "location": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                    },
                },
            },
            "education": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "institution": {"type": ["string", "null"]},
                        "degree": {"type": ["string", "null"]},
                        "field_of_study": {"type": ["string", "null"]},
                        "start_date": {"type": ["string", "null"]},
                        "end_date": {"type": ["string", "null"]},
                        "grade": {"type": ["string", "null"]},
                    },
                },
            },
            "certifications": {"type": "array", "items": {"type": "string"}},
            "projects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                        "technologies": {"type": "array", "items": {"type": "string"}},
                        "url": {"type": ["string", "null"]},
                    },
                },
            },
            "achievements": {"type": "array", "items": {"type": "string"}},
            "linkedin_url": {"type": ["string", "null"]},
            "github_url": {"type": ["string", "null"]},
            "portfolio_url": {"type": ["string", "null"]},
            "current_company": {"type": ["string", "null"]},
            "current_designation": {"type": ["string", "null"]},
            "total_experience_years": {"type": ["number", "null"]},
            "resume_summary": {
                "type": ["string", "null"],
                "description": "A 2–3 sentence neutral summary of the candidate.",
            },
            "additional_info": {
                "type": "object",
                "description": "Any other relevant candidate info that doesn't fit above.",
            },
        },
        "required": ["is_resume", "confidence"],
    },
}

SYSTEM_PROMPT = (
    "You are an expert technical recruiter and resume-parsing engine. "
    "You are given raw text extracted from a document that may be a candidate's "
    "resume (possibly noisy OCR output). Extract accurate structured data. "
    "Never invent facts: if a field is absent, use null or an empty list. "
    "Normalise obvious OCR errors in names/emails/phones when confident. "
    "Always respond by calling the record_candidate_profile tool exactly once."
)
