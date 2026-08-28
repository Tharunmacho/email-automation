"""Turn extracted resume text into a structured CandidateProfile via Claude.

Uses Anthropic tool-use with a forced tool call, so the model must return JSON
matching our schema. We validate that JSON into a CandidateProfile pydantic model.
"""
from __future__ import annotations

import copy
import re

from app.ai.schema import RESUME_TOOL_NAME, RESUME_TOOL_SCHEMA, SYSTEM_PROMPT
from app.config import settings
from app.core.exceptions import AIParseError
from app.core.models import CandidateProfile
from app.logging_config import get_logger

log = get_logger(__name__)

# Backstop against runaway token usage / model context limits. Page selection
# upstream is what normally keeps the payload small; this only fires on a single
# résumé that really is this long, so truncating the tail is the lesser loss.
_MAX_INPUT_CHARS = 60_000


# Labels from the contact / personal block. A line like "Mob: 9984013450" is a
# field, not a project, but it matches the same "Title: body" shape.
_CONTACT_LABELS = {
    "mob", "mobile", "phone", "tel", "telephone", "cell", "contact", "email",
    "e mail", "mail", "address", "addr", "dob", "date of birth", "birth",
    "nationality", "gender", "sex", "age", "marital status", "religion",
    "father", "father name", "mother", "mother name", "pin", "pincode",
    "linkedin", "github", "website", "portfolio", "passport", "languages known",
}

_SECTION_WORDS = re.compile(
    r"education|experience|skills|contact|summary|year|about|career|objective"
    r"|strength|language|declaration|reference|hobb|interest|personal|qualification"
    r"|software|certification|welding|specialization|academics|signature|place|date",
    re.IGNORECASE,
)


