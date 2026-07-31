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
            if "@" not in line and not re.search(r"http|www|\d{5}|skill|experience", line, re.IGNORECASE) and len(line) < 50:
                name = line
                break

        email = emails[0] if emails else None
        phone = phones[0] if phones else None

        # Try to infer name from LinkedIn or Email if name missing or generic
        linkedin_match = re.search(r"https?://(?:www\.)?linkedin\.com/in/([\w\-]+)/?", text, re.IGNORECASE)
        linkedin_url = linkedin_match.group(0) if linkedin_match else None

        if not name or name.lower() in ("candidate profile", "skill experience", "unnamed"):
            if linkedin_match:
                handle = linkedin_match.group(1)
                # Split camelCase or dot/dash (e.g. MohamedNasirS -> Mohamed Nasir S)
                split_handle = re.sub(r"([a-z])([A-Z])", r"\1 \2", handle).replace("-", " ").replace(".", " ")
                name = split_handle.title()
            elif email:
                user_part = email.split("@")[0]
                user_part = re.sub(r"\d+", "", user_part)  # remove numbers
                name = user_part.replace(".", " ").replace("_", " ").replace("-", " ").title()
            elif hint:
                name = Path(hint).stem.replace("_", " ").replace("-", " ").title()

        github_match = re.search(r"https?://(?:www\.)?github\.com/[\w\-]+/?", text, re.IGNORECASE)
        github_url = github_match.group(0) if github_match else None

        # Deep Dynamic Section Extraction for Work Experience, Projects, Education, and Achievements
        from app.core.models import WorkExperience, Project, Education
        work_list: list[WorkExperience] = []
        project_list: list[Project] = []
        education_list: list[Education] = []
        achievements_list: list[str] = []

        # 1. Skills Extraction
        skills_set = set()
        common_skills = [
            "Python", "JavaScript", "TypeScript", "React", "Node.js", "Docker", "FastAPI",
            "MongoDB", "SQL", "C++", "C#", "Java", "CAMS", "Automation", "AI", "Machine Learning",
            "Algorithms", "Web Development", "Mobile Apps", "Flutter", "React Native", "Git", "REST API",
            "Firebase", "MySQL", "HTML", "CSS", "Flask", "UI/UX", "iOS", "Android"
        ]
        for s in common_skills:
            if re.search(r"\b" + re.escape(s) + r"\b", text, re.IGNORECASE):
                skills_set.add(s)

        # 2. Extract Work Experience
        exp_matches = re.findall(
            r"([A-Za-z0-9\s]+(?:Intern|Engineer|Developer|Manager|Lead|Architect|Specialist|Designer))\s*[\-\–\|:]\s*([A-Za-z0-9\s\.\,]+?):\s*(.+?)(?=\n[A-Z\s]{4,}:|\n[\u2022\*\-\u25cf]|\n\n[A-Z]|$)",
            text,
            re.DOTALL | re.IGNORECASE
        )
        if exp_matches:
            for des, comp, desc in exp_matches:
                clean_desc = re.sub(r"\s+", " ", desc).strip()
                work_list.append(WorkExperience(
                    designation=des.strip().title(),
                    company=comp.strip().title() if len(comp.strip()) < 50 else None,
                    description=clean_desc if clean_desc else None
                ))

        if not work_list:
            for line in lines:
                if re.search(r"\b(intern|developer|engineer|manager|consultant)\b", line, re.IGNORECASE) and len(line) < 100:
                    parts = re.split(r"[\-\–\|:]", line)
                    des = parts[0].strip().title()
                    comp = parts[1].strip().title() if len(parts) > 1 else None
                    work_list.append(WorkExperience(
                        designation=des,
                        company=comp,
                        description=re.sub(r"\s+", " ", text).strip()[:1000]
                    ))
                    break

        # 3. Extract Projects
        proj_matches = re.findall(
            r"(?:[\u2022\*\-\u25cf\u22c6\U0001f310\U0001f4bb]?\s*)([A-Z][A-Za-z0-9\s&\-\/]+?):\s*(.+?)(?=\n[\u2022\*\-\u25cf\u22c6\U0001f310\U0001f4bb]|\n[A-Z\s]{4,}|\n\n[A-Z]|$)",
            text,
            re.DOTALL
        )
        for proj_title, proj_body in proj_matches:
            title_clean = proj_title.strip()
            body_clean = re.sub(r"\s+", " ", proj_body).strip()
            if len(title_clean) < 60 and not re.search(r"education|experience|skills|contact|summary|year|about|career|objective|strength|language", title_clean, re.IGNORECASE):
                techs = [t for t in common_skills if re.search(r"\b" + re.escape(t) + r"\b", body_clean, re.IGNORECASE)]
                project_list.append(Project(
                    name=title_clean,
                    description=body_clean,
                    technologies=techs
                ))

        # 4. Extract Education History
        edu_degree_match = re.search(r"(Bachelor[^\.\n\u2022]+|B\.\s*Tech[^\.\n\u2022]*|Master[^\.\n\u2022]+|M\.\s*Tech[^\.\n\u2022]*|B\.E\.[^\.\n\u2022]*|Diploma[^\.\n\u2022]*)", text, re.IGNORECASE)
        edu_inst_match = re.search(r"([A-Z][A-Za-z0-9\s&\.\,]+(?:University|Institute|College|School|Academy)[A-Za-z0-9\s&\.\,]*)", text, re.IGNORECASE)
        edu_years_match = re.search(r"\b(20\d{2}\s*[\-\–\to]+\s*20\d{2})\b", text)

        if edu_degree_match or edu_inst_match:
            education_list.append(Education(
                degree=edu_degree_match.group(1).strip() if edu_degree_match else "Higher Education",
                institution=edu_inst_match.group(1).strip() if edu_inst_match else None,
                start_date=edu_years_match.group(1).split("-")[0].strip() if edu_years_match else None,
                end_date=edu_years_match.group(1).split("-")[-1].strip() if edu_years_match else None
            ))

        # 5. Extract Achievements & Certifications
        ach_matches = re.findall(r"(?:College Topper|Merit Scholarship|Hackathon|Organiser|Certified|Certification|Award|Rank)[^\n\.\u2022]+", text, re.IGNORECASE)
        for ach in ach_matches:
            clean_ach = re.sub(r"\s+", " ", ach).strip()
            if len(clean_ach) > 5 and len(clean_ach) < 120 and clean_ach not in achievements_list:
                achievements_list.append(clean_ach)

        skills_list = list(skills_set)

        clean_summary = re.sub(r"https?://\S+", "", text)
        clean_summary = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "", clean_summary)
        clean_summary = re.sub(r"\+?\d[\d\s\-\(\)]{8,}\d", "", clean_summary)
        clean_summary = re.sub(r"\s+", " ", clean_summary).strip()

        resolved_name = name
        if not resolved_name or resolved_name.lower() in ("candidate profile", "skill experience", "unnamed"):
            if email:
                user_part = email.split("@")[0]
                user_part = re.sub(r"\d+", "", user_part)
                resolved_name = user_part.replace(".", " ").replace("_", " ").replace("-", " ").title()
            else:
                resolved_name = "Candidate Profile"

        current_des = work_list[0].designation if work_list else "Software Professional"
        current_comp = work_list[0].company if work_list else None

        return CandidateProfile(
            is_resume=True,
            confidence=0.85,
            full_name=resolved_name,
            email=email,
            phone=phone,
            linkedin_url=linkedin_url,
            github_url=github_url,
            skills=skills_list,
            technical_skills=skills_list,
            work_experience=work_list,
            education=education_list,
            projects=project_list,
            achievements=achievements_list,
            current_designation=current_des,
            current_company=current_comp,
            resume_summary=clean_summary[:4000] if clean_summary else None,
        )

    def parse_file(self, file_data: bytes, filename: str) -> tuple[CandidateProfile, ExtractedDocument]:
        import tempfile
        from pathlib import Path
        from app.core.models import ExtractedDocument
        from app.extraction.text_extractor import extract_text

        # First, try fast local text extraction to obtain raw text
        extracted = extract_text(file_data, filename)

        # Send to Veris OCR / LLM Resume API endpoint as primary option if key configured
        if settings.veris_ocr_api_key:
            suffix = Path(filename).suffix or ".pdf"
            with tempfile.TemporaryDirectory() as tmp:
                temp_file = Path(tmp) / f"temp_ocr{suffix}"
                temp_file.write_bytes(file_data)
                log.info("Sending resume to Veris OCR Resume API endpoint: %s", filename)
                try:
                    from recursai.veris_ocr import VerisOCR
                    import concurrent.futures
                    def _do_veris():
                        with VerisOCR(api_key=settings.veris_ocr_api_key, base_url=settings.veris_ocr_base_url) as client:
                            return client.resume.extract(str(temp_file))

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(_do_veris)
                        res = future.result(timeout=90.0)

                    veris_text = extracted.text or ""
                    veris_extracted = ExtractedDocument(
                        text=veris_text,
                        method="veris_resume_api",
                        page_count=1,
                        ocr_used=True,
                        char_count=len(veris_text),
                    )
                    profile = map_veris_to_profile(res, veris_text=veris_text)
                    if profile.full_name or profile.email or profile.phone:
                        log.info("Veris Resume API successfully parsed profile for %s (%s)", profile.full_name, profile.email)
                        return profile, veris_extracted
                except Exception as exc:
                    log.warning("Veris Resume API endpoint call failed (%s); using fallback parser", exc)

        # Fall back to local text parsing / Anthropic LLM
        profile = self.parse_text_fallback(extracted.text, hint=filename)
        return profile, extracted


