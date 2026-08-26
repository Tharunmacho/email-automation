"""Reading candidates the bot wrote straight into the collection.

The sanctioned way for a WhatsApp candidate to reach this database is
``POST /candidates``: the bot maps its own record onto ``WhatsAppProfileIn``,
the intake service derives the CV requirement from the policy table, allocates
the profile through the balancer, and writes it in the CRM's own shape.

Not every record came that way. The bot also writes its *own* document into the
same collection — camelCase field names, ``_id`` an ObjectId, no ``profile``
sub-document, no ``created_at`` — and against one of those the CRM is blind in
two separate ways:

* the directory reads ``profile.full_name`` / ``profile.email`` / ``created_at``,
  finds none of them, and renders a real applicant as the placeholder row
  "Candidate Profile — No email on file";
* every by-id operation looks ``_id`` up as a string, misses an ObjectId key,
  and 404s — so the row cannot be opened, edited, allocated, or even deleted.

This module fixes the first of those by presenting such a document in the shape
the rest of the CRM already understands. It does not write anything back: the
stored record is left exactly as the bot wrote it, and what the CRM shows is a
view over it. (The id half of the problem is handled by ``_id_filter`` in
``app.db.repository``.)

**The mapping is an allow-list, not a passthrough**, and for the same reason
``WhatsAppProfileIn`` is one: the bot's record carries Aadhaar and PAN details
because a documentation officer needs them, this system has no screen that
shows them and no workflow that reads them, and a mapper that copied whatever
it found would put them in front of every recruiter the first time the bot
added a field. Anything ``_profile`` below does not name is dropped, so the
fields a bot record shows here are exactly the fields it would have shown had
it arrived through the front door.

None of this is the real fix. The real fix is upstream, in the bot: submitting
through the intake endpoint is also the only path that derives the CV
requirement, records which policy version decided it, deduplicates against the
idempotency key, and puts the candidate in somebody's queue. A record that
skipped it has none of those things, and no amount of read-side mapping can
invent them. What this buys is that the applicant is legible and reachable in
the meantime, instead of being an empty row an admin can only try to delete.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

# What a bot-written document is recognised by. Names from the bot's own
# vocabulary and no other writer's: the CRM's records are built from
# `CandidateRecord`, which has no field called any of these.
_MARKERS = ("waId", "fullName", "applicationId")

# The bot fields a *list row* is built from, as a Mongo projection. The list
# endpoint projects in the database — the OCR payload alone made a 200-row
# response several megabytes — so without these the fields would be gone before
# `normalize` ever saw the document.
LIST_PROJECTION: Dict[str, int] = {
    "waId": 1,
    "fullName": 1,
    "email": 1,
    "phone": 1,
    "mobileNumber": 1,
    "currentCity": 1,
    "currentState": 1,
    "currentCountry": 1,
    "currentOccupation": 1,
    "employers": 1,
    "skills": 1,
    "primaryTrade": 1,
    "jobCategory": 1,
    "educationCourse": 1,
    "totalExperienceYears": 1,
    "totalExperienceBand": 1,
    "registeredAt": 1,
    "firstContactAt": 1,
    "source": 1,
}

# The narrow one: what `_minimal_row` reads and nothing else.
MINIMAL_PROJECTION: Dict[str, int] = {
    "waId": 1,
    "fullName": 1,
    "email": 1,
    "phone": 1,
    "mobileNumber": 1,
    "registeredAt": 1,
    "firstContactAt": 1,
}


def is_bot_document(doc: Optional[Dict[str, Any]]) -> bool:
    """Whether this document carries the bot's own field names.

    Not "and has no profile". A record can be both: an admin edits one of these
    from the CRM, or a second registration refreshes it, and either writes a
    handful of `profile.*` keys onto the document the bot wrote. Treating that
    hybrid as a CRM record would drop the mapping for everything the write did
    not cover — including `created_at` and the string `_id` a read validates
    against — so a single edit would put the row back to the blank placeholder
    it started as, and the detail read back to raising.

    A candidate created through `POST /candidates` carries none of these names:
    the intake service builds a `CandidateRecord`, and nothing in that model is
    called `waId`. So the sanctioned path is untouched by this and stays so.
    """
    return bool(doc) and any(key in doc for key in _MARKERS)


def _first(*values: Any) -> Optional[Any]:
    """The first value that is actually present. `0` and `False` are values."""
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _clean_list(value: Any) -> list:
    """A list of non-empty strings, from a list, a scalar, or nothing."""
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [str(item).strip() for item in value if item not in (None, "")]


def _humanise(value: Any) -> Optional[str]:
    """`sales_retail` as `Sales Retail` — the bot's controlled values are
    snake_case ids, and a recruiter reads the row, not the enum."""
    if not value:
        return None
    return str(value).replace("_", " ").strip().title() or None


def _phones(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The candidate's numbers, kept apart rather than merged.

    `waId` is the WhatsApp id — the number the agency actually reaches them on,
    and always in international form. `mobileNumber` is what they typed when
    asked for a contact number, which is usually local and sometimes a second
    handset. The primary is the one they gave; the WhatsApp number is kept in
    E.164 beside it, which is the field a country-aware comparison reads.
    """
    wa = str(_first(doc.get("waId"), doc.get("phone")) or "").strip()
    mobile = str(_first(doc.get("mobileNumber"), doc.get("phone")) or "").strip()

    e164: Optional[str] = None
    if wa:
        digits = wa.lstrip("+")
        e164 = f"+{digits}" if digits.isdigit() else wa

    numbers = [n for n in dict.fromkeys([mobile, wa]) if n]
    return {
        "phone": mobile or wa or None,
        "phone_e164": e164,
        "phone_numbers": numbers,
    }


