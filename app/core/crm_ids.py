"""Stable, human-facing identifiers for CRM records.

Mongo ``_id`` values remain the database keys.  These codes are deliberately
separate: they are short enough to quote over the phone, safe to expose in the
UI, and deterministic so legacy records can be backfilled without a sequence
service or a renumbering migration.
"""
from __future__ import annotations

import hashlib


def _crm_code(prefix: str, internal_id: object) -> str:
    value = str(internal_id).strip()
    if not value:
        raise ValueError("A CRM code requires an internal record id.")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"


def candidate_code(internal_id: object) -> str:
    """Return the public candidate identifier for one database key."""
    return _crm_code("CAN", internal_id)


def staff_code(internal_id: object) -> str:
    """Return the public staff identifier for one database key."""
    return _crm_code("STF", internal_id)
