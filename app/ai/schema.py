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
            "phone": {"type": ["string", "null"], "description": "Primary mobile/phone number."},
            "phone_numbers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Every phone number on the resume, including the primary one.",
            },
            "location": {"type": ["string", "null"]},
            "city": {"type": ["string", "null"]},
            "country": {"type": ["string", "null"]},
            "skills": {"type": "array", "items": {"type": "string"}},
            "technical_skills": {"type": "array", "items": {"type": "string"}},
            "trade_skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Specific machinery, equipment and trade operations the candidate "
                    "names — e.g. 'EOT Crane', 'CNC Lathe', 'TIG Welding', 'Pipe Fitting'."
                ),
            },
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
                        "board_or_university": {
                            "type": ["string", "null"],
                            "description": "Awarding board or university, when named separately from the school.",
                        },
                        "degree": {"type": ["string", "null"]},
                        "field_of_study": {"type": ["string", "null"]},
                        "start_date": {"type": ["string", "null"]},
                        "end_date": {"type": ["string", "null"]},
                        "passing_year": {"type": ["string", "null"]},
                        "grade": {"type": ["string", "null"]},
                    },
                },
            },
            "certifications": {"type": "array", "items": {"type": "string"}},
            "licenses": {
                "type": "array",
                "description": (
                    "Trade licences, safety cards and certificates that carry an "
                    "identifying number — keep the number, it is what makes the "
                    "credential verifiable."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "number": {"type": ["string", "null"]},
                        "issuer": {"type": ["string", "null"]},
                        "issue_date": {"type": ["string", "null"]},
                        "expiry_date": {"type": ["string", "null"]},
                    },
                },
            },
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
    "You are an expert resume parser and document intelligence engine. You are "
    "given raw text extracted from a candidate's application document (possibly "
    "noisy OCR output, possibly pages selected out of a larger bundle that also "
    "held certificates, ID scans and experience letters).\n"
    "\n"
    "VERIFY FROM CONTENT, NEVER FROM THE FILENAME. A file called 'cv.pdf' whose "
    "text is an invoice, hall ticket, admit card, payment receipt, OTP or any "
    "other non-resume document is NOT a resume: set is_resume=false, set "
    "confidence below 0.30, and leave every profile field empty. A genuine "
    "resume shows a name with contact details, and work history, trade skills or "
    "education.\n"
    "\n"
    "EXTRACT EVERYTHING. Do not truncate, summarise away or drop any candidate "
    "detail that is present: every phone number, every employer with its "
    "designation, location and dates, every responsibility, every trade skill and "
    "piece of machinery named, every qualification with its board/university and "
    "passing year, and every certificate or licence WITH its number. Blue-collar "
    "and trade profiles matter as much as technical ones — record 'EOT Crane "
    "Operator', 'TIG Welding' and 'Pipe Fitting' with the same care as a "
    "programming language.\n"
    "\n"
    "Never invent facts: if a field is absent, use null or an empty list. "
    "Normalise obvious OCR errors in names/emails/phones when confident. "
    "Set confidence to reflect how complete and unambiguous the extracted profile "
    "is. Always respond by calling the record_candidate_profile tool exactly once."
)