def _location(doc: Dict[str, Any]) -> Optional[str]:
    """"Chennai, Tamil Nadu, India" — the parts the bot asked for, in order."""
    parts = [
        doc.get("currentCity"),
        doc.get("currentState"),
        doc.get("currentCountry"),
    ]
    named = [str(p).strip() for p in parts if p]
    return ", ".join(named) or None


def _education(doc: Dict[str, Any]) -> list:
    """The one education entry the bot collects.

    It asks for a level ("graduate") and a course ("Welding"), never an
    institution — so the entry has a degree and a field of study and leaves the
    school blank, rather than inventing one to fill the shape.
    """
    degree = _humanise(doc.get("education"))
    course = doc.get("educationCourse")
    if not degree and not course:
        return []
    return [{"degree": degree, "field_of_study": course or None}]


def _job_answers(doc: Dict[str, Any]) -> list:
    """`tradeAnswers` as the screening answers they are.

    The bot stores them as ``{question_key: [answers]}`` with no question ids —
    they are the free-form questions it asked around the trade, which
    `JobAnswer` already allows for. The key is humanised into the question text
    because it is the only wording there is; joining the answers keeps what the
    candidate picked in the order they picked it.
    """
    answers = doc.get("tradeAnswers")
    if not isinstance(answers, dict):
        return []
    out = []
    for key, value in answers.items():
        picked = _clean_list(value)
        if not picked:
            continue
        out.append({"question": _humanise(key), "answer": ", ".join(picked)})
    return out


def _work_experience(doc: Dict[str, Any]) -> list:
    """The employers they named, as history with only the company known.

    The bot collects company names and nothing else about each — no dates, no
    designation per employer — so each entry carries the company alone. The
    current designation is a profile-level field and is not copied onto every
    past employer, which would claim they held today's job at all of them.
    """
    return [{"company": name} for name in _clean_list(doc.get("employers"))]


