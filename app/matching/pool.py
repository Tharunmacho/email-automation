"""Who is matched: everybody not yet submitted, read in one projected query.

The pool is the candidate collection minus three groups, and nothing else:

    recruitment_status    anything but `available` means the candidate has
                          gone to a company or associate — on any order,
                          this one included — and is past the point a
                          shortlist is for
    placement_locked      placed; implied by the status above, kept as a
                          second guard because it is the field the CRM
                          itself refuses to move
    duplicate_of          a second copy of a person who is already in the
                          pool under their original record; ranking it
                          would list the same human twice

A candidate a recruiter typed in without a CV (`profile.is_resume == False`)
*is* in the pool: a thin record is for the matcher to rank — it lands in
manual review when its title fits — not for a filter to hide.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

#: What matching reads beyond the job-order matcher projection. Passed as
#: `extra_projection`, so the shared list row stays as narrow as it is for
#: every other caller.
EVIDENCE_PROJECTION = {
    "profile.trade_skills": 1,
    "profile.certifications": 1,
    "profile.licenses.name": 1,
    "profile.licenses.issuer": 1,
    "profile.licenses.expiry_date": 1,
    "profile.education": 1,
    "profile.course_or_trade": 1,
    "profile.country": 1,
    "profile.destination_country": 1,
    "profile.destination_countries": 1,
    "profile.available_from": 1,
    "profile.total_experience_band": 1,
    "profile.passport_expiry": 1,
    # Read only so readiness can say "passport on file"; the number itself is
    # never copied into a result or a response.
    "profile.passport_number": 1,
    "registration_state.outstanding_documents": 1,
}

#: Scanning is paged so a large collection is never one cursor held open.
PAGE_SIZE = 1000
#: A hard ceiling on one scan. Far above any pool this CRM has; here so a
#: runaway query has an end.
MAX_SCAN = 50_000

#: Recruitment states that mean "not yet submitted". `None` covers a record
#: written before the field existed, which `$in` also matches when missing.
NOT_SUBMITTED = (None, "available")


def retrieval_query() -> Dict[str, Any]:
    """The Mongo filter for the pool: everyone not already submitted.

    The same for every order. A candidate submitted to *this* order is
    excluded like any other — the shortlist is for finding people to submit,
    and they have been.
    """
    return {
        "recruitment_status": {"$in": list(NOT_SUBMITTED)},
        "placement_locked": {"$ne": True},
        "duplicate_of": {"$in": [None, ""]},
    }


def id_values(candidate_ids: Iterable[str]) -> List[Any]:
    """Ids in every form `_id` is stored in.

    The CRM keys candidates on hex-string ids and the WhatsApp bot on
    ObjectIds (see `repository._id_filter`); a row carries `str(_id)` for
    both, so an `$in` over strings alone would miss every bot-written record.
    """
    from bson import ObjectId

    values: List[Any] = []
    for candidate_id in candidate_ids:
        values.append(candidate_id)
        if len(candidate_id) == 24 and ObjectId.is_valid(candidate_id):
            values.append(ObjectId(candidate_id))
    return values


def retrieve(repository: Any, candidate_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Every candidate in the pool, as projected rows — or just the named ones."""
    from app.db.repository import MATCH_PROJECTION

    rows: List[Dict[str, Any]] = []
    skip = 0
    query = retrieval_query()
    if candidate_ids is not None:
        query["_id"] = {"$in": id_values(candidate_ids)}
    while skip < MAX_SCAN:
        page = repository.list_summaries(
            limit=PAGE_SIZE, skip=skip, staff_id=None, query=query,
            projection=MATCH_PROJECTION, extra_projection=EVIDENCE_PROJECTION,
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return rows