def _is_project_candidate(title: str, body: str) -> bool:
    """True only when 'Title: body' looks like a real project entry."""
    t = title.strip()
    if not (3 <= len(t) < 60):
        return False
    if t.lower().strip(" .:-") in _CONTACT_LABELS:
        return False
    if _SECTION_WORDS.search(t):
        return False

    b = (body or "").strip()
    if len(b) < 20:
        return False
    # A body that is mostly digits is a phone number or an ID, not a description.
    digits = sum(c.isdigit() for c in b)
    if digits and digits / len(b) > 0.30:
        return False
    if re.match(r"^[\+\(]?\d[\d\s\-\(\)]{7,}", b):
        return False
    return True


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
        raw_phones = re.findall(r"\+?\d[\d\s\-\(\)]{8,}\d", text)
        phones = []
        for p in raw_phones:
            p_clean = p.strip()
            # Skip dates like 13-03-1998 or 1998-03-13
            if re.search(r"^(?:19|20)\d{2}[\-\/]\d{2}[\-\/]\d{2}$|^\d{2}[\-\/]\d{2}[\-\/](?:19|20)\d{2}$", p_clean):
                continue
            phones.append(p_clean)
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        name = None
        HEADER_TITLES = r"^(?:curriculum\s+vitae|curriculum|vitae|resume|biodata|profile\s+summary|personal\s+details|contact\s+details|page\s+\d+|cv)$"
        for line in lines[:8]:
            if re.search(HEADER_TITLES, line.strip(), re.IGNORECASE):
                continue
            if "@" not in line and not re.search(r"http|www|\d{5}|skill|experience", line, re.IGNORECASE) and 2 <= len(line) < 50:
                name = line
                break

        email = emails[0] if emails else None
        phone = phones[0] if phones else None

        # Try to infer name from LinkedIn or Email if name missing or generic
        linkedin_match = re.search(r"https?://(?:www\.)?linkedin\.com/in/([\w\-]+)/?", text, re.IGNORECASE)
        linkedin_url = linkedin_match.group(0) if linkedin_match else None

        if not name or name.lower() in ("candidate profile", "skill experience", "unnamed", "curriculum vitae", "resume", "cv"):
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
        MONTH_NAME = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        DATE_TOKEN = rf"(?:{MONTH_NAME}\s*\d{{4}}|\d{{4}}\s*{MONTH_NAME}|{MONTH_NAME}|\d{{4}}|Present|Current)"
        DATE_RANGE_REGEX = re.compile(rf"({DATE_TOKEN})\s*[\-\–\u2013\u2014\to]+\s*({DATE_TOKEN})", re.IGNORECASE)

        for line in lines:
            if "|" in line:
                m = DATE_RANGE_REGEX.search(line)
                if m:
                    sdate = m.group(1).strip()
                    edate = m.group(2).strip()
                    parts = [p.strip() for p in line.split("|")]
                    comp_loc = parts[0]
                    role = parts[1] if len(parts) > 1 else ""

                    company = comp_loc
                    loc = None
                    if "," in comp_loc:
                        c_parts = comp_loc.rsplit(",", 1)
                        company = c_parts[0].strip()
                        loc = c_parts[1].strip()

                    work_list.append(WorkExperience(
                        company=company,
                        designation=role,
                        title=role,
                        location=loc,
                        start_date=sdate,
                        end_date=edate,
                    ))

        if not work_list:
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
                        title=des.strip().title(),
                        company=comp.strip().title() if len(comp.strip()) < 50 else None,
                        description=clean_desc if clean_desc else None
                    ))

        if not work_list:
            # Extract section lines specifically from work experience section
            sections_temp = extract_sections(text)
            exp_lines = sections_temp.get("experience") or sections_temp.get("work_experience") or []
            if exp_lines:
                clean_exp_desc = " ".join(exp_lines[:5]).strip()
                work_list.append(WorkExperience(
                    designation=name + " Role" if name else "Candidate Role",
                    company=None,
                    description=clean_exp_desc[:500] if clean_exp_desc else None
                ))

        # 3. Extract Projects
        proj_matches = re.findall(
            r"(?:[\u2022\*\-\u25cf\u22c6\U0001f310\U0001f4bb]?\s*)([A-Z][A-Za-z0-9\s&\-\/]+?):\s*(.+?)(?=\n[\u2022\*\-\u25cf\u22c6\U0001f310\U0001f4bb]|\n[A-Z\s]{4,}|\n\n[A-Z]|$)",
            text,
            re.DOTALL
        )
        for proj_title, proj_body in proj_matches:
            title_clean = proj_title.strip()
            body_clean = re.sub(r"\s+", " ", proj_body).strip()
            if _is_project_candidate(title_clean, body_clean):
                techs = [t for t in common_skills if re.search(r"\b" + re.escape(t) + r"\b", body_clean, re.IGNORECASE)]
                project_list.append(Project(
                    name=title_clean,
                    description=body_clean,
                    technologies=techs
                ))

        # 4. Extract Education History
        edu_degree_match = re.search(r"(Bachelor[^\.\n\u2022]+|B\.\s*Tech[^\.\n\u2022]*|Master[^\.\n\u2022]+|M\.\s*Tech[^\.\n\u2022]*|B\.E\.[^\.\n\u2022]*|Diploma[^\.\n\u2022]*)", text, re.IGNORECASE)
        # Space, not \s: \s matches newlines, which let this run across the whole
        # document and capture an entire page as the "institution".
        edu_inst_match = re.search(
            r"([A-Z][A-Za-z0-9 &.,'\-]{0,60}?(?:University|Institute|College|School|Academy)"
            r"[A-Za-z0-9 &.,'\-]{0,40})",
            text,
        )
        edu_years_match = re.search(r"\b(20\d{2}\s*[\-\–\to]+\s*20\d{2}|\d{4})\b", text)

        if edu_degree_match or edu_inst_match:
            deg_str = edu_degree_match.group(1).strip() if edu_degree_match else "Higher Education"
            inst_str = edu_inst_match.group(1).strip() if edu_inst_match else None
            if not inst_str and ("–" in deg_str or "-" in deg_str or " from " in deg_str):
                deg_parts = re.split(r"[\-\–]| from ", deg_str, 1)
                deg_str = deg_parts[0].strip()
                inst_str = deg_parts[1].strip()

            education_list.append(Education(
                degree=deg_str,
                institution=inst_str,
                start_date=edu_years_match.group(1).strip() if edu_years_match else None,
                end_date=None
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

        current_des = work_list[0].designation if work_list else None
        current_comp = work_list[0].company if work_list else None

        sections = extract_sections(text)
        heuristic_languages = _languages_from_lines(sections.get("languages", []))
        if not heuristic_languages:
            heuristic_languages = _languages_from_lines([text])

        heuristic_certs = _collect_strings(sections.get("certifications", []))
        if not skills_list:
            raw_skill_lines = sections.get("skills", [])
            for s_line in raw_skill_lines:
                parts = re.split(r"[,;|•\n]", str(s_line))
                for p in parts:
                    p_clean = p.strip(" .:-")
                    if _is_meaningful(p_clean, min_len=2) and not re.search(r"software:|welding:|specialization:|certification:", p_clean, re.IGNORECASE):
                        if p_clean not in skills_list:
                            skills_list.append(p_clean)
        if not achievements_list:
            achievements_list = _collect_strings(
                sections.get("achievements", []) + sections.get("hobbies", [])
            )

        # A summary is a summary, not the whole document. Prefer the resume's
        # own objective section; never dump raw OCR into the field.
        objective = " ".join(sections.get("objective", [])).strip()
        summary_text = objective or (clean_summary or "")
        if len(summary_text) > 2000 and not objective:
            summary_text = ""

        raw_ocr_fallback = {
            "text": text,
            "pages": [{"page_number": 1, "text": text}],
            "source": "text_fallback",
        }

        # Confidence has to reflect what was actually recognised. This used to
        # return a flat 0.85 for any text at all, so when Veris was down every
        # hall ticket and class schedule came back as a high-confidence resume,
        # was stored, and got auto-replied. Score the evidence instead: contact
        # details plus resume-shaped sections. A document with none of it lands
        # far below `min_ingest_confidence` and is refused.
        confidence = 0.2
        if email:
            confidence += 0.2
        if phone:
            confidence += 0.15
        if skills_list:
            confidence += 0.15
        if education_list:
            confidence += 0.1
        if work_list:
            confidence += 0.1
        if resolved_name:
            confidence += 0.1
        confidence = round(min(confidence, 0.9), 2)

        return CandidateProfile(
            is_resume=True,
            confidence=confidence,
            full_name=resolved_name,
            email=email,
            phone=phone,
            phone_numbers=phones,
            linkedin_url=linkedin_url,
            github_url=github_url,
            skills=skills_list,
            technical_skills=skills_list,
            languages=heuristic_languages,
            work_experience=work_list,
            education=education_list,
            certifications=heuristic_certs,
            licenses=_licenses_from_strings(heuristic_certs),
            projects=project_list,
            achievements=achievements_list,
            current_designation=current_des,
            current_company=current_comp,
            resume_summary=summary_text[:2000] or None,
            raw_ocr=raw_ocr_fallback,
        )

    def parse_file(self, file_data: bytes, filename: str) -> tuple[CandidateProfile, ExtractedDocument]:
        import tempfile
        from pathlib import Path
        from app.core.models import ExtractedDocument
        from app.extraction.text_extractor import extract_text

        # First, try fast local text extraction to obtain raw text. This also
        # locates the résumé inside the file: candidates send bundles, and the CV
        # can start on page 1 or on page 15.
        extracted = extract_text(file_data, filename)

        # The filename claimed a résumé; the content decides. An invoice or a
        # hall ticket named `cv.pdf` stops here, before Veris, before the LLM,
        # before a record is created.
        if extracted.is_resume is False:
            log.info("Rejecting '%s' on content: %s", filename, extracted.classification_reason)
            return (
                _rejected_profile(
                    extracted.classification_reason,
                    extracted.classification_confidence,
                    extracted,
                ),
                extracted,
            )

        # Only the pages that carry candidate profile data go to the parsers.
        # `extracted.text` still holds every page — nothing is discarded, it is
        # simply not paid for twice.
        resume_text = extracted.resume_text
        parse_data, parse_name = self._resume_only_document(file_data, filename, extracted)

        # Kept across the Veris block so a response that came back without a
        # name/email/phone still hands its raw JSON to the fallback profile.
        # Veris answered; dropping its payload would be data loss.
        veris_raw: dict | None = None

        # Send to Veris OCR / LLM Resume API endpoint as primary option if key configured
        if settings.veris_ocr_api_key:
            suffix = Path(parse_name).suffix or ".pdf"
            with tempfile.TemporaryDirectory() as tmp:
                temp_file = Path(tmp) / f"temp_ocr{suffix}"
                temp_file.write_bytes(parse_data)
                log.info("Sending resume to Veris OCR Resume API endpoint: %s", parse_name)
                try:
                    from app.extraction.jobs import AsyncOCRJobClient, current_job_context, content_key, OCRJobError
                    from recursai.veris_ocr.models import ResumeResult

                    # The digest of what is actually being uploaded, always:
                    # the résumé pages are carved out of the bundle here, and
                    # two carves of the same page are not byte-identical, so a
                    # key without it is a promise this call cannot keep.
                    ctx = current_job_context()
                    digest = content_key(parse_data)
                    idempotency_key = ctx.key_for("resume_parse", digest) if ctx else digest
                    
                    with AsyncOCRJobClient() as job_client:
                        handle, outcome = job_client.run(
                            parse_data,
                            parse_name,
                            mode="resume",
                            idempotency_key=idempotency_key,
                            budget_seconds=settings.veris_timeout_seconds
                        )
                        
                        if outcome.timed_out:
                            raise TimeoutError(f"Veris OCR async job timed out after {settings.veris_timeout_seconds}s")
                        
                        if not outcome.succeeded:
                            raise OCRJobError(f"Veris OCR job failed: {outcome.error}")
                        
                        res = ResumeResult.from_dict(outcome.result or {})

                    veris_text = resume_text or ""
                    veris_extracted = extracted.model_copy(update={
                        "text": veris_text,
                        "method": "veris_resume_api",
                        "ocr_used": True,
                        "char_count": len(veris_text),
                    })
                    veris_raw = veris_payload(res) or None
                    profile = map_veris_to_profile(res, veris_text=veris_text)
                    if profile.full_name or profile.email or profile.phone:
                        info = dict(profile.additional_info or {})
                        info["extraction_source"] = "veris_ocr_api"
                        profile.additional_info = info
                        log.info("Veris Resume API successfully parsed profile for %s (%s)", profile.full_name, profile.email)
                        return profile, veris_extracted
                except Exception as exc:
                    # This used to be a quiet warning, so a Veris outage looked
                    # identical to a successful parse — the record just silently
                    # came back with heuristic-grade (often wrong) data.
                    log.error(
                        "Veris Resume API FAILED for '%s' (%s: %s).",
                        filename, type(exc).__name__, exc,
                    )
                    if settings.require_veris_resume:
                        # Fail the attachment rather than store a profile the
                        # heuristic parser guessed at. The email stays
                        # unlabelled, so the next poll extracts it properly
                        # instead of leaving a plausible-looking wrong record
                        # that nobody will ever revisit.
                        raise AIParseError(
                            f"The résumé extraction service could not read "
                            f"'{filename}' ({type(exc).__name__}: {exc}). Leaving the "
                            f"email for the next poll rather than storing a "
                            f"locally-guessed profile."
                        ) from exc
                    log.warning(
                        "Falling back to the heuristic parser, which extracts far less. "
                        "Profile quality for this candidate will be degraded.",
                    )

        # Fall back to local text parsing.
        profile = self.parse_text_fallback(resume_text, hint=filename)
        # Mark the provenance so a degraded profile is identifiable downstream
        # rather than being indistinguishable from a full Veris extraction.
        info = dict(profile.additional_info or {})
        info.setdefault("extraction_source", "heuristic_fallback")
        profile.additional_info = info
        # Veris did answer, it just did not resolve a name/email/phone. Its JSON
        # is still the real extraction and belongs on the record verbatim,
        # rather than being replaced by our synthesised fallback payload.
        if veris_raw:
            profile.raw_ocr = veris_raw
        return profile, extracted

    @staticmethod
    def _resume_only_document(
        file_data: bytes, filename: str, extracted
    ) -> tuple[bytes, str]:
        """The bytes to hand the document parser — résumé pages only, if we can.

        Veris takes a file, not text, so trimming the other documents out of the
        bundle is the only way to keep them out of the extraction *and* to keep
        the request small enough to answer inside its timeout. A 30-page
        application with a two-page CV becomes a two-page PDF.

        This is a parsing-time copy and nothing more. `file_data` is untouched,
        and it is `file_data` that the pipeline stores — the recruiter always
        downloads the full original, every page and scan intact.
        """
        from app.extraction import pdf_pages

        pages = list(extracted.resume_pages or [])
        page_count = extracted.page_count or 0
        if not pages or page_count <= 1 or not file_data.startswith(b"%PDF"):
            return file_data, filename

        # Page numbers are only meaningful if the classified pages line up
        # one-to-one with the PDF's. A cloud OCR that merged or dropped a page
        # would shift every number, and trimming on a shifted number cuts the
        # resume out instead of keeping it.
        if len(extracted.pages) != page_count or pdf_pages.page_count(file_data) != page_count:
            log.info(
                "Page numbering does not line up with '%s' (%d classified vs %d "
                "declared); sending the whole file",
                filename, len(extracted.pages), page_count,
            )
            return file_data, filename

        trimmed = pdf_pages.subset_pdf(file_data, pages)
        if trimmed is None:
            return file_data, filename

        log.info(
            "Trimmed '%s' from %d page(s) to resume page(s) %s before parsing "
            "(the stored original keeps all %d)",
            filename, page_count, sorted(pages), page_count,
        )
        from pathlib import Path

        stem = Path(filename).stem or "resume"
        return trimmed, f"{stem}_resume_pages.pdf"


def _rejected_profile(reason: str, confidence: float | None, extracted) -> CandidateProfile:
    """The profile for a document that is not a résumé.

    Capped below 0.30 so it lands under every ingest gate no matter how the
    thresholds are tuned, and carrying the reason so the skip is explainable
    rather than a silent disappearance.
    """
    score = 0.15 if confidence is None else min(float(confidence), 0.29)
    return CandidateProfile(
        is_resume=False,
        confidence=round(score, 2),
        additional_info={
            "extraction_source": "page_classifier",
            "rejection_reason": reason,
            "page_kinds": {
                str(p.page_number): p.kind for p in getattr(extracted, "pages", []) or []
            },
        },
    )


# --------------------------------------------------------------------------- #
#  Generic value hygiene — deliberately content-agnostic.
#  Anything resume-specific belongs in the extractor, not in a keyword list.
# --------------------------------------------------------------------------- #
_PLACEHOLDER_VALUES = {"", "null", "none", "n/a", "na", "-", "--", "not specified", "unknown"}


def _is_meaningful(value, min_len: int = 3) -> bool:
    """Reject blanks, placeholders and bare durations like '0 months'."""
    if value is None:
        return False
    text = str(value).strip()
    if len(text) < min_len:
        return False
    if text.lower() in _PLACEHOLDER_VALUES:
        return False
    # "0 months", "0 years", "0.0 yrs" carry no information.
    if re.fullmatch(r"0+(\.0+)?\s*(month|months|year|years|yr|yrs)", text.lower()):
        return False
    return True


# The Gulf recruitment corridor plus the usual home countries. Enough to split
# "Chennai, Tamil Nadu, India" into a city and a country without guessing.
_KNOWN_COUNTRIES = {
    "india", "uae", "u.a.e", "united arab emirates", "saudi arabia", "ksa",
    "qatar", "kuwait", "oman", "bahrain", "singapore", "malaysia", "nepal",
    "bangladesh", "sri lanka", "pakistan", "philippines", "indonesia",
    "united kingdom", "uk", "usa", "united states", "canada", "australia",
    "germany", "france", "italy", "netherlands", "south africa", "kenya",
    "nigeria", "egypt", "jordan", "lebanon", "turkey", "russia", "china",
}


def _split_location(location) -> tuple:
    """"Chennai, Tamil Nadu, India" -> ("Chennai", "India").

    Only splits on a country it recognises. A location it cannot resolve stays
    whole on `location` rather than being cut at a guess.
    """
    text = " ".join(str(location or "").split())
    if not text:
        return None, None
    parts = [p.strip(" .") for p in text.split(",") if p.strip(" .")]
    if not parts:
        return None, None

    country = None
    if parts[-1].lower() in _KNOWN_COUNTRIES:
        country = parts.pop()
    if not parts:
        return None, country
    # The city is the first component; the middle ones are state/region.
    city = parts[0] if _is_meaningful(parts[0], min_len=2) else None
    return city, country


def _trade_skills_from(skills: list, text: str) -> list:
    """The machinery and trade operations among everything that was extracted.

    Vocabulary-driven on purpose — same list the page classifier scores with, so
    "EOT Crane" and "TIG Welding" are recognised as the trade credentials they
    are instead of being flattened into a generic skills list.
    """
    from app.extraction.page_classifier import _TRADE_NOUN_RE

    out, seen = [], set()
    for skill in skills or []:
        if _TRADE_NOUN_RE.search(str(skill)) and str(skill).lower() not in seen:
            seen.add(str(skill).lower())
            out.append(str(skill))
    for match in _TRADE_NOUN_RE.finditer(text or ""):
        value = " ".join(match.group(0).split()).title()
        if value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)
    return out


def _evidence_confidence(
    *, name, email, phone, skills, education, work, extras: bool = False
) -> float:
    """Score a profile by what was actually recovered from the document.

    An extractor answering at all is not evidence; the fields it filled are.
    """
    score = 0.30
    if name:
        score += 0.15
    if email:
        score += 0.15
    if phone:
        score += 0.10
    if skills:
        score += 0.10
    if education:
        score += 0.10
    if work:
        score += 0.10
    if extras:
        score += 0.05
    return round(min(score, 0.98), 2)


# A licence number: at least four characters, containing a digit, next to a
# label. Anything looser matches the year in "Certified 2019".
_LICENSE_NUMBER_RE = re.compile(
    r"(?:no\.?|number|#|id|card)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-]{3,})",
    re.IGNORECASE,
)


def _licenses_from_strings(certifications: list) -> list:
    """Split a certification line into a name and the licence number in it."""
    from app.core.models import TradeLicense

    out = []
    for entry in certifications or []:
        text = " ".join(str(entry).split())
        match = _LICENSE_NUMBER_RE.search(text)
        if not match or not any(c.isdigit() for c in match.group(1)):
            continue
        name = text[: match.start()].strip(" .,:;-()") or text
        out.append(TradeLicense(name=name, number=match.group(1).strip()))
    return out


_DURATION_KEY = re.compile(r"experience_(months|years)$", re.IGNORECASE)


def _is_zero_duration(key: str, value) -> bool:
    """True for `*_experience_months` / `*_experience_years` fields equal to 0."""
    if not _DURATION_KEY.search(key or ""):
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _collect_strings(raw, min_len: int = 3) -> list:
    """Normalise a Veris list-of-strings-or-dicts into a deduped string list."""
    out: list = []
    seen = set()
    for item in raw or []:
        if isinstance(item, dict):
            text = item.get("title") or item.get("name") or item.get("value") or ""
        else:
            text = str(item)
        text = " ".join(str(text).split())
        if not _is_meaningful(text, min_len=min_len):
            continue
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out


# --------------------------------------------------------------------------- #
#  Generic section reader.
#
#  Veris returns structured skills/education/experience reliably, but leaves
#  achievements, certifications, languages and the personal-details block inside
#  the raw page text. Rather than matching a hardcoded keyword list (which only
#  ever worked for the one resume it was written against), find the section
#  HEADINGS and take whatever sits under them. Works for any resume that uses
#  conventional headings, in any domain.
# --------------------------------------------------------------------------- #
_SECTION_ALIASES = {
    "achievements": ["achievement", "achievements", "accomplishments", "awards",
                     "awards and achievements", "honors", "honours", "extra curricular",
                     "extracurricular", "activities", "achievements & certificates",
                     "achievements and certificates"],
    "certifications": ["certification", "certifications", "certificates", "courses",
                       "training", "trainings", "licenses"],
    "languages": ["language", "languages", "languages known", "linguistic proficiency"],
    "hobbies": ["hobbies", "interests", "hobbies and interests"],
    "personal": ["personal information", "personal details", "personal profile",
                 "personal data", "about me"],
    "objective": ["objective", "career objective", "summary", "profile summary",
                  "professional summary"],
    "skills": ["skills", "key skills", "core competencies", "competencies",
               "areas of expertise", "strengths", "technical skills", "skill set"],
    "projects": ["projects", "projects & hands-on experience", "projects and hands-on experience",
                 "hands-on experience", "key projects"],
    "education": ["education", "academic background", "qualifications", "academic qualifications"],
    "experience": ["experience", "work experience", "employment", "professional experience",
                   "internship", "internships"],
}

# Headings that end a section without starting one we want.
_OTHER_HEADINGS = [
    "declaration", "references", "contact", "contact info", "contact information",
]

_BULLET = re.compile(r"^[\s\u2022\u25cf\u25aa\u00b7\*\-\u2013\u2014\d\.\)]+")

# Glyphs the OCR invents around icons in styled resumes: ®, 下, ¢, |, box-drawing.
_OCR_JUNK = re.compile(
    r"[\u00a9\u00ae\u2122\u2020\u2021\u00a2\u00a4\u00a7\u00b6"
    r"\u2500-\u257f\u25a0-\u25ff\u2b00-\u2bff\ue000-\uf8ff"
    r"\u3000-\u303f\u4e00-\u9fff]+"
)


def _strip_ocr_junk(text: str) -> str:
    """Remove icon-glyph noise the OCR emits, then collapse whitespace."""
    return " ".join(_OCR_JUNK.sub(" ", text or "").split())


def _heading_of(line: str):
    """Return the canonical section name if this line is a heading, else None."""
    text = re.sub(r"[^a-z\s&/]", "", _strip_ocr_junk(line).lower()).strip()
    if not text or len(text) > 40:
        return None
    for canon, aliases in _SECTION_ALIASES.items():
        if text in aliases:
            return canon
    if text in _OTHER_HEADINGS:
        return "__other__"
    return None


def _stitched_headings(line: str) -> list:
    """Headings from two columns can land on one line ("LANGUAGES  EDUCATION").

    Returns every heading found, in order. NOTE: we deliberately do *not* try to
    then split the body lines by column — those are stitched too, and guessing
    the split point produces confidently wrong values ("Data science" recorded
    as a language). Knowing which sections exist is useful; blindly assigning
    stitched content to them is not.
    """
    cleaned = _strip_ocr_junk(line)
    words = re.sub(r"[^A-Za-z\s&/]", " ", cleaned).split()
    if not words or len(words) > 8:
        return []

    found, i = [], 0
    while i < len(words):
        matched = False
        # Longest-first so "work experience" wins over "experience".
        for size in (3, 2, 1):
            phrase = " ".join(words[i:i + size]).lower()
            if not phrase:
                continue
            for canon, aliases in _SECTION_ALIASES.items():
                if phrase in aliases:
                    found.append(canon)
                    i += size
                    matched = True
                    break
            if matched:
                break
            if phrase in _OTHER_HEADINGS:
                found.append("__other__")
                i += size
                matched = True
                break
        if not matched:
            # A leftover word means this is prose, not a heading row. Without
            # this guard a sentence like "Strong command over English language
            # & grammar" is read as a LANGUAGES heading and swallows the
            # section it actually belongs to.
            return []
    return found


def extract_sections(raw_text: str) -> dict:
    """Split resume text into {section_name: [lines]} using its own headings."""
    sections: dict = {}
    stitched: set = set()
    current: list = []
    for raw_line in (raw_text or "").splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        heads = _stitched_headings(line)
        if heads:
            wanted = [h for h in heads if h != "__other__"]
            for h in wanted:
                sections.setdefault(h, [])
            # Body lines after a stitched heading belong to *every* section it
            # named, because the columns are interleaved on each line. Collect
            # into all of them and flag them, so only vocabulary-validated
            # fields (languages) read from them and free text does not.
            current = wanted
            if len(heads) > 1:
                stitched.update(wanted)
            continue
        if current:
            cleaned = _strip_ocr_junk(_BULLET.sub("", line))
            if _is_meaningful(cleaned, min_len=3):
                for target in current:
                    sections.setdefault(target, []).append(cleaned)
    sections["__stitched__"] = sorted(stitched)
    return sections


# Languages are a finite, closed set — unlike skills, a vocabulary here is
# legitimate rather than a hardcoded guess, and it is what lets us pull
# "Tamil English Telugu" out of a line stitched with education text.
_KNOWN_LANGUAGES = {
    "english", "hindi", "tamil", "telugu", "kannada", "malayalam", "marathi",
    "bengali", "gujarati", "punjabi", "urdu", "odia", "oriya", "assamese",
    "sanskrit", "konkani", "maithili", "nepali", "sindhi", "kashmiri", "bhojpuri",
    "arabic", "french", "german", "spanish", "portuguese", "italian", "dutch",
    "russian", "japanese", "chinese", "mandarin", "cantonese", "korean", "thai",
    "vietnamese", "indonesian", "malay", "filipino", "tagalog", "turkish",
    "persian", "farsi", "hebrew", "swahili", "polish", "swedish", "norwegian",
    "danish", "finnish", "greek", "czech", "hungarian", "romanian", "ukrainian",
}


def _languages_from_lines(lines: list) -> list:
    """Pick real language names out of possibly column-stitched text."""
    out, seen = [], set()
    for line in lines or []:
        for token in re.split(r"[^A-Za-z]+", _strip_ocr_junk(line)):
            key = token.lower()
            if key in _KNOWN_LANGUAGES and key not in seen:
                seen.add(key)
                out.append(token.capitalize())
    return out


def _split_inline(values: list) -> list:
    """"Hindi, English, Tamil" on one line is three languages, not one."""
    out = []
    for v in values:
        parts = re.split(r"[,;/|]| and ", v)
        for part in parts:
            part = part.strip(" .:-")
            if _is_meaningful(part, min_len=2):
                out.append(part)
    return out

def decolumnize_ocr_page(page) -> str:
    """Un-interleave a 2-column OCR page using bbox coordinates of words/lines."""
    if isinstance(page, dict):
        lines = page.get("lines") or []
        raw_text = page.get("text", "")
    else:
        lines = getattr(page, "lines", [])
        raw_text = getattr(page, "text", "")

    if not lines or not isinstance(lines, list):
        return raw_text

    tokens = []
    for line in lines:
        if isinstance(line, dict):
            words = line.get("words") or [line]
            for w in words:
                if isinstance(w, dict) and "text" in w and "bbox" in w:
                    b = w["bbox"]
                    if isinstance(b, dict):
                        tokens.append({
                            "text": str(w["text"]),
                            "x": float(b.get("x", 0)),
                            "y": float(b.get("y", 0)),
                        })
        elif hasattr(line, "words") or hasattr(line, "text"):
            words = getattr(line, "words", [line])
            for w in words:
                text_val = getattr(w, "text", None)
                bbox_val = getattr(w, "bbox", None)
                if text_val and bbox_val:
                    x_val = getattr(bbox_val, "x", 0) if hasattr(bbox_val, "x") else bbox_val.get("x", 0) if isinstance(bbox_val, dict) else 0
                    y_val = getattr(bbox_val, "y", 0) if hasattr(bbox_val, "y") else bbox_val.get("y", 0) if isinstance(bbox_val, dict) else 0
                    tokens.append({"text": str(text_val), "x": float(x_val), "y": float(y_val)})

    if len(tokens) < 10:
        return raw_text

    x_coords = [t["x"] for t in tokens]
    min_x, max_x = min(x_coords), max(x_coords)
    mid_x = (min_x + max_x) / 2.0

    left_tokens = [t for t in tokens if t["x"] < mid_x]
    right_tokens = [t for t in tokens if t["x"] >= mid_x]

    if len(left_tokens) > len(tokens) * 0.15 and len(right_tokens) > len(tokens) * 0.15:
        def build_col(col_tokens):
            col_tokens.sort(key=lambda t: (round(t["y"] / 15.0) * 15.0, t["x"]))
            lines_out = []
            curr_y = None
            curr_line = []
            for t in col_tokens:
                yb = round(t["y"] / 15.0) * 15.0
                if curr_y is None or abs(yb - curr_y) <= 15.0:
                    curr_line.append(t["text"])
                    curr_y = yb
                else:
                    lines_out.append(" ".join(curr_line))
                    curr_line = [t["text"]]
                    curr_y = yb
            if curr_line:
                lines_out.append(" ".join(curr_line))
            return "\n".join(lines_out)

        return build_col(left_tokens) + "\n\n" + build_col(right_tokens)

    return raw_text


def veris_payload(res) -> dict:
    """The Veris response exactly as the API returned it.

    This is what gets persisted as ``raw_ocr`` and shown on the Raw JSON tab, so
    it must be the untouched body: no keys added, none removed, none renamed.
    ``ResumeResult`` keeps the original under ``_raw_response``; everything else
    falls back to its public attributes (private ``_`` attrs are client
    bookkeeping, never part of the response).
    """
    raw = getattr(res, "_raw_response", None)
    if isinstance(raw, dict) and raw:
        return copy.deepcopy(raw)
    if isinstance(res, dict):
        return copy.deepcopy(res)
    if hasattr(res, "__dict__"):
        return copy.deepcopy({k: v for k, v in res.__dict__.items() if not k.startswith("_")})
    return {}


# Where the career objective ends and the next section begins.
#
# A résumé's opening paragraph runs until the next heading, and on a scan those
# headings routinely arrive on the *same line* as the sentence before them —
# the page is one flowing string by the time it has been de-columnised, so a
# line-based section split cannot see them. They are therefore matched anywhere
# in the text, which makes being precise about what counts as a heading the
# whole difficulty.
#
# Two kinds, and the split between them matters. A multi-word heading
# ("EDUCATIONAL QUALIFICATION", "WORK EXPERIENCE") is unambiguous wherever it
# appears. A bare one — "skills", "education", "experience" — is an ordinary
# English word that objectives are full of: "...develop and update my knowledge
# and skills..." is a sentence, not a heading, and matching it truncated the
# summary mid-thought. So a single generic word only counts when it is punctuated
# like a heading, with a colon after it.
_SECTION_AFTER_OBJECTIVE = re.compile(
    r"\b(?:"
    # Specific enough to be a heading wherever it appears.
    r"educational?\s+qualifications?"
    r"|academic\s+qualifications?"
    r"|education(?:al)?\s+background"
    r"|qualification\s+institution"
    r"|work\s+experience"
    r"|professional\s+experience"
    r"|employment\s+history"
    r"|core\s+qualifications?"
    r"|key\s+skills"
    r"|technical\s+skills"
    r"|personal\s+(?:details|information|profile)"
    r"|languages?\s+known"
    r"|career\s+summary"
    r")\b\s*[:\-]?"
    # Or a generic word, but only when punctuated as a heading.
    r"|\b(?:"
    r"education|experience|skills|certifications?|licen[cs]es|projects?"
    r"|achievements?|declaration|references?|hobbies|strengths?"
    r")\s*:",
    re.IGNORECASE,
)


def _objective_only(summary):
    """The career objective, without the rest of the résumé glued to it.

    The summary on a profile is meant to be the candidate's own opening
    statement. What was being stored instead ran straight on through the next
    heading and into its contents:

        "To work with progressive organization ... growth of organization.
         EDUCATIONAL QUALIFICATION: QUALIFICATION INSTITUTION/UNIVERSITY YEAR
         OF PASSING SSLC ICI GOVT BOYS HR. SEC. SCHOOL, TENKASI 2006 HSC ..."

    — that tail is the Education section, already shown properly in its own
    section from the structured data, repeated here as an unreadable run-on.
    Cutting at the first heading leaves the objective and nothing else.
    """
    text = (summary or "").strip()
    if not text:
        return None
    found = _SECTION_AFTER_OBJECTIVE.search(text)
    # Only when there is a real sentence in front of it: a summary that *starts*
    # with a heading has no objective to keep, and truncating to nothing would
    # lose the little it does say.
    if found and found.start() > 40:
        text = text[: found.start()].strip()
    return text.strip(" .,-:;") or None


def map_veris_to_profile(res, veris_text: str = "") -> CandidateProfile:
    from app.core.models import CandidateProfile, WorkExperience, Education, Project

    if hasattr(res, "__dict__"):
        data = {k: v for k, v in res.__dict__.items() if not k.startswith("_")}
    elif isinstance(res, dict):
        data = dict(res)
    else:
        data = {}

    name = getattr(res, "name", None) or data.get("name") or data.get("full_name")
    if name and isinstance(name, str) and " " in name and len(name.split()) > 4 and all(len(w) == 1 for w in name.split()):
        name = name.replace(" ", "")

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
            proj_kwargs = dict(p)
            proj_kwargs.update({
                "name": p.get("name") or p.get("title"),
                "description": p.get("description"),
                "technologies": p.get("technologies") or [],
                "url": p.get("url"),
            })
            projects_list.append(Project(**proj_kwargs))

    education_list = []
    raw_edu = data.get("education") or getattr(res, "education", [])
    for e in raw_edu:
        if isinstance(e, dict):
            edu_kwargs = dict(e)
            edu_kwargs.update({
                "institution": e.get("institution") or e.get("school") or e.get("university"),
                "degree": e.get("degree") or e.get("qualification"),
                "field_of_study": e.get("field_of_study") or e.get("major"),
                "start_date": e.get("start_date") or e.get("start"),
                "end_date": e.get("end_date") or e.get("end"),
                "grade": e.get("grade") or e.get("gpa"),
            })
            education_list.append(Education(**edu_kwargs))

    work_list = []
    raw_work = data.get("experience") or data.get("work_experience") or getattr(res, "experience", [])
    for w in raw_work:
        if isinstance(w, dict):
            exp_kwargs = dict(w)
            des = w.get("designation") or w.get("role") or w.get("title")
            tit = w.get("title") or w.get("designation") or w.get("role")
            sdate = w.get("start_date") or w.get("start")
            edate = w.get("end_date") or w.get("end")
            exp_kwargs.update({
                "company": w.get("company"),
                "designation": des,
                "title": tit,
                "start_date": sdate,
                "end_date": edate,
                "location": w.get("location"),
                "description": w.get("description"),
            })
            work_list.append(WorkExperience(**exp_kwargs))

    skills = data.get("skills") or getattr(res, "skills", [])
    skills_list = []
    seen_skills = set()
    for s in skills:
        skill_str = s.get("name", "") if isinstance(s, dict) else str(s)
        skill_str = skill_str.strip()
        if not _is_meaningful(skill_str, min_len=2):
            continue
        if skill_str.lower() in seen_skills:
            continue
        seen_skills.add(skill_str.lower())
        skills_list.append(skill_str)

    # Achievements and certifications are distinct fields on the profile; the
    # old mapper collapsed them into one and dropped certifications entirely.
    achievements_list = _collect_strings(
        data.get("achievements") or data.get("awards") or getattr(res, "achievements", [])
    )
    certifications_list = _collect_strings(
        data.get("certifications") or data.get("certificates")
        or getattr(res, "certifications", [])
    )
    personal_info = data.get("personal_info") or {}
    personal_langs = personal_info.get("languages_known") if isinstance(personal_info, dict) else []
    languages_list = _collect_strings(
        data.get("languages") or getattr(res, "languages", []) or personal_langs, min_len=2
    )

    # Veris reliably returns skills/education/experience but usually leaves
    # achievements, certifications and languages in the page text. Recover them
    # from the resume's own section headings — no per-candidate keywords.
    page_text_parts = []
    pages_list = data.get("pages") or getattr(res, "pages", [])
    if pages_list:
        for page in pages_list:
            decol = decolumnize_ocr_page(page)
            if decol:
                page_text_parts.append(decol)
    elif veris_text:
        page_text_parts.append(veris_text)

    page_text = "\n\n".join(page_text_parts)
    sections = extract_sections(page_text)

    stitched = set(sections.get("__stitched__") or [])

    # Free-text sections are only trusted when their heading stood alone. A
    # stitched heading means the lines below carry another column's text.
    if not achievements_list and "achievements" not in stitched:
        achievements_list = _collect_strings(
            sections.get("achievements", []) + sections.get("hobbies", [])
        )
    if not certifications_list and "certifications" not in stitched:
        certifications_list = _collect_strings(sections.get("certifications", []))

    # Experience, projects and education are *structured* fields: Veris either
    # resolves an entry into its parts (company, designation, dates) or reports
    # nothing. We used to backfill them line-by-line from the page text whenever
    # Veris returned an empty list, using the raw line as both the title and the
    # description. On a fresher's resume with no work history that manufactured
    # an entire EXPERIENCE section out of the OCR of the PROJECTS heading —
    # "PR OJE CTS", "AIr QUaLItY InSIGHtS...", each with no dates. Veris shows
    # no experience for that resume because there is none; so do we now.
    #
    # Free-text fields below (languages, skills, achievements, certifications)
    # are different: Veris genuinely leaves those in the page text, and a line
    # under a LANGUAGES heading *is* the value, so recovering them is not a guess.

    if not languages_list:
        # Vocabulary-validated, so a column-stitched line yields the real
        # languages and discards the education text glued onto it.
        languages_list = _languages_from_lines(sections.get("languages", []))
        if not languages_list:
            languages_list = _collect_strings(
                _split_inline(sections.get("languages", [])), min_len=2
            )
    if not skills_list and "skills" not in stitched:
        # Non-technical resumes ("classroom management", "creative teaching")
        # routinely come back with no skills from the extractor.
        skills_list = _collect_strings(sections.get("skills", []), min_len=2)

    # Trade licences and safety cards are the certifications that carry a
    # number, and the number is the whole point of recording them.
    licenses_list = _licenses_from_strings(certifications_list)

    phone_numbers = []
    if isinstance(contact, dict):
        phone_numbers = _collect_strings(contact.get("phones") or [], min_len=6)
    if phone and phone not in phone_numbers:
        phone_numbers.insert(0, phone)

    personal_lines = sections.get("personal", [])
    objective_lines = sections.get("objective", [])

    exp_years = data.get("total_experience_years")
    if exp_years is not None:
        try:
            exp_years = float(exp_years)
        except (ValueError, TypeError):
            exp_years = None

    summary = data.get("summary") or getattr(res, "summary", None)
    if not summary and objective_lines:
        summary = " ".join(objective_lines)[:2000]
    summary = _objective_only(summary)
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
        # Veris always emits the duration counters, at zero for a fresher.
        # "Total experience months: 0", "Indian experience years: 0" and their
        # four siblings are not findings, they are the absence of one.
        if _is_zero_duration(k, v):
            continue
        if isinstance(v, dict):
            non_null_v = {sub_k: sub_v for sub_k, sub_v in v.items() if sub_v is not None and sub_v != "null"}
            if not non_null_v:
                continue
            v = non_null_v
        if isinstance(v, list) and not v:
            continue
        clean_additional_info[k] = v

    # Personal-information blocks (nationality, DOB, marital status, ...) are
    # common on Indian-format resumes and had nowhere to live before.
    if personal_lines:
        details = {}
        for line in personal_lines:
            if ":" in line:
                k2, v2 = line.split(":", 1)
                k2, v2 = k2.strip(), v2.strip()
                if _is_meaningful(k2, 2) and _is_meaningful(v2, 1):
                    details[k2] = v2
        if details:
            clean_additional_info.setdefault("personal_details", details)
        else:
            clean_additional_info.setdefault("personal_details_raw", personal_lines)

    # `raw_ocr` is a verbatim copy of the Veris response — nothing derived, and
    # in particular no `extracted_text` of our own bolted on. The Raw JSON tab
    # renders this dict directly, and it has to match what Veris returned key
    # for key. Our own text lives on `ExtractedDocument`, not in here.
    raw_ocr_dict = veris_payload(res)

    # Confidence used to be a flat 1.0 here, which meant "Veris answered", not
    # "this is a resume". A document the extractor could find almost nothing in
    # was still recorded as a certainty and sailed through every ingest gate.
    confidence = _evidence_confidence(
        name=name, email=email, phone=phone, skills=skills_list,
        education=education_list, work=work_list,
        extras=bool(certifications_list or projects_list or achievements_list),
    )

    # Veris resolves a single free-text address and no trade breakdown, but the
    # profile schema asks for city, country and machinery separately. Both are
    # derived from what Veris already returned — nothing is invented, and an
    # address that cannot be resolved is left whole on `location`.
    city, country = _split_location(location)
    trade_skills = _trade_skills_from(skills_list, page_text)

    return CandidateProfile(
        is_resume=True,
        confidence=confidence,
        full_name=name,
        email=email,
        phone=phone,
        phone_numbers=phone_numbers,
        location=location,
        city=city,
        country=country,
        skills=skills_list,
        technical_skills=skills_list,
        trade_skills=trade_skills,
        languages=languages_list,
        work_experience=work_list,
        education=education_list,
        certifications=certifications_list,
        licenses=licenses_list,
        projects=projects_list,
        achievements=achievements_list,
        linkedin_url=linkedin_url,
        github_url=github_url,
        total_experience_years=exp_years,
        current_designation=data.get("designation") or (work_list[0].designation if work_list else None),
        current_company=data.get("company") or (work_list[0].company if work_list else None),
        resume_summary=summary,
        additional_info=clean_additional_info if clean_additional_info else None,
        raw_ocr=raw_ocr_dict if raw_ocr_dict else None,
    )