def _profile(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The bot's record as a `CandidateProfile`.

    Every key below is one `WhatsAppProfileIn` already accepts, so a record
    reads the same whichever door it came in by.

    Two fields are deliberately *not* filled:

    `destination_country` — the bot's `countryPreference` is a region
    ("europe"), and that field is documented as one actual country precisely
    because the CV policy keys on it and cannot tell what to do with a
    continent. So the region is carried in `additional_info` under the name the
    bot gave it, and the country field stays empty: unknown is a state the
    policy handles, and a continent in a country field is not.

    `total_experience_years` — copied only when the bot actually has a number.
    A band ("fresher", "3_5") stays a band, for the reason the model gives:
    turning it into a figure states something the candidate never said.
    """
    profile: Dict[str, Any] = {
        # Self-reported, structured answers to questions the bot asked — not a
        # model's reading of a scanned page. There is no extraction to be
        # unsure about, so this is 1.0 rather than the 0.0 an absent profile
        # defaults to, which the directory would otherwise flag as "needs
        # review" for a record that has nothing wrong with it.
        "confidence": 1.0,
        "full_name": doc.get("fullName"),
        "email": doc.get("email"),
        "location": _location(doc),
        "city": doc.get("currentCity"),
        "country": doc.get("currentCountry"),
        "job_category": doc.get("jobCategory"),
        "job_preference": _humanise(doc.get("primaryTrade")),
        "course_or_trade": doc.get("educationCourse"),
        "available_from": _humanise(doc.get("availability")),
        "current_designation": doc.get("currentOccupation"),
        "current_company": _first(*_clean_list(doc.get("employers"))),
        "total_experience_band": doc.get("totalExperienceBand"),
        "skills": _clean_list(doc.get("skills")),
        # Both, not the first of the two: `language` is the language the
        # conversation happened in and `languageOther` is one they volunteered,
        # so keeping only one drops a language the candidate actually speaks.
        "languages": _clean_list(doc.get("language")) + _clean_list(doc.get("languageOther")),
        "certifications": _clean_list(doc.get("certifications")),
        "education": _education(doc),
        "work_experience": _work_experience(doc),
        "job_answers": _job_answers(doc),
        # Passport only, exactly as the intake allow-list has it: overseas
        # placement turns on whether it is in date. Aadhaar and PAN are in the
        # document this was read from and are not copied.
        "passport_number": doc.get("passportNumber"),
        "passport_expiry": doc.get("passportExpiry"),
    }
    profile.update(_phones(doc))

    years = doc.get("totalExperienceYears")
    if isinstance(years, (int, float)):
        profile["total_experience_years"] = float(years)

    # What the bot knows and the CRM has no field for. Kept together and named,
    # so it is readable on the record without pretending to be something the
    # schema models — and still an allow-list, not the rest of the document.
    extra = {
        "application_id": doc.get("applicationId"),
        "country_preference": doc.get("countryPreference"),
        "country_strictness": doc.get("countryStrictness"),
        "work_type_preference": doc.get("workTypePreference"),
        "registration_stage": doc.get("stage"),
        "whatsapp_number_id": doc.get("whatsappNumberId"),
    }
    profile["additional_info"] = {k: v for k, v in extra.items() if v not in (None, "")}

    return {k: v for k, v in profile.items() if v not in (None, [], {})}


def _timestamp(*values: Any) -> Optional[datetime]:
    """The first value that is already a datetime. The bot writes real BSON
    dates, so nothing here parses strings — a string date would be a different
    bug and silently guessing at its format would hide it."""
    for value in values:
        if isinstance(value, datetime):
            return value
    return None


def normalize(doc: Dict[str, Any]) -> Dict[str, Any]:
    """One bot-written document, in the CRM's shape.

    Returns a new dict; the argument is not modified and neither is the stored
    record. Documents that are not the bot's own are returned untouched.
    """
    if not is_bot_document(doc):
        return doc

    out = dict(doc)

    # The bot's fields fill the gaps; anything the CRM already holds wins.
    #
    # The same rule `refresh_whatsapp_profile` applies when a second
    # registration arrives — a value that was captured is never replaced by an
    # absent one — and it is what makes editing one of these records stick: the
    # edited name is a real value in `profile`, so it is the one that shows,
    # while the fields the edit did not touch go on being read from the bot's.
    mapped = _profile(doc)
    held = {
        key: value
        for key, value in (doc.get("profile") or {}).items()
        if value not in (None, "", [], {})
    }
    out["profile"] = {**mapped, **held}
    out["source"] = doc.get("source") or "whatsapp"

    # The bot keys `_id` on an ObjectId; every id in the CRM is a string, and
    # `CandidateRecord.id` is typed as one. Stringifying here rather than at each
    # reader keeps "looks like a CRM record" the single job of this function —
    # and `_id_filter` in the repository accepts the hex back either way, so the
    # id a client is handed still resolves to this document.
    if "_id" in out:
        out["_id"] = str(out["_id"])

    # When the candidate registered — the date the directory sorts on and the
    # SLA clock falls back to. `registeredAt` is the moment they finished; the
    # first contact is the fallback for a record that stopped halfway.
    created = _timestamp(
        doc.get("created_at"),
        doc.get("registeredAt"),
        doc.get("firstContactAt"),
        doc.get("consentGivenAt"),
    )
    if created:
        out["created_at"] = created
        # Not "when the résumé was parsed" — nothing was parsed. It is when
        # this person became a record the CRM is answerable for, which is the
        # fact every SLA reader actually wants from this field.
        out.setdefault("ingested_at", created)

    updated = _timestamp(doc.get("updated_at"), doc.get("lastMessageAt"), created)
    if updated:
        out["updated_at"] = updated

    # No CV requirement was ever computed for this record, because computing it
    # is something the intake endpoint does and this record did not go through
    # it. False is not a claim that the policy exempted them — `cv_policy_version`
    # is left empty, and an empty version beside a False requirement is exactly
    # how a record that was never assessed reads. Asserting True would be worse
    # than wrong: the model requires a résumé to go with it, and there is no
    # résumé in the CRM's storage, so every read of this candidate would raise.
    out.setdefault("cv_required", False)

    return out


def normalize_all(docs) -> list:
    """`normalize` over a cursor or list, in one pass."""
    return [normalize(doc) for doc in docs]
