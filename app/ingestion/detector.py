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
from app.extraction import file_type as ft
from app.extraction import page_classifier as pc
from app.logging_config import get_logger

log = get_logger(__name__)

# A pasted CV is at least this long. Below it, the body is a covering note
# ("Please find my resume attached") and there is nothing to parse.
_MIN_BODY_RESUME_CHARS = 400

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


# The one list, shared with the extractor. A second copy here drifted the moment
# `.heic` was added — an iPhone photo of a CV then counted as a document and
# skipped the size floor entirely.
_IMAGE_EXTS = ft.IMAGE_EXTENSIONS


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
        mime = ft.normalize_mime(att.mime_type)

        # Extension *or* declared type. An unnamed inline part reconstructed as
        # `document_1.bin`, and a `.pages` file nobody thought to list, both have
        # to survive this: the extension is a hint about a file, not a fact, and
        # the only thing that settles what a document is, is reading it.
        if ext not in allowed and not ft.is_document_mime(mime):
            log.info(
                "Ignoring '%s': neither its extension (%s) nor its type (%s) "
                "suggests a document or an image",
                att.filename, ext or "none", mime or "none",
            )
            continue

        if _looks_like_image(ext, mime) and att.size < settings.min_image_attachment_bytes:
            log.info(
                "Ignoring image '%s': %d bytes is below the %d-byte floor for a "
                "legible scanned page (almost certainly a signature logo or icon)",
                att.filename, att.size, settings.min_image_attachment_bytes,
            )
            continue
        keep.append(att)
    return keep


def _looks_like_image(ext: str, mime: str) -> bool:
    return ext in _IMAGE_EXTS or mime.startswith("image/")


def _is_document(att: Attachment) -> bool:
    """A non-image attachment — something whose text can be read without OCR."""
    ext = os.path.splitext(att.filename)[1].lower()
    return not _looks_like_image(ext, ft.normalize_mime(att.mime_type))


# Where a CV lives when it did not come as a file. Reported, never fetched.
_CLOUD_LINK_RE = re.compile(
    r"https?://(?:[\w.-]*\.)?(?:drive\.google\.com|docs\.google\.com|dropbox\.com|"
    r"1drv\.ms|onedrive\.live\.com|sharepoint\.com|wetransfer\.com|icloud\.com|"
    r"mega\.nz|box\.com|linkedin\.com/in)/\S*",
    re.IGNORECASE,
)


def _body_as_attachment(email: EmailMessage) -> Attachment | None:
    """The email body itself, as a text attachment, when it reads as a résumé.

    Returned as a synthetic `.txt` attachment rather than handled as a special
    case, so the rest of the pipeline — hashing, the ledger, dedup, storage,
    the LLM — needs to know nothing about where the text came from.
    """
    body = (email.body_text or "").strip()
    if len(body) < _MIN_BODY_RESUME_CHARS:
        return None

    verdict = pc.classify_page(body)
    if verdict.kind != pc.RESUME:
        log.info(
            "Email body of %s is not a resume (kind=%s, score=%.2f)",
            email.message_id, verdict.kind, verdict.score,
        )
        return None

    data = body.encode("utf-8")
    log.info(
        "No attachment on %s, but the body scores as a resume (%.2f) — ingesting it",
        email.message_id, verdict.score,
    )
    return Attachment(
        filename="email_body.txt",
        mime_type="text/plain",
        size=len(data),
        attachment_id="",
        data=data,
    )


def detect(email: EmailMessage) -> DetectionResult:
    resume_atts = _resume_type_attachments(email.attachments)

    if not resume_atts:
        # No file came with the mail — but plenty of candidates paste the CV
        # straight into the message, and the body is text we already hold. It
        # costs nothing to read, and the page classifier judges it on exactly
        # the same evidence it applies to a PDF page.
        if _sender_ignored(email.from_addr):
            return DetectionResult(
                False, 0.1, f"no attachment; sender ignored ({email.from_addr})", [],
            )

        body_att = _body_as_attachment(email)
        if body_att is not None:
            return DetectionResult(
                True, 0.7, "no attachment; resume content found in the email body",
                [body_att],
            )

        names = ", ".join(a.filename for a in email.attachments) or "none"
        detail = f"no resume-type attachment (saw: {names})"
        links = _CLOUD_LINK_RE.findall(email.body_text or "")
        if links:
            # Not fetched: pulling a stranger's cloud link is an outbound request
            # to an unknown host, and most are permission-walled anyway. Naming
            # it in the reason is what lets a recruiter act on it.
            detail += f"; body links to {links[0]} — open it manually if this is an application"
        return DetectionResult(False, 0.0, detail, [])

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
    # opened, yet it is exactly the file a candidate sends. So an attachment
    # earns a content inspection on its own: the page classifier reads it and
    # refuses a hall ticket in milliseconds, which is a far better judge than a
    # keyword search over a filename.
    #
    # Images count here too, and that is a deliberate trade. A photo of a CV
    # named `image.png`, sent under the subject "Fwd:", carries no signal a
    # keyword can find — it was the single largest class of false negative, and
    # no amount of filename policy recovers it. The cost is an OCR call on
    # images that turn out to be signature logos, bounded on the other side by
    # `min_image_attachment_bytes`. Those two settings are one dial: lowering
    # the floor to catch compressed scans is what makes this inspection matter,
    # and raising it is the lever if the OCR bill gets loud.
    #
    # A promotional subject still blocks it. That is a judgement about the
    # email, not about what its attachments are named.
    promotional = "promotional/transactional subject" in reasons
    if settings.inspect_all_documents and not promotional:
        is_candidate = True
        if score < settings.detector_min_score:
            kind = "document" if any(_is_document(a) for a in resume_atts) else "image"
            reasons.append(f"{kind} opened for content inspection regardless of filename")
    else:
        is_candidate = score >= settings.detector_min_score

    result = DetectionResult(is_candidate, round(score, 2), "; ".join(reasons), resume_atts)
    log.info(
        "Detector: msg=%s candidate=%s score=%.2f (%s)",
        email.message_id, is_candidate, result.score, result.reason,
    )
    return result
