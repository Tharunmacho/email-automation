"""Stage-1 resume detection — cheap, no AI, no downloads.

Goal: quickly discard obvious non-resume mail (OTPs, newsletters, invoices,
notifications) before we spend money on downloads, OCR, and the LLM. The final
say still belongs to the AI (``is_resume`` in the profile), but this filter keeps
the volume — and cost — down.

Signals:
  * Must have at least one attachment of a resume-friendly file type.
  * Sender not on the ignore-list (no-reply, billing, notifications, …).
  * Promotional/transactional subject patterns lower the score.
  * Resume-ish filenames / subjects raise the score.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List

from app.config import settings
from app.core.models import Attachment, EmailMessage
from app.logging_config import get_logger

log = get_logger(__name__)

_RESUME_KEYWORDS = re.compile(
    r"\b(resume|résumé|cv|curriculum\s*vitae|candidate|applicant|application|"
    r"apply(ing)?|job|position|profile|hiring|vacancy|opening)\b",
    re.IGNORECASE,
)
_PROMO_SUBJECT = re.compile(
    r"\b(otp|one[-\s]?time\s*password|verify|verification code|newsletter|unsubscribe|"
    r"invoice|receipt|order|payment|sale|discount|offer|deal|promo|"
    r"notification|alert|reminder|statement|subscription)\b",
    re.IGNORECASE,
)


@dataclass
class DetectionResult:
    is_candidate: bool
    score: float
    reason: str
    resume_attachments: List[Attachment]


def _sender_ignored(from_addr: str) -> bool:
    addr = (from_addr or "").lower()
    return any(frag in addr for frag in settings.ignore_sender_fragments)


def _resume_type_attachments(attachments: List[Attachment]) -> List[Attachment]:
    keep: List[Attachment] = []
    allowed = {e.lower() for e in settings.resume_extensions}
    for att in attachments:
        ext = os.path.splitext(att.filename)[1].lower()
        if ext in allowed:
            keep.append(att)
    return keep


def detect(email: EmailMessage) -> DetectionResult:
    resume_atts = _resume_type_attachments(email.attachments)

    if not resume_atts:
        return DetectionResult(False, 0.0, "no resume-type attachment", [])

    if _sender_ignored(email.from_addr):
        return DetectionResult(False, 0.1, f"sender ignored ({email.from_addr})", resume_atts)

    score = 0.5  # baseline: a document/image attachment from a real sender
    reasons: list[str] = ["has document attachment"]

    subject = email.subject or ""
    filenames = " ".join(a.filename for a in resume_atts)

    if _RESUME_KEYWORDS.search(subject) or _RESUME_KEYWORDS.search(filenames):
        score += 0.35
        reasons.append("resume keyword present")

    if _RESUME_KEYWORDS.search(email.body_text or ""):
        score += 0.1
        reasons.append("resume keyword in body")

    if _PROMO_SUBJECT.search(subject):
        score -= 0.4
        reasons.append("promotional/transactional subject")

    is_candidate = score >= 0.5
    result = DetectionResult(is_candidate, round(score, 2), "; ".join(reasons), resume_atts)
    log.info(
        "Detector: msg=%s candidate=%s score=%.2f (%s)",
        email.message_id, is_candidate, result.score, result.reason,
    )
    return result
