"""Telling the OCR service's recovery log apart from its actual complaints.

Veris returns one ``warnings`` array and puts two unrelated kinds of message in
it. Most of what arrives is a provenance log — what the service had to *do* to
read the document, all of it successful:

    page 2 was rotated in the scan — auto-corrected 90° before extraction
    MRZ recovered from page 2
    back-page rescue: 2 LLM call(s), 4 field(s) rescued
    page 1: rescued 2 back-page field(s) via vision LLM
    structured complete resume — validated before persistence

None of those is a problem. Every one describes a document that was read, and a
record carrying them is *better* evidence than one carrying none, because the
service says how it got there. Filed under the word "warnings" they read as
five things going wrong on a passport that in fact parsed cleanly with its
check digits intact — so an operator either learns to ignore the list, which
defeats it, or chases scans that were never broken.

The split is deliberately lopsided. Only messages matching a known
recovery phrasing become notes; **anything unrecognised stays a warning**.
Veris owns this vocabulary and can extend it without telling us, and the two
mistakes are not equal: a recovery note shown as a warning is noise, while a
real complaint hidden in a notes list is a misread document nobody looks at.
When in doubt this errs toward the noise.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Sequence, Tuple

#: Phrasings that describe work the service did successfully. Matched case
#: insensitively anywhere in the message, because the service wraps them in
#: page numbers and counts that vary per document.
_RECOVERY = re.compile(
    "|".join(
        (
            r"auto-corrected",
            r"was rotated in the scan",
            r"\brecovered from\b",
            r"\brescued\b",
            r"\brescue:",
            r"re-read at",
            r"validated before persistence",
            r"already had",
        )
    ),
    re.IGNORECASE,
)


def is_recovery_note(message: str) -> bool:
    """Whether this message describes a recovery rather than a problem."""
    return bool(_RECOVERY.search(message or ""))


def split_service_messages(
    messages: Iterable[Any] | None,
) -> Tuple[List[str], List[str]]:
    """``(extraction_notes, warnings)`` from one service ``warnings`` array.

    Blank entries and non-strings are dropped — they carry nothing an operator
    can act on, and a `None` rendered as "None" in a warning list is its own
    small lie. Order is preserved within each list so the notes still read as
    the sequence of steps the service took.
    """
    notes: List[str] = []
    warnings: List[str] = []

    for raw in messages or ():
        if not isinstance(raw, str):
            continue
        message = raw.strip()
        if not message:
            continue
        (notes if is_recovery_note(message) else warnings).append(message)

    return notes, warnings


def merged(notes: Sequence[str], warnings: Sequence[str]) -> List[str]:
    """Everything the service said, warnings first.

    For callers that want the whole log back — the admin view of a record, a
    support question about what happened to one scan. Warnings lead because
    that is the half somebody reading the combined list is looking for.
    """
    return list(warnings) + list(notes)
