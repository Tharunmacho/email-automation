"""Reading a candidate the bot wrote straight into the collection.

`POST /candidates` is the sanctioned door and `tests/test_whatsapp_intake.py`
covers it. These tests are about the records that did not come through it: the
bot's own document, camelCase and ObjectId-keyed, sitting in the candidates
collection with no `profile` and no `created_at`.

Two failures came of that, and both are pinned here. The directory rendered a
real applicant as the placeholder row "Candidate Profile — No email on file",
because every field it reads lives under `profile.`. And every by-id operation
404'd — open, edit, allocate, delete — because the lookup keys `_id` on a
string and the document's is an ObjectId. The second one is why such a row
could not even be removed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from app.db import whatsapp_compat
from app.db.repository import _id_filter


# A real record's shape, with the identity numbers a bot document carries and
# this system has no business storing.
BOT_DOC = {
    "_id": ObjectId("6a8c9d9cf1a8cb33943a1e57"),
    "source": "whatsapp",
    "waId": "919994690490",
    "mobileNumber": "9840949763",
    "applicationId": "ADR-00001",
    "fullName": "SATHISH SAKKARABANI",
    "email": "candidate@example.com",
    "currentCity": "Chennai",
    "currentState": "Tamil Nadu",
    "currentCountry": "India",
    "currentOccupation": "Digital marketing executive",
    "employers": ["Caratlane", "Reliance Digital"],
    "skills": ["Meta ads", "Canva"],
    "education": "graduate",
    "educationCourse": "Welding",
    "primaryTrade": "sales_retail",
    "jobCategory": "other",
    "countryPreference": "europe",
    "availability": "immediate",
    "language": "en",
    "totalExperienceYears": 0,
    "totalExperienceBand": "fresher",
    "passportNumber": "AP162295",
    "passportExpiry": "03/2036",
    "tradeAnswers": {"marketing_channels": ["email_marketing", "seo"]},
    "registeredAt": datetime(2026, 8, 24, 19, 38, tzinfo=timezone.utc),
    "lastMessageAt": datetime(2026, 8, 26, 17, 35, tzinfo=timezone.utc),
    "status": "profile_registered",
    "stage": "REGISTRATION_COMPLETED",
    # Never to be copied into the CRM. See `WhatsAppProfileIn`.
    "aadhaarNumber": "1234 5678 9012",
    "panNumber": "ABCDE1234F",
    "documents": {"aadhaar": {"status": "incomplete"}},
}


def _profile(doc: dict) -> dict:
    return whatsapp_compat.normalize(doc)["profile"]


# --------------------------------------------------------------------------- #
#  Recognising one
# --------------------------------------------------------------------------- #
def test_a_bot_document_is_recognised():
    assert whatsapp_compat.is_bot_document(BOT_DOC)


def test_a_crm_record_is_left_alone():
    """A candidate created through the intake endpoint is not touched.

    It is built from `CandidateRecord`, and no field in that model is called
    `waId` or `fullName` — so the sanctioned path carries none of the names
    this module keys on, and passes straight through.
    """
    crm = {"_id": "abc", "profile": {"full_name": "Alice"}, "source": "whatsapp"}
    assert not whatsapp_compat.is_bot_document(crm)
    assert whatsapp_compat.normalize(crm) is crm


def test_an_edited_bot_record_keeps_the_edit_and_the_rest_of_the_mapping():
    """The hybrid: a `profile.*` write landing on the bot's own document.

    An admin corrects the name, or a second registration refreshes it. What the
    CRM holds must win — otherwise the edit is invisible — and everything the
    write did not cover must go on being mapped, or one correction would blank
    every other column and take `created_at` and the string id with it.
    """
    edited = dict(BOT_DOC)
    edited["profile"] = {"full_name": "Sathish Sakkarabani", "email": ""}

    doc = whatsapp_compat.normalize(edited)
    assert doc["profile"]["full_name"] == "Sathish Sakkarabani"
    # Blanked by the edit, so the bot's value still stands — the same rule
    # `refresh_whatsapp_profile` applies to an answer a later run skipped.
    assert doc["profile"]["email"] == "candidate@example.com"
    assert doc["profile"]["location"] == "Chennai, Tamil Nadu, India"
    assert doc["created_at"] == BOT_DOC["registeredAt"]
    assert doc["_id"] == "6a8c9d9cf1a8cb33943a1e57"


def test_normalize_does_not_mutate_the_stored_document():
    before = dict(BOT_DOC)
    whatsapp_compat.normalize(BOT_DOC)
    assert BOT_DOC == before


# --------------------------------------------------------------------------- #
#  The row that used to be blank
# --------------------------------------------------------------------------- #
def test_the_directory_columns_are_filled():
    """Name, contact, role, experience and date — the six things a row shows."""
    doc = whatsapp_compat.normalize(BOT_DOC)
    profile = doc["profile"]

    assert profile["full_name"] == "SATHISH SAKKARABANI"
    assert profile["email"] == "candidate@example.com"
    assert profile["current_designation"] == "Digital marketing executive"
    assert profile["total_experience_band"] == "fresher"
    assert doc["created_at"] == BOT_DOC["registeredAt"]


def test_confidence_is_certain_because_nothing_was_extracted():
    """A profile the candidate typed is not a model's reading of a scan.

    Left at the 0.0 an absent profile defaults to, the directory would file
    every bot record under "Needs review" — a verdict about OCR quality, on a
    record where no OCR happened.
    """
    assert _profile(BOT_DOC)["confidence"] == 1.0


def test_created_at_is_when_they_registered_not_when_it_was_read():
    """The date the directory sorts on, and the one the SLA clock falls back
    to. A default of "now" would say every one of these arrived today."""
    doc = whatsapp_compat.normalize(BOT_DOC)
    assert doc["created_at"] == datetime(2026, 8, 24, 19, 38, tzinfo=timezone.utc)
    assert doc["ingested_at"] == doc["created_at"]
    assert doc["updated_at"] == datetime(2026, 8, 26, 17, 35, tzinfo=timezone.utc)


def test_the_id_is_a_string():
    """`CandidateRecord.id` is typed `str`; the bot writes an ObjectId."""
    assert whatsapp_compat.normalize(BOT_DOC)["_id"] == "6a8c9d9cf1a8cb33943a1e57"


# --------------------------------------------------------------------------- #
#  What must not be copied
# --------------------------------------------------------------------------- #
def test_aadhaar_and_pan_never_reach_the_crm():
    """The reason the mapping is an allow-list.

    The bot's record carries them because a documentation officer needs them.
    No screen here shows them and no workflow reads them, so a mapper that
    copied what it found would put them in front of every recruiter.
    """
    flat = repr(whatsapp_compat.normalize(BOT_DOC)["profile"])
    assert "1234 5678 9012" not in flat
    assert "ABCDE1234F" not in flat
    assert "aadhaar" not in flat.lower()
    assert "pan_number" not in flat.lower()


def test_a_region_never_becomes_the_destination_country():
    """`destination_country` is what the CV policy keys on, and it is one
    country. "europe" is not one, so it stays out of the field and is kept
    under the name the bot gave it."""
    profile = _profile(BOT_DOC)
    assert profile.get("destination_country") is None
    assert profile["additional_info"]["country_preference"] == "europe"


def test_a_band_is_never_turned_into_a_number():
    """"fresher" carries a real 0 here; a band on its own must not invent one."""
    banded = dict(BOT_DOC)
    banded.pop("totalExperienceYears")
    banded["totalExperienceBand"] = "3_5"

    profile = _profile(banded)
    assert profile["total_experience_band"] == "3_5"
    assert "total_experience_years" not in profile


# --------------------------------------------------------------------------- #
#  The mapping itself
# --------------------------------------------------------------------------- #
def test_the_whatsapp_number_and_the_contact_number_stay_apart():
    """`waId` is where the agency reaches them, in international form.
    `mobileNumber` is what they gave when asked. Merging the two loses one."""
    profile = _profile(BOT_DOC)
    assert profile["phone"] == "9840949763"
    assert profile["phone_e164"] == "+919994690490"
    assert set(profile["phone_numbers"]) == {"9840949763", "919994690490"}


def test_residence_is_assembled_in_order():
    assert _profile(BOT_DOC)["location"] == "Chennai, Tamil Nadu, India"


def test_employers_become_history_with_only_the_company_known():
    """The bot collects company names and nothing else about each. Stamping
    today's designation onto every one would claim they held it at all of
    them."""
    experience = _profile(BOT_DOC)["work_experience"]
    assert experience == [{"company": "Caratlane"}, {"company": "Reliance Digital"}]
    assert _profile(BOT_DOC)["current_company"] == "Caratlane"


def test_education_keeps_the_level_and_the_course_without_a_school():
    assert _profile(BOT_DOC)["education"] == [
        {"degree": "Graduate", "field_of_study": "Welding"}
    ]


def test_trade_answers_become_screening_answers():
    assert _profile(BOT_DOC)["job_answers"] == [
        {"question": "Marketing Channels", "answer": "email_marketing, seo"}
    ]


def test_both_languages_are_kept():
    """`language` is the one the conversation happened in; `languageOther` is
    one they volunteered. Keeping the first drops a language they speak."""
    multi = dict(BOT_DOC, languageOther="Tamil")
    assert _profile(multi)["languages"] == ["en", "Tamil"]


def test_passport_is_carried_because_placement_turns_on_it():
    profile = _profile(BOT_DOC)
    assert profile["passport_number"] == "AP162295"
    assert profile["passport_expiry"] == "03/2036"


def test_a_record_with_almost_nothing_in_it_still_maps():
    """A registration that stopped early is a record, not a crash."""
    doc = whatsapp_compat.normalize({"_id": ObjectId(), "waId": "9199", "fullName": "A"})
    assert doc["profile"]["full_name"] == "A"
    assert doc["source"] == "whatsapp"


# --------------------------------------------------------------------------- #
#  Validating into the record the rest of the CRM passes around
# --------------------------------------------------------------------------- #
def test_it_validates_as_a_candidate_record():
    from app.core.models import CandidateRecord

    record = CandidateRecord.from_mongo(whatsapp_compat.normalize(BOT_DOC))
    assert record.id == "6a8c9d9cf1a8cb33943a1e57"
    assert record.source == "whatsapp"
    assert record.profile.full_name == "SATHISH SAKKARABANI"


def test_no_cv_requirement_is_claimed_for_a_record_that_was_never_assessed():
    """Deriving the requirement is something the intake endpoint does, and this
    record did not go through it. An empty `cv_policy_version` beside a False
    requirement is how "never assessed" reads — and True would be worse than
    wrong, because the model demands a résumé to go with it and there is none,
    so every read of the candidate would raise.
    """
    doc = whatsapp_compat.normalize(BOT_DOC)
    assert doc["cv_required"] is False
    assert doc.get("cv_policy_version") is None


# --------------------------------------------------------------------------- #
#  The 404 — why one of these rows could not be opened, edited, or deleted
# --------------------------------------------------------------------------- #
def test_an_objectid_keyed_record_is_found_by_its_hex_id():
    matched = _id_filter("6a8c9d9cf1a8cb33943a1e57")["_id"]["$in"]
    assert ObjectId("6a8c9d9cf1a8cb33943a1e57") in matched
    assert "6a8c9d9cf1a8cb33943a1e57" in matched


def test_the_crms_own_string_ids_still_match():
    """A uuid hex is 32 characters and cannot be an ObjectId, so the common
    path stays the plain equality it always was."""
    assert _id_filter("a" * 32) == {"_id": "a" * 32}


def test_a_hex_uuid_that_is_24_characters_is_matched_both_ways():
    """The two id spaces overlap at 24 hex characters and nothing can tell them
    apart, so both are tried. Ids are unique either way."""
    ambiguous = "0" * 24
    assert ambiguous in _id_filter(ambiguous)["_id"]["$in"]


def test_an_unusable_id_does_not_raise():
    assert _id_filter("not-an-id") == {"_id": "not-an-id"}
