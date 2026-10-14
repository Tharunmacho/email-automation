"""Plain-language explanations for documents already classified as non-CVs."""
from __future__ import annotations

import re
import unicodedata

from app.extraction import page_classifier as pc


def not_resume_reason(extracted, fallback: str) -> str:
    # A resume missing contact details or failing another validation has its
    # own specific reason. Do not relabel it as a different document type.
    if extracted is None or getattr(extracted, "is_resume", None) is not False:
        return fallback

    text = unicodedata.normalize("NFKC", getattr(extracted, "text", "") or "")
    text = re.sub(r"\s+", " ", text).casefold()
    # A CV can mention a visa. Only describe an already-rejected document as
    # a visa when several actual form fields identify it.
    visa_fields = set(re.findall(r"\bvisa\s+(no\b|number\b|type\b|fees\b)", text))
    kind = ""
    if len(visa_fields) >= 2:
        # PDF rows can glue an Arabic label directly to its English value.
        # ASCII word boundaries separate those scripts without matching a
        # substring such as "homework" as the visa type.
        work = re.search(
            r"\bwork\b.{0,60}\bvisa\s+type\b|\bvisa\s+type\b.{0,30}\bwork\b",
            text, flags=re.ASCII,
        )
        kind = "work visa" if work else "visa"
    else:
        kinds = {getattr(page, "kind", "") for page in getattr(extracted, "pages", []) or []}
        kinds.discard(pc.BLANK)
        labels = {
            pc.PASSPORT: "passport", pc.AADHAAR: "Aadhaar document",
            pc.ID_DOCUMENT: "identity document", pc.DOCUMENT: "identity document",
            pc.CERTIFICATE: "certificate", pc.EXPERIENCE_LETTER: "experience letter",
        }
        if len(kinds) == 1:
            kind = labels.get(next(iter(kinds)), "")
    if kind:
        article = "an" if kind[0].lower() in "aeiou" else "a"
        reason = f"This attachment is {article} {kind}, not a resume/CV."
    else:
        reason = "No resume/CV content was found in this attachment."
    return f"{reason} Only resumes and CVs are ingested."