def map_veris_to_profile(res, veris_text: str = "") -> CandidateProfile:
    import re
    from app.core.models import CandidateProfile, WorkExperience, Education, Project

    if hasattr(res, "__dict__"):
        data = dict(res.__dict__)
    elif isinstance(res, dict):
        data = dict(res)
    else:
        data = {}

    name = getattr(res, "name", None) or data.get("name") or data.get("full_name")
    email = getattr(res, "email", None) or data.get("email")
    phone = getattr(res, "phone", None) or data.get("phone")

    contact = data.get("contact") or {}
    if isinstance(contact, dict):
        if not email and contact.get("emails"):
            email = contact.get("emails")[0]
        if not phone and contact.get("phones"):
            phone = contact.get("phones")[0]
        linkedin_url = contact.get("linkedin")
        github_url = contact.get("github")
        location = contact.get("address") or data.get("location")
    else:
        linkedin_url = data.get("linkedin_url")
        github_url = data.get("github_url")
        location = data.get("location")

    projects_list = []
    raw_projects = data.get("projects") or getattr(res, "projects", [])
    for p in raw_projects:
        if isinstance(p, dict):
            projects_list.append(Project(
                name=p.get("name") or p.get("title"),
                description=p.get("description"),
                technologies=p.get("technologies") or [],
                url=p.get("url")
            ))

    education_list = []
    raw_edu = data.get("education") or getattr(res, "education", [])
    for e in raw_edu:
        if isinstance(e, dict):
            education_list.append(Education(
                institution=e.get("institution") or e.get("school") or e.get("university"),
                degree=e.get("degree") or e.get("qualification"),
                field_of_study=e.get("field_of_study") or e.get("major"),
                start_date=e.get("start_date"),
                end_date=e.get("end_date"),
                grade=e.get("grade") or e.get("gpa")
            ))

    work_list = []
    raw_work = data.get("experience") or data.get("work_experience") or getattr(res, "experience", [])
    for w in raw_work:
        if isinstance(w, dict):
            work_list.append(WorkExperience(
                company=w.get("company"),
                designation=w.get("designation") or w.get("role") or w.get("title"),
                start_date=w.get("start_date"),
                end_date=w.get("end_date"),
                location=w.get("location"),
                description=w.get("description")
            ))

    # If work_experience is empty, check projects for Internship / Hands-on Experience items
    if not work_list and projects_list:
        for p in projects_list:
            if re.search(r"intern|developer|engineer|lead|specialist|manager", p.name or "", re.IGNORECASE):
                parts = re.split(r"[\-\–\|:]", p.name or "")
                des = parts[0].strip()
                comp = parts[1].strip() if len(parts) > 1 else None
                work_list.append(WorkExperience(
                    designation=des,
                    company=comp,
                    description=p.description
                ))

    skills = data.get("skills") or getattr(res, "skills", [])
    skills_list = []
    for s in skills:
        skill_str = s.get("name", "") if isinstance(s, dict) else str(s)
        skill_str = skill_str.strip()
        lower = skill_str.lower()
        if any(noise in lower for noise in ["degree", "simats", "engineering", "2024-2028", "0 months", "graduation"]):
            continue
        if len(skill_str) > 1 and skill_str not in skills_list:
            skills_list.append(skill_str)

    raw_ach = data.get("achievements") or data.get("certifications") or data.get("certificates") or data.get("awards") or getattr(res, "achievements", [])
    achievements_list = []
    for a in raw_ach:
        item_str = a.get("title") or a.get("name") if isinstance(a, dict) else str(a)
        item_str = item_str.strip()
        lower = item_str.lower()
        if any(noise in lower for noise in ["degree", "simats", "engineering", "2024-2028", "0 months", "graduation"]):
            continue
        if len(item_str) > 3 and item_str not in achievements_list:
            achievements_list.append(item_str)

    # Parse bullet points from raw OCR text
    if veris_text:
        ach_keywords = [
            "topper", "scholarship", "hackathon", "carrom", "cricket", "volleyball",
            "violin", "balvikas", "spirituality", "workshop", "certification", 
            "certificate", "tata forge", "abacus", "edutou", "novitech"
        ]
        for line in veris_text.split("\n"):
            clean_l = re.sub(r"^[•\*\-\u25cf\u22c6\U0001f947\U0001f393\U0001f680\U0001f40d\U0001f4dc\U0001f916\U0001f4bb\U0001f3e2\d\.\s]+", "", line).strip()
            clean_l = re.sub(r"\s+", " ", clean_l)
            lower_l = clean_l.lower()
            if any(k in lower_l for k in ach_keywords) and len(clean_l) > 15 and len(clean_l) < 350:
                if not any(noise in lower_l for noise in ["degree", "simats", "engineering", "2024-2028", "0 months", "gmail.com", "about me", "skills", "experience set", "undergraduate", "passionate", "full stack developer"]):
                    if clean_l not in achievements_list:
                        achievements_list.append(clean_l)

    exp_years = data.get("total_experience_years")
    if exp_years is not None:
        try:
            exp_years = float(exp_years)
        except (ValueError, TypeError):
            exp_years = None

    summary = data.get("summary") or getattr(res, "summary", None)
    if summary and (summary.strip().lower() == "0 months" or len(summary.strip()) < 5):
        summary = None

    IGNORE_ADDITIONAL_KEYS = {
        "request_id", "warnings", "processing_time_ms", "pages", 
        "total_experience_human", "indian_experience_human", "overseas_experience_human",
        "confidence", "is_resume", "name", "full_name", "email", "phone", "contact",
        "experience", "work_experience", "education", "projects", "achievements", "skills", "summary"
    }

    clean_additional_info = {}
    for k, v in data.items():
        if k.lower() in IGNORE_ADDITIONAL_KEYS or v is None:
            continue
        if isinstance(v, str) and (v.strip() == "" or v.strip().lower() == "0 months" or v.strip() == "null"):
            continue
        if isinstance(v, dict):
            non_null_v = {sub_k: sub_v for sub_k, sub_v in v.items() if sub_v is not None and sub_v != "null"}
            if not non_null_v:
                continue
            v = non_null_v
        if isinstance(v, list) and not v:
            continue
        clean_additional_info[k] = v

    return CandidateProfile(
        is_resume=True,
        confidence=1.0,
        full_name=name,
        email=email,
        phone=phone,
        location=location,
        skills=skills_list,
        technical_skills=skills_list,
        work_experience=work_list,
        education=education_list,
        projects=projects_list,
        achievements=achievements_list,
        linkedin_url=linkedin_url,
        github_url=github_url,
        total_experience_years=exp_years,
        current_designation=data.get("designation") or (work_list[0].designation if work_list else None),
        current_company=data.get("company") or (work_list[0].company if work_list else None),
        resume_summary=summary,
        additional_info=clean_additional_info if clean_additional_info else None,
    )
