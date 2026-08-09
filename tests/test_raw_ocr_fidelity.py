"""`raw_ocr` must be the Veris response, byte for byte.

The Raw JSON tab in the UI is read as "what the OCR actually returned", so
anything this codebase adds to, removes from, or derives inside that payload is
a lie about the extraction. Two regressions lived here:

  * the mapper appended an `extracted_text` key holding *our* text extraction;
  * every profile save copied the edited profile into `raw_ocr["profile"]`,
    which the next save copied in again one level deeper.

These tests pin the payload to the response.
"""
from __future__ import annotations

import copy

from app.ai.resume_parser import map_veris_to_profile, veris_payload
from app.core.models import CandidateProfile
from app.db.repository import CandidateRepository
from recursai.veris_ocr.models import ResumeResult


# Shaped like a real /v1/resume/extract body, including the keys Veris emits
# even when empty — a null and a zero counter must survive untouched.
VERIS_RESPONSE = {
    "request_id": "req-123",
    "name": "Vignesh S",
    "designation": "QA Engineer",
    "industry": "IT",
    "highest_qualification": "B.E. Computer Science",
    "contact": {
        "emails": ["vignesh@example.com"],
        "phones": ["9876543210"],
        "linkedin": "https://linkedin.com/in/vignesh",
        "github": None,
        "address": "Chennai",
    },
    "skills": ["Selenium", "Python"],
    "experience": [
        {"company": "Acme", "designation": "QA Engineer",
         "start_date": "2021", "end_date": "2023", "description": None},
    ],
    "education": [{"institution": "Anna University", "degree": "B.E."}],
    "projects": [],
    "achievements": [],
    "certifications": [],
    "languages": ["English", "Tamil"],
    "personal_info": {"languages_known": ["English", "Tamil"]},
    "passport_details": None,
    "total_experience_human": "2 years",
    "total_experience_months": 24,
    "total_experience_years": 2.0,
    "indian_experience_months": 24,
    "overseas_experience_months": 0,
    "pages": [{"page_number": 1, "text": "VIGNESH S\nQA Engineer",
               "average_confidence": 0.97, "lines": [{"text": "VIGNESH S"}]}],
    "processing_time_ms": 4210,
    "warnings": [],
    "summary": None,
}


class FakeCollection:
    """Just enough of a PyMongo collection for the repository's write path."""

    def __init__(self, doc: dict):
        self.doc = copy.deepcopy(doc)

    def find_one(self, query):
        return copy.deepcopy(self.doc) if query.get("_id") == self.doc["_id"] else None

    def update_one(self, query, update):
        assert query["_id"] == self.doc["_id"]
        for key, value in update["$set"].items():
            node = self.doc
            *parents, leaf = key.split(".")
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = copy.deepcopy(value)


def _result() -> ResumeResult:
    return ResumeResult.from_dict(copy.deepcopy(VERIS_RESPONSE))


def test_payload_is_the_untouched_response():
    assert veris_payload(_result()) == VERIS_RESPONSE


def test_payload_is_a_copy_not_a_live_reference():
    """Mutating what we stored must not reach back into the client's dict."""
    res = _result()
    payload = veris_payload(res)
    payload["name"] = "tampered"
    payload["contact"]["emails"].append("tampered@example.com")
    assert veris_payload(res) == VERIS_RESPONSE


def test_mapper_stores_the_response_verbatim():
    profile = map_veris_to_profile(_result(), veris_text="VIGNESH S QA Engineer")

    # The mapper's own text extraction is *not* part of the OCR response.
    assert "extracted_text" not in profile.raw_ocr
    assert profile.raw_ocr == VERIS_RESPONSE
    # Nulls and zero counters are data too: the response said them.
    assert profile.raw_ocr["summary"] is None
    assert profile.raw_ocr["contact"]["github"] is None
    assert profile.raw_ocr["overseas_experience_months"] == 0
    # Sanity: the mapping itself still happened.
    assert profile.full_name == "Vignesh S"
    assert profile.email == "vignesh@example.com"


def test_client_bookkeeping_never_leaks_into_the_profile():
    """`_raw_response` is a client attribute, not a Veris field."""
    profile = map_veris_to_profile(_result())
    assert not any(k.startswith("_") for k in (profile.additional_info or {}))


def test_editing_a_profile_leaves_raw_ocr_untouched():
    coll = FakeCollection({
        "_id": "cand-1",
        "raw_ocr": copy.deepcopy(VERIS_RESPONSE),
        "profile": {"full_name": "Vignesh S", "raw_ocr": copy.deepcopy(VERIS_RESPONSE)},
    })
    repo = CandidateRepository(collection=coll)

    edited = CandidateProfile(
        full_name="Vignesh Sundaram",          # operator corrected the name
        email="vignesh@example.com",
        raw_ocr={"name": "whatever the browser sent back"},
    )
    repo.update_profile("cand-1", edited)

    assert coll.doc["profile"]["full_name"] == "Vignesh Sundaram"
    assert coll.doc["raw_ocr"] == VERIS_RESPONSE
    assert coll.doc["profile"]["raw_ocr"] == VERIS_RESPONSE


def test_repeated_saves_do_not_nest_the_profile_inside_raw_ocr():
    coll = FakeCollection({
        "_id": "cand-1",
        "raw_ocr": copy.deepcopy(VERIS_RESPONSE),
        "profile": {"full_name": "Vignesh S", "raw_ocr": copy.deepcopy(VERIS_RESPONSE)},
    })
    repo = CandidateRepository(collection=coll)

    for attempt in range(5):
        repo.update_profile("cand-1", CandidateProfile(full_name=f"Edit {attempt}"))

    assert "profile" not in coll.doc["raw_ocr"]
    assert coll.doc["raw_ocr"] == VERIS_RESPONSE
    assert coll.doc["profile"]["raw_ocr"] == VERIS_RESPONSE


def test_record_and_profile_copies_stay_identical_through_mongo():
    from app.core.models import CandidateRecord, SourceEmail, StoredResume

    profile = map_veris_to_profile(_result())
    record = CandidateRecord(
        id="cand-1",
        profile=profile,
        resume=StoredResume(
            original_filename="v.pdf", mime_type="application/pdf", size=1,
            sha256="h", storage_backend="local", storage_key="k",
        ),
        source_email=SourceEmail(message_id="m", thread_id="t", from_addr="a@b.c"),
        raw_ocr=profile.raw_ocr,
    )

    doc = record.to_mongo()
    # `exclude_none=True` on the record must not reach inside the payload and
    # prune the response's own null fields.
    assert doc["raw_ocr"] == VERIS_RESPONSE
    assert doc["profile"]["raw_ocr"] == VERIS_RESPONSE

    restored = CandidateRecord.from_mongo(doc)
    assert restored.raw_ocr == VERIS_RESPONSE
    assert restored.profile.raw_ocr == VERIS_RESPONSE
    # What the API hands the frontend — the Raw JSON tab renders this dict.
    assert restored.model_dump(mode="json")["raw_ocr"] == VERIS_RESPONSE
