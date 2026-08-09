"""Turn extracted resume text into a structured CandidateProfile via Claude.

Uses Anthropic tool-use with a forced tool call, so the model must return JSON
matching our schema. We validate that JSON into a CandidateProfile pydantic model.
"""
from __future__ import annotations

import re

from app.ai.schema import RESUME_TOOL_NAME, RESUME_TOOL_SCHEMA, SYSTEM_PROMPT
from app.config import settings
from app.core.exceptions import AIParseError
from app.core.models import CandidateProfile
from app.logging_config import get_logger

log = get_logger(__name__)

# Guard against runaway token usage / model context limits on huge resumes.
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
    r"|strength|language|declaration|reference|hobb|interest|personal|qualification",
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
            # Extract section lines specifically from work experience section
            sections_temp = extract_sections(text)
            exp_lines = sections_temp.get("experience") or sections_temp.get("work_experience") or []
            if exp_lines:
                clean_exp_desc = " ".join(exp_lines[:5]).strip()
                work_list.append(WorkExperience(
                    designation=lines[0] if lines else "Candidate Role",
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

        # Never invent a designation. An empty field is honest; a wrong one
        # silently corrupts search, matching and the profile screen.
        current_des = work_list[0].designation if work_list else None
        current_comp = work_list[0].company if work_list else None

        # The heuristic never populated languages, even when they sat in the
        # text under a LANGUAGES heading. Reuse the same section reader and
        # vocabulary check the Veris path uses.
        sections = extract_sections(text)
        heuristic_languages = _languages_from_lines(sections.get("languages", []))
        if not heuristic_languages:
            heuristic_languages = _languages_from_lines([text])

        heuristic_certs = _collect_strings(sections.get("certifications", []))
        if not skills_list:
            skills_list = _collect_strings(sections.get("skills", []), min_len=2)
        if not achievements_list:
            achievements_list = _collect_strings(
                sections.get("achievements", []) + sections.get("hobbies", [])
            )

        # A summary is a summary, not the whole document. Prefer the resume's
        # own objective section; never dump raw OCR into the field.
        objective = " ".join(sections.get("objective", [])).strip()
        summary_text = objective or (clean_summary or "")
        if len(summary_text) > 600 and not objective:
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
            linkedin_url=linkedin_url,
            github_url=github_url,
            skills=skills_list,
            technical_skills=skills_list,
            languages=heuristic_languages,
            work_experience=work_list,
            education=education_list,
            certifications=heuristic_certs,
            projects=project_list,
            achievements=achievements_list,
            current_designation=current_des,
            current_company=current_comp,
            resume_summary=summary_text[:600] or None,
            raw_ocr=raw_ocr_fallback,
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

                    import socket
                    old_timeout = socket.getdefaulttimeout()
                    try:
                        socket.setdefaulttimeout(180)
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(_do_veris)
                            res = future.result(timeout=180.0)
                    finally:
                        socket.setdefaulttimeout(old_timeout)

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
                    # This used to be a quiet warning, so a Veris outage looked
                    # identical to a successful parse — the record just silently
                    # came back with heuristic-grade (often wrong) data.
                    log.error(
                        "Veris Resume API FAILED for '%s' (%s: %s). Falling back to the "
                        "heuristic parser, which extracts far less. Profile quality for "
                        "this candidate will be degraded.",
                        filename, type(exc).__name__, exc,
                    )

        # Fall back to local text parsing.
        profile = self.parse_text_fallback(extracted.text, hint=filename)
        # Mark the provenance so a degraded profile is identifiable downstream
        # rather than being indistinguishable from a full Veris extraction.
        info = dict(profile.additional_info or {})
        info.setdefault("extraction_source", "heuristic_fallback")
        profile.additional_info = info
        return profile, extracted


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
                start_date=e.get("start_date") or e.get("start"),
                end_date=e.get("end_date") or e.get("end"),
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
    page_text_parts = [veris_text] if veris_text else []
    pages_list = data.get("pages") or getattr(res, "pages", [])
    for page in pages_list:
        decol = decolumnize_ocr_page(page)
        if decol:
            page_text_parts.append(decol)

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

    if not projects_list and sections.get("projects"):
        for line in sections.get("projects", []):
            if _is_meaningful(line, 5):
                projects_list.append(Project(name=line[:80], description=line))

    if not work_list and sections.get("experience"):
        for line in sections.get("experience", []):
            if _is_meaningful(line, 5):
                work_list.append(WorkExperience(designation=line[:80], description=line))

    if not education_list and sections.get("education"):
        edu_lines = sections.get("education", [])
        if edu_lines:
            inst = edu_lines[0] if len(edu_lines) > 0 else "N/A"
            deg = edu_lines[1] if len(edu_lines) > 1 else None
            education_list.append(Education(institution=inst, degree=deg))

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
        summary = " ".join(objective_lines)[:600]
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

    if hasattr(res, "model_dump"):
        raw_ocr_dict = res.model_dump(mode="python")
    elif hasattr(res, "__dict__"):
        raw_ocr_dict = dict(res.__dict__)
    elif isinstance(res, dict):
        raw_ocr_dict = dict(res)
    else:
        raw_ocr_dict = {}

    if veris_text and isinstance(raw_ocr_dict, dict):
        raw_ocr_dict.setdefault("extracted_text", veris_text)

    return CandidateProfile(
        is_resume=True,
        confidence=1.0,
        full_name=name,
        email=email,
        phone=phone,
        location=location,
        skills=skills_list,
        technical_skills=skills_list,
        languages=languages_list,
        work_experience=work_list,
        education=education_list,
        certifications=certifications_list,
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
