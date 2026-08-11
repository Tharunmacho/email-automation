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
    r"\b(resume|résumé|cv|curriculum\s*vitae|bio|biodata|bio[-\s]?data|candidate|applicant|application|"
    r"apply(ing)?|job|position|profile|hiring|vacancy|opening|career|portfolio|writeup|write[-\s]?up|details|"
    r"cook|chef|driver|welder|electrician|worker|operator|engineer|technician|staff|document|doc)\b",
    re.IGNORECASE,
)
_PROMO_SUBJECT = re.compile(
    r"\b(otp|one[-\s]?time\s*password|verify|verification code|newsletter|unsubscribe|"
    r"invoice|receipt|order|payment|sale|discount|promo|"
    r"notification|alert|reminder|statement|subscription|springboard|certificate|"
    r"completion|course|learning)\b",
    re.IGNORECASE,
)

# Words that only ever appear in a subject line because someone is applying for
# a job. `_RESUME_KEYWORDS` is far broader ("job", "career", "details"), too
# broad to overrule anything on its own.
_STRONG_RESUME_SUBJECT = re.compile(
    r"\b(resume|résumé|cv|curriculum\s*vitae|bio[-\s]?data|"
    r"applying\s+for|application\s+for|apply\s+for|cook|chef|driver|welder|engineer|worker)\b",
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


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}


def _resume_type_attachments(attachments: List[Attachment]) -> List[Attachment]:
    """Attachments worth opening. A *type* filter — never a content judge.

    No filename is read for meaning here, deliberately. The old blocklist threw
    away a real candidate whose CV was attached as
    "Asif_mohd_MOTOR WORKSHOP ADMIN.pdf" — "workshop" and "admin" are that man's
    job title. `01.pdf`, `Scan_2026.pdf` and `Doc.pdf` have to be opened for the
    same reason: what a document *is* can only be learned by reading it, and the
    page classifier does exactly that, cheaply, a few lines downstream.

    Images are the one case with a real cost asymmetry — they cannot be read
    without OCR, and every email signature logo arrives as an image attachment.
    They are screened on *size*, which is a property of the file rather than of
    its name.
    """
    keep: List[Attachment] = []
    allowed = {e.lower() for e in settings.resume_extensions}

    for att in attachments:
        ext = os.path.splitext(att.filename)[1].lower()
        if ext not in allowed:
            continue
        if ext in _IMAGE_EXTS and att.size < settings.min_image_attachment_bytes:
            log.info(
                "Ignoring image '%s': %d bytes is below the %d-byte floor for a "
                "legible scanned page (almost certainly a signature logo or icon)",
                att.filename, att.size, settings.min_image_attachment_bytes,
            )
            continue
        keep.append(att)
    return keep


def detect(email: EmailMessage) -> DetectionResult:
    resume_atts = _resume_type_attachments(email.attachments)

    if not resume_atts:
        names = ", ".join(a.filename for a in email.attachments) or "none"
        return DetectionResult(
            False, 0.0, f"no resume-type attachment (saw: {names})", [],
        )

    if _sender_ignored(email.from_addr):
        return DetectionResult(False, 0.1, f"sender ignored ({email.from_addr})", resume_atts)

    score = 0.5  # baseline: a document/image attachment from a real sender
    reasons: list[str] = ["has document attachment"]

    subject = email.subject or ""
    # A resume word in the filename can only ever *raise* the score. It is a
    # hint, never a requirement, and its absence is not evidence of anything.
    filenames = " ".join(a.filename for a in resume_atts)

    if _RESUME_KEYWORDS.search(subject) or _RESUME_KEYWORDS.search(filenames):
        score += 0.35
        reasons.append("resume keyword present")

    if _RESUME_KEYWORDS.search(email.body_text or ""):
        score += 0.1
        reasons.append("resume keyword in body")

    if _PROMO_SUBJECT.search(subject):
        # "Application for Welder - certificates attached" trips the promo list
        # on the word "certificate" and loses 0.4, which sinks a genuine trade
        # application below the cut-off. The promo penalty is for mail with no
        # application signal at all; an explicit "resume"/"application for" in
        # the subject settles the question.
        if _STRONG_RESUME_SUBJECT.search(subject):
            reasons.append("promo word present but subject states an application")
        else:
            score -= 0.4
            reasons.append("promotional/transactional subject")

    # `Scan_2026.pdf` under the subject "Fwd:" scores 0.50 and would never be
    # opened, yet it is exactly the file a candidate sends. So a *document*
    # attachment earns a content inspection on its own: the page classifier
    # reads it and refuses a hall ticket in milliseconds, which is a far better
    # judge than a keyword search over a filename.
    #
    # A promotional subject still blocks it. That is a judgement about the
    # email, not about what the attachment is named.
    promotional = "promotional/transactional subject" in reasons
    has_document = any(
        os.path.splitext(a.filename)[1].lower() not in _IMAGE_EXTS for a in resume_atts
    )
    if settings.inspect_all_documents and has_document and not promotional:
        is_candidate = True
        if score < settings.detector_min_score:
            reasons.append("document opened for content inspection regardless of filename")
    else:
        is_candidate = score >= settings.detector_min_score

    result = DetectionResult(is_candidate, round(score, 2), "; ".join(reasons), resume_atts)
    log.info(
        "Detector: msg=%s candidate=%s score=%.2f (%s)",
        email.message_id, is_candidate, result.score, result.reason,
    )
    return result
