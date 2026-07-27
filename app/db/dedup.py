"""Duplicate-detection helpers.

Three independent signals, cheapest first:
  1. resume_hash  — exact same file bytes (SHA-256). Definitive.
  2. email_key    — same normalised email address.
  3. phone_key    — same normalised phone (last 10 digits).

The repository checks these in order. Hash is a hard duplicate; email/phone are
'same person' matches (their resume may have been updated) and are flagged so a
recruiter — or a future auto-merge step — can decide what to do.
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
