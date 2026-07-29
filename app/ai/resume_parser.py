"""Turn extracted resume text into a structured CandidateProfile via Claude.

Uses Anthropic tool-use with a forced tool call, so the model must return JSON
matching our schema. We validate that JSON into a CandidateProfile pydantic model.
"""
from __future__ import annotations

from app.ai.schema import RESUME_TOOL_NAME, RESUME_TOOL_SCHEMA, SYSTEM_PROMPT
from app.config import settings
from app.core.exceptions import AIParseError
from app.core.models import CandidateProfile
from app.logging_config import get_logger

log = get_logger(__name__)

# Guard against runaway token usage / model context limits on huge resumes.
_MAX_INPUT_CHARS = 60_000


class ResumeParser:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key
        self._model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            key = self._api_key or settings.anthropic_api_key
            if not key:
                raise AIParseError("ANTHROPIC_API_KEY is not configured.")
            import anthropic

            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def parse(self, resume_text: str, hint: str = "") -> CandidateProfile:
        return self._parse_via_anthropic(resume_text, hint)

    def _parse_via_anthropic(self, resume_text: str, hint: str = "") -> CandidateProfile:
        text = resume_text.strip()
        if not text:
            raise AIParseError("Empty resume text; nothing to parse.")
        if len(text) > _MAX_INPUT_CHARS:
            log.warning("Truncating resume text from %d to %d chars", len(text), _MAX_INPUT_CHARS)
            text = text[:_MAX_INPUT_CHARS]

        user_content = text
        if hint:
            user_content = f"[Context from email: {hint}]\n\n{text}"

        model_name = self._model or settings.anthropic_model
        try:
            response = self.client.messages.create(
                model=model_name,
                max_tokens=settings.anthropic_max_tokens,
                system=SYSTEM_PROMPT,
                tools=[RESUME_TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": RESUME_TOOL_NAME},
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as exc:  # noqa: BLE001
            raise AIParseError(f"Anthropic API call failed: {exc}") from exc

        tool_input = self._extract_tool_input(response)
        try:
            return CandidateProfile.model_validate(tool_input)
        except Exception as exc:  # noqa: BLE001
            raise AIParseError(f"AI output did not match schema: {exc}") from exc

    @staticmethod
    def _extract_tool_input(response) -> dict:
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == RESUME_TOOL_NAME:
                return block.input
        raise AIParseError("Model did not return the expected tool call.")

    def parse_text_fallback(self, resume_text: str, hint: str = "") -> CandidateProfile:
        if settings.anthropic_api_key:
            try:
                return self._parse_via_anthropic(resume_text, hint)
            except Exception as exc:
                log.warning("Anthropic parsing failed (%s); using heuristic fallback parser", exc)

        import re
        from pathlib import Path
        text = (resume_text or "").strip()
        emails = re.findall(r"[\w\.-]+@[\w\.-]+\.\w+", text)
        phones = re.findall(r"\+?\d[\d\s\-\(\)]{8,}\d", text)
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        name = None
        for line in lines[:5]:
            if "@" not in line and not re.search(r"http|www|\d{5}", line) and len(line) < 50:
                name = line
                break
        if not name and hint:
            name = Path(hint).stem.replace("_", " ").replace("-", " ").title()

        email = emails[0] if emails else None
        phone = phones[0] if phones else None

        return CandidateProfile(
            is_resume=True,
            confidence=0.85,
            full_name=name or "Candidate Profile",
            email=email,
            phone=phone,
            resume_summary=text[:400] if text else "Word/Document resume content extracted.",
        )

    def parse_file(self, file_data: bytes, filename: str) -> tuple[CandidateProfile, ExtractedDocument]:
        import tempfile
        from pathlib import Path
        from recursai.veris_ocr import VerisOCR
        from app.core.models import ExtractedDocument
        from app.extraction.text_extractor import extract_text
        from app.extraction import file_type as ft

        # Try fast local text extraction first (works instantly for .pdf, .docx, .doc, .txt)
        extracted = extract_text(file_data, filename)
        if extracted.text and len(extracted.text.strip()) > 50:
            log.info("Fast local extraction succeeded for %s (%d chars)", filename, len(extracted.text))
            profile = self.parse_text_fallback(extracted.text, hint=filename)
            return profile, extracted

        suffix = Path(filename).suffix or ".pdf"
        with tempfile.TemporaryDirectory() as tmp:
            temp_file = Path(tmp) / f"temp_ocr{suffix}"
            temp_file.write_bytes(file_data)

            log.info("Sending resume to Veris OCR Resume API: %s", filename)
            try:
                import concurrent.futures
                def _do_veris():
                    with VerisOCR(api_key=settings.veris_ocr_api_key, base_url=settings.veris_ocr_base_url) as client:
                        return client.resume.extract(str(temp_file))

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_do_veris)
                    res = future.result(timeout=10.0)

                pages = getattr(res, "pages", [])
                if isinstance(pages, list):
                    extracted_text = "\n".join(
                        page.get("text", "") if isinstance(page, dict) else getattr(page, "text", "")
                        for page in pages
                    )
                else:
                    extracted_text = ""

                extracted = ExtractedDocument(
                    text=extracted_text,
                    method="veris_resume_api",
                    page_count=len(pages),
                    ocr_used=True,
                    char_count=len(extracted_text)
                )

                profile = map_veris_to_profile(res)
                return profile, extracted

            except Exception as exc:
                log.warning("Veris Resume API failed (%s). Falling back to local text extraction.", exc)
                extracted = extract_text(file_data, filename)
                profile = self.parse_text_fallback(extracted.text, hint=filename)
                return profile, extracted


def map_veris_to_profile(res) -> CandidateProfile:
    from app.core.models import CandidateProfile, WorkExperience, Education, Project
    
    # Extract keys safely
    data = res if isinstance(res, dict) else getattr(res, "__dict__", {})
    
    contact = data.get("contact") or {}
    emails = contact.get("emails") or []
    phones = contact.get("phones") or []
    email = emails[0] if emails else None
    phone = phones[0] if phones else None
    linkedin_url = contact.get("linkedin")
    github_url = contact.get("github")
    location = contact.get("address")
    
    projects_list = []
    for p in (data.get("projects") or []):
        projects_list.append(Project(
            name=p.get("name"),
            description=p.get("description"),
            technologies=p.get("technologies") or [],
            url=p.get("url")
        ))
        
    education_list = []
    for e in (data.get("education") or []):
        education_list.append(Education(
            institution=e.get("institution"),
            degree=e.get("degree"),
            field_of_study=e.get("field_of_study"),
            start_date=e.get("start_date"),
            end_date=e.get("end_date"),
            grade=e.get("grade")
        ))
        
    work_list = []
    for w in (data.get("experience") or []):
        work_list.append(WorkExperience(
            company=w.get("company"),
            designation=w.get("designation"),
            start_date=w.get("start_date"),
            end_date=w.get("end_date"),
            location=w.get("location"),
            description=w.get("description")
        ))
        
    skills = data.get("skills") or []
    skills_list = []
    for s in skills:
        if isinstance(s, str):
            skills_list.append(s)
        elif isinstance(s, dict) and "name" in s:
            skills_list.append(s["name"])
        else:
            skills_list.append(str(s))

    exp_years = data.get("total_experience_years")
    if exp_years is not None:
        try:
            exp_years = float(exp_years)
        except (ValueError, TypeError):
            exp_years = None
            
    additional_info = {}
    for k, v in data.items():
        if k in ("name", "contact", "personal_info", "passport_details", "designation", 
                 "highest_qualification", "experience", "skills", "projects", "education", "pages"):
            additional_info[k] = v

    return CandidateProfile(
        is_resume=True,
        confidence=1.0,
        full_name=data.get("name"),
        email=email,
        phone=phone,
        location=location,
        skills=skills_list,
        technical_skills=skills_list,
        work_experience=work_list,
        education=education_list,
        projects=projects_list,
        linkedin_url=linkedin_url,
        github_url=github_url,
        total_experience_years=exp_years,
        current_designation=data.get("designation"),
        additional_info=additional_info
    )
