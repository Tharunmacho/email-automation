"""Duplicate-detection helpers.

Four independent signals. The first is the strongest and the rest are ordered
cheapest first:

  1. passport_key — same passport number. One person, one passport; a number
                    that matches is the same human being, whichever handset
                    they wrote from and whichever of the agency's lines they
                    wrote to.
  2. resume_hash  — exact same file bytes (SHA-256). Definitive about the
                    *file*, which is not quite the same claim.
  3. email_key    — same normalised email address.
  4. phone_key    — same normalised phone (last 10 digits).

A phone is a *contact detail*, not an identity. One person carries two numbers
— the handset they message from and the one printed on their CV — and two
people share one number often enough to matter in a labour-supply database:
a shared handset, an agent submitting on somebody's behalf, a recycled SIM. So
a phone match routes a submission to a record; it is not evidence strong enough
to fuse two records that already exist. Passport is.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    email = email.strip().lower()
    # Basic sanity: must look like an address.
    if "@" not in email or " " in email:
        return None
    return email


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return None
    # Compare on the last 10 digits to ignore country-code / formatting variance.
    return digits[-10:]


#: Below this, a "passport number" is not one. Real passport numbers run to
#: six to nine characters; anything shorter is a stray token off a scan and
#: matching on it would fuse unrelated people.
MIN_PASSPORT_CHARS = 6


def normalize_passport(number: Optional[str]) -> Optional[str]:
    """The comparable form of a passport number, or None if it is not one.

    Upper-cased with every separator removed, because the same passport is
    written "Z1234567", "z 1234567" and "Z-1234567" by three different sources
    — the MRZ, the candidate typing it into WhatsApp, and a recruiter copying
    it off the page — and all three have to collide.

    Returns None rather than a short string for anything implausible. This
    value is an identity: a `None` costs a match that has to be made another
    way, while a bad value silently welds two candidates together, and only one
    of those is recoverable.
    """
    if not number:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", number).upper()
    if len(cleaned) < MIN_PASSPORT_CHARS:
        return None
    # A run of digits with no letter at all, or the reverse, is what a failed
    # OCR produces when it reads a date or a line of the address as the number.
    # Both shapes exist as real passport numbers, so neither is refused here —
    # this only refuses the placeholder every extractor emits.
    if set(cleaned) <= {"X"} or set(cleaned) <= {"0"}:
        return None
    return cleaned
