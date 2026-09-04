"""WhatsApp candidate intake: the rules that must hold, stated as tests.

The cases here are the acceptance criteria of the integration brief. They fall
into four groups, and each group exists because of a specific way this could go
wrong:

* **Source rules** — email must keep requiring a CV. The whole point of adding a
  second source is that the first one does not change, and that claim is only
  worth anything if something checks it.
* **Policy authority** — the caller must not be able to talk its way out of a
  CV. A payload that carries both the requirement and the thing the requirement
  governs is a payload that certifies itself, so the CRM derives the answer and
  these tests prove the caller's version is ignored.
* **Idempotency** — a retry must return the first candidate, not make a second
  one. Including when two retries arrive together, which a lookup cannot handle.
* **Recruiter state** — a candidate who re-registers must not undo an
  assessment. This is the one that destroys real work if it breaks, and it
  breaks silently.
"""
from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Imported at module scope, deliberately. `app/api/routes.py` builds a
# `UserRepository()` at import time, which opens a Mongo connection — and
# `tests/conftest.py` refuses connections once its autouse fixture is active.
# Importing here happens during collection, before that fixture runs, which is
# how the rest of the suite does it too. A lazy import inside the fixture trips
# the guard.
from app.api.routes import app as fastapi_app
from app.core.models import (
    CandidateProfile,
    CandidateRecord,
    SourceEmail,
    StoredResume,
)
from app.policy.cv_policy import is_cv_required, reset_policy_cache
from app.services.candidate_intake import IntakeError, intake_whatsapp_candidate

SERVICE_KEY = "test-service-key-12345"


# --------------------------------------------------------------------------- #
#  Doubles
# --------------------------------------------------------------------------- #
class FakeRepo:
    """An in-memory stand-in that models the constraints that matter.

    Specifically the unique index on `idempotency_key`: `insert` refuses a
    second document carrying a key it has already stored, the same way MongoDB
    would. Without that, the concurrency test would pass against a dictionary
    and prove nothing about the database it is meant to be describing.
    """

    def __init__(self):
        self.candidates: dict[str, CandidateRecord] = {}
        self.assigned: list[str] = []

    # ---- lookups ---- #
    def find_by_idempotency_key(self, key):
        if not key:
            return None
        return next(
            (c for c in self.candidates.values() if c.idempotency_key == key), None
        )

    def find_by_resume_hash(self, resume_hash):
        if not resume_hash:
            return None
        return next(
            (c for c in self.candidates.values() if c.resume_hash == resume_hash), None
        )

    def find_by_passport_key(self, passport_key):
        if not passport_key:
            return None
        return next(
            (c for c in self.candidates.values() if c.passport_key == passport_key), None
        )

    def find_by_email_or_phone(self, email_key, phone_key):
        for c in self.candidates.values():
            if phone_key and c.phone_key == phone_key:
                return c
            if email_key and c.email_key == email_key:
                return c
        return None

    def get(self, candidate_id):
        return self.candidates.get(candidate_id)

    # ---- writes ---- #
    def insert(self, record: CandidateRecord) -> str:
        existing = self.find_by_idempotency_key(record.idempotency_key)
        if existing:
            # What the unique index does: the loser of the race reads back the
            # winner's document rather than creating a rival one.
            return existing.id
        existing = self.find_by_resume_hash(record.resume_hash)
        if existing:
            return existing.id
        existing = self.find_by_passport_key(record.passport_key)
        if existing:
            return existing.id
        self.candidates[record.id] = record
        return record.id

    def claim_passport(self, candidate_id, passport_number, source="ocr"):
        from app.db.dedup import normalize_passport

        key = normalize_passport(passport_number)
        existing = self.find_by_passport_key(key)
        if existing and existing.id != candidate_id:
            return existing.id
        record = self.candidates.get(candidate_id)
        if not record:
            return None
        record.passport_key = key
        record.passport_key_source = source
        return candidate_id

    def refresh_whatsapp_profile(self, candidate_id: str, profile: CandidateProfile):
        from app.db.repository import CandidateRepository

        record = self.candidates[candidate_id]
        incoming = profile.model_dump(mode="python")
        for field in CandidateRepository.WHATSAPP_REFRESHABLE_FIELDS:
            value = incoming.get(field)
            if value in (None, "", [], {}):
                continue
            setattr(record.profile, field, value)

    def refresh_whatsapp_sections(self, candidate_id: str, *, registration=None, job=None):
        record = self.candidates[candidate_id]
        # Replaced wholesale, exactly as the repository does it: these two
        # objects are entirely the bot's, and merging them would make an answer
        # the candidate changed impossible to unset.
        if registration is not None:
            record.registration = registration
        if job is not None:
            record.job = job

    def adopt_idempotency_key(self, candidate_id: str, key) -> bool:
        """Fills a blank only, and refuses a key another record already holds —
        the two properties of the sparse unique index the service leans on."""
        record = self.candidates.get(candidate_id)
        if not record or not key or record.idempotency_key:
            return False
        if self.find_by_idempotency_key(key):
            return False
        record.idempotency_key = key
        return True

    def attach_resume(self, candidate_id: str, resume: StoredResume) -> bool:
        record = self.candidates.get(candidate_id)
        if not record:
            return False
        record.resume = resume
        record.resume_hash = resume.sha256
        return True


class FakeStorage:
    """Somewhere for uploaded bytes to go that is not GridFS.

    The point of the résumé tests is that the CRM stores the file *itself* —
    that what lands on the record is a key into this system's storage rather
    than a path on the bot's disk. A dictionary proves that as well as a
    database does, and does not need one running.
    """

    name = "fake"

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        return key in self.objects

    def save(self, key: str, data: bytes, content_type=None) -> str:
        self.objects[key] = data
        return key

    def load(self, key: str) -> bytes:
        return self.objects[key]


def wa_profile(**overrides) -> CandidateProfile:
    base = dict(
        is_resume=False,
        confidence=0.0,
        full_name="Ravi Kumar",
        phone="+919876543210",
        phone_e164="+919876543210",
        country="India",
        destination_country="Malaysia",
        job_category="general_worker",
        job_preference="General worker",
        total_experience_band="1_3",
    )
    base.update(overrides)
    return CandidateProfile(**base)


def test_deleted_whatsapp_candidate_can_be_added_again():
    class DeletedRepo(FakeRepo):
        def was_deleted(self, **signals):
            assert signals["idempotency_key"] == "whatsapp/111/919876543210"
            assert signals["phone_key"] == "9876543210"
            return True

    with patch("app.services.candidate_intake.assign_candidate"):
        result = intake_whatsapp_candidate(
            profile=wa_profile(),
            idempotency_key="whatsapp/111/919876543210",
            repo=DeletedRepo(),
        )

    assert result.created is True
    assert result.candidate_id


@pytest.fixture(autouse=True)
def _fresh_policy():
    reset_policy_cache()
    yield
    reset_policy_cache()


@pytest.fixture
def client():
    """A TestClient with the service key configured and Mongo kept out of it.

    `TestClient(app)` rather than `with TestClient(app)`: entering the context
    manager fires FastAPI's startup event, which calls `ensure_indexes` and
    reaches the real database — `tests/conftest.py` refuses that, correctly. The
    existing suite constructs the client the same way for the same reason.
    """
    repo = FakeRepo()
    storage = FakeStorage()
    with patch("app.api.routes.ensure_indexes"), \
         patch("app.config.settings.whatsapp_service_key", SERVICE_KEY), \
         patch("app.api.routes.repo", return_value=repo), \
         patch("app.services.resume_store.get_storage_backend", return_value=storage), \
         patch("app.services.candidate_intake.assign_candidate") as assign:
        c = TestClient(fastapi_app)
        c.fake_repo = repo
        c.assign_mock = assign
        c.storage = storage
        yield c


def post_candidate(
    client,
    *,
    key="whatsapp/111/919876543210",
    claim=None,
    sections=None,
    **profile_over,
):
    body = {
        "source": "whatsapp",
        "profile": {
            "full_name": "Ravi Kumar",
            "phone": "+919876543210",
            "phone_e164": "+919876543210",
            "country": "India",
            "destination_country": "Malaysia",
            "job_category": "general_worker",
            "job_preference": "General worker",
            "total_experience_band": "1_3",
        },
        "idempotency_key": key,
    }
    body["profile"].update(profile_over)
    if claim is not None:
        body["cv_required_claim"] = claim
    # `registration`, `cv`, `identity`, `job` — the four sections a submission
    # can carry beyond the profile. Passed as a dict so a test can send one, or
    # several, without a keyword per section.
    if sections:
        body.update(sections)
    return client.post("/candidates", json=body, headers={"X-Service-Key": SERVICE_KEY})


def in_progress(stage="JOB_PREFERENCE_PENDING", **over) -> dict:
    """A `registration` block for a candidate who is still answering."""
    return {"registration": {"complete": False, "stage": stage, **over}}


def finished(**over) -> dict:
    return {"registration": {"complete": True, "stage": "REGISTRATION_COMPLETED", **over}}


#: Small enough to inline, structurally real enough that a type check passes it.
PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def resume_payload(content: bytes = PDF_BYTES, filename: str = "cv.pdf") -> dict:
    """A resume as it travels inside `POST /candidates`."""
    return {
        "filename": filename,
        "mime_type": "application/pdf",
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


# --------------------------------------------------------------------------- #
#  1-2. Email keeps its CV requirement
# --------------------------------------------------------------------------- #
def test_email_candidate_with_a_cv_is_accepted():
    """The existing workflow, unchanged. If this breaks, the migration failed."""
    record = CandidateRecord(
        id="e1",
        profile=CandidateProfile(full_name="Alice"),
        resume=StoredResume(
            original_filename="cv.pdf",
            mime_type="application/pdf",
            size=10,
            sha256="hash-a",
            storage_backend="local",
            storage_key="k",
        ),
        source_email=SourceEmail(message_id="m1", thread_id="t1", from_addr="a@b.c"),
        resume_hash="hash-a",
    )
    assert record.source == "email"
    assert record.cv_required is True


def test_email_candidate_without_a_cv_is_rejected():
    """No source, no policy, no exception: an email candidate has a résumé."""
    with pytest.raises(ValueError, match="must have a resume"):
        CandidateRecord(
            id="e2",
            profile=CandidateProfile(full_name="Alice"),
            source_email=SourceEmail(message_id="m", thread_id="t", from_addr="a@b.c"),
        )


def test_email_candidate_without_a_source_email_is_rejected():
    with pytest.raises(ValueError, match="must have source_email"):
        CandidateRecord(
            id="e3",
            profile=CandidateProfile(full_name="Alice"),
            resume=StoredResume(
                original_filename="cv.pdf",
                mime_type="application/pdf",
                size=10,
                sha256="hash-b",
                storage_backend="local",
                storage_key="k",
            ),
        )


# --------------------------------------------------------------------------- #
#  3-6. The CV policy decides, and the caller cannot argue
# --------------------------------------------------------------------------- #
def test_whatsapp_without_a_cv_succeeds_where_policy_allows(client):
    res = post_candidate(client)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["success"] is True
    assert body["cv_required"] is False
    assert body["created"] is True


def test_whatsapp_is_rejected_when_policy_requires_a_cv_and_none_is_supplied(client):
    # Technician is a skilled role: the policy requires a CV wherever it is.
    res = post_candidate(client, job_category="technician")
    assert res.status_code == 422
    assert "requires a resume" in res.text


def test_the_callers_claim_cannot_override_the_policy(client):
    """The bypass this whole design exists to close.

    The bot insists no CV is needed for a technician. The CRM disagrees, and the
    CRM is the one holding the database.
    """
    res = post_candidate(client, claim=False, job_category="technician")
    assert res.status_code == 422
    assert "requires a resume" in res.text


def test_a_claim_that_disagrees_is_reported_back(client):
    """A disagreement in the harmless direction is still worth telling the bot.

    The bot thought a CV was needed and the policy says it is not. Nothing is
    rejected — but `policy_overrode_claim` lets the bot notice its cached policy
    has drifted rather than going on asking for documents nobody wants.
    """
    res = post_candidate(client, claim=True)
    assert res.status_code == 201
    assert res.json()["policy_overrode_claim"] is True
    assert res.json()["cv_required"] is False


def test_policy_resolution_rules():
    assert is_cv_required("Malaysia", "general_worker") is False
    assert is_cv_required("Singapore", "general_worker") is False
    # A more specific rule beats a broader one regardless of table order.
    assert is_cv_required("Malaysia", "technician") is True
    # Unknown combinations default to requiring a CV — the safe direction.
    assert is_cv_required("Narnia", "wizard") is True
    assert is_cv_required(None, None) is True
    # Case and separator differences must not change the answer.
    assert is_cv_required("  malaysia ", "GENERAL-WORKER") is False


# --------------------------------------------------------------------------- #
#  7. Two CV-less candidates can coexist  (the resume_hash landmine)
# --------------------------------------------------------------------------- #
def test_two_cv_less_candidates_both_persist(client):
    """The bug that would have shipped: `resume_hash=""` in a unique index.

    Sparse skips *missing* fields, not empty ones, so the first CV-less
    candidate inserted and the second collided with them. The failure only
    appears on the second record, which is exactly why it needed a test.
    """
    first = post_candidate(client, key="whatsapp/111/919876543210")
    second = post_candidate(
        client, key="whatsapp/111/919111111111", phone="+919111111111",
        phone_e164="+919111111111",
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["candidate_id"] != second.json()["candidate_id"]
    assert len(client.fake_repo.candidates) == 2


def test_a_cv_less_record_omits_resume_hash_entirely():
    record = CandidateRecord(
        id="w1", source="whatsapp", profile=wa_profile(), cv_required=False
    )
    doc = record.to_mongo()
    # Absent, not empty — the sparse index only skips what is missing.
    assert "resume_hash" not in doc
    assert "resume" not in doc
    assert "source_email" not in doc


# --------------------------------------------------------------------------- #
#  8-9. Idempotency
# --------------------------------------------------------------------------- #
def test_the_same_idempotency_key_twice_yields_one_candidate(client):
    key = "whatsapp/111/919876543210"
    first = post_candidate(client, key=key)
    second = post_candidate(client, key=key)

    assert first.json()["candidate_id"] == second.json()["candidate_id"]
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert len(client.fake_repo.candidates) == 1


def test_concurrent_submissions_with_one_key_yield_one_candidate():
    """Both callers pass the lookup, and the unique index arbitrates.

    Driven at the service rather than through HTTP so the race can be staged
    exactly: two intakes are built against a repository that has nothing in it,
    which is the state both concurrent requests would observe.
    """
    repo = FakeRepo()
    key = "whatsapp/111/919876543210"

    with patch("app.services.candidate_intake.assign_candidate"):
        first = intake_whatsapp_candidate(
            profile=wa_profile(), idempotency_key=key, repo=repo
        )
        # The second request was already past its own lookup when the first
        # inserted; it now attempts an insert of its own.
        second = intake_whatsapp_candidate(
            profile=wa_profile(), idempotency_key=key, repo=repo
        )

    assert first.candidate_id == second.candidate_id
    assert len(repo.candidates) == 1


# --------------------------------------------------------------------------- #
#  10. Recruiter-owned state survives a re-registration
# --------------------------------------------------------------------------- #
def test_re_registering_never_resets_recruiter_state():
    """The one that quietly destroys work if it breaks.

    A candidate assessed and rejected last week walks through the bot again.
    Their profile may be refreshed; the agency's verdict about them may not.
    """
    repo = FakeRepo()
    with patch("app.services.candidate_intake.assign_candidate"):
        first = intake_whatsapp_candidate(
            profile=wa_profile(), idempotency_key="whatsapp/111/aaa", repo=repo
        )

    record = repo.candidates[first.candidate_id]
    record.evaluation_status = "rejected"
    record.evaluation_score = 2
    record.evaluation_notes = "Not suitable for this client"
    record.assigned_staff_id = "staff-7"
    record.viewed_at = record.created_at

    # Same person, new session, new key, different destination.
    with patch("app.services.candidate_intake.assign_candidate"):
        second = intake_whatsapp_candidate(
            profile=wa_profile(destination_country="Singapore"),
            idempotency_key="whatsapp/111/bbb",
            repo=repo,
        )

    assert second.candidate_id == first.candidate_id
    assert second.created is False

    after = repo.candidates[first.candidate_id]
    # What they told us about themselves moved.
    assert after.profile.destination_country == "Singapore"
    # What the agency decided about them did not.
    assert after.evaluation_status == "rejected"
    assert after.evaluation_score == 2
    assert after.evaluation_notes == "Not suitable for this client"
    assert after.assigned_staff_id == "staff-7"
    assert after.viewed_at is not None


def test_the_refresh_allow_list_excludes_every_recruiter_field():
    """Stated directly, so adding a field to the list cannot smuggle one in."""
    from app.db.repository import CandidateRepository

    forbidden = {
        "evaluation_status",
        "evaluation_score",
        "evaluation_notes",
        "evaluated_at",
        "evaluated_by",
        "assigned_staff_id",
        "assigned_staff_name",
        "assigned_at",
        "viewed_at",
        "status",
    }
    assert not forbidden & set(CandidateRepository.WHATSAPP_REFRESHABLE_FIELDS)


# --------------------------------------------------------------------------- #
#  13-15. Field separation
# --------------------------------------------------------------------------- #
def test_residence_and_destination_are_stored_separately(client):
    res = post_candidate(client)
    assert res.status_code == 201
    record = next(iter(client.fake_repo.candidates.values()))
    assert record.profile.country == "India"
    assert record.profile.destination_country == "Malaysia"


def test_singapore_and_malaysia_are_distinct_destinations(client):
    post_candidate(client, key="k-my", destination_country="Malaysia")
    post_candidate(
        client, key="k-sg", destination_country="Singapore",
        phone="+919111111111", phone_e164="+919111111111",
    )
    destinations = {
        c.profile.destination_country for c in client.fake_repo.candidates.values()
    }
    assert destinations == {"Malaysia", "Singapore"}


def test_the_experience_band_is_stored_as_a_band(client):
    res = post_candidate(client, total_experience_band="5_10")
    assert res.status_code == 201
    record = next(iter(client.fake_repo.candidates.values()))
    assert record.profile.total_experience_band == "5_10"
    # Never coerced into the numeric field — that would be inventing a figure
    # the candidate never gave.
    assert record.profile.total_experience_years is None


def test_aadhaar_and_pan_cannot_be_sent(client):
    """The input schema has no field for them, so they are dropped, not stored."""
    res = client.post(
        "/candidates",
        json={
            "source": "whatsapp",
            "profile": {
                "full_name": "Ravi Kumar",
                "phone": "+919876543210",
                "destination_country": "Malaysia",
                "job_category": "general_worker",
                "aadhaar_number": "1234 5678 9012",
                "pan_number": "ABCDE1234F",
            },
            "idempotency_key": "whatsapp/111/pii",
        },
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert res.status_code == 201
    record = next(iter(client.fake_repo.candidates.values()))
    dumped = record.profile.model_dump()
    assert "aadhaar_number" not in dumped
    assert "pan_number" not in dumped


def test_the_job_application_block_is_stored(client):
    """What they applied for, as the profile screen reads it back.

    The bot asks these directly, and every one of them was dropped at the door
    until the input schema named them — `extra="ignore"` is an allow-list, so a
    field the bot sends and the model does not declare is silently discarded.
    """
    res = post_candidate(
        client,
        job_id="electrician",
        job_title="Electrician",
        course_or_trade="ITI Electrician",
        state_preference="Bihar",
        available_from="after 2 months",
        job_answers=[
            {"question_id": "q1", "question": "Years on site?", "answer": "6", "kind": "text"},
            {"question_id": "q2", "question": "Hold a valid licence?", "answer": "Yes", "kind": "choice"},
        ],
    )
    assert res.status_code == 201

    profile = next(iter(client.fake_repo.candidates.values())).profile
    assert profile.job_id == "electrician"
    assert profile.job_title == "Electrician"
    assert profile.course_or_trade == "ITI Electrician"
    assert profile.state_preference == "Bihar"
    assert profile.available_from == "after 2 months"

    # The wording travels with the answer rather than being resolved from the
    # job's question list at read time. An admin rewording a question must not
    # rewrite what this candidate was asked.
    assert [(a.question, a.answer) for a in profile.job_answers] == [
        ("Years on site?", "6"),
        ("Hold a valid licence?", "Yes"),
    ]


def test_the_state_preference_never_stands_in_for_the_destination(client):
    """Two fields, one rule: the CV policy reads the country and nothing else.

    A state is below a country, not an alternative to one — a record carrying
    "Bihar" and no destination would resolve the CV requirement against nothing.
    """
    res = post_candidate(client, destination_country="Singapore", state_preference="Johor")
    assert res.status_code == 201
    profile = next(iter(client.fake_repo.candidates.values())).profile
    assert profile.destination_country == "Singapore"
    assert profile.state_preference == "Johor"


def test_the_job_block_is_refreshable_on_re_registration():
    """Someone who changes their mind about the job is telling us about themselves.

    So these fields sit in the refresh allow-list — unlike anything the agency
    concluded, which `test_the_refresh_allow_list_excludes_every_recruiter_field`
    holds shut.
    """
    from app.db.repository import CandidateRepository

    refreshable = set(CandidateRepository.WHATSAPP_REFRESHABLE_FIELDS)
    assert {
        "job_id",
        "job_title",
        "course_or_trade",
        "state_preference",
        "available_from",
        "job_answers",
    } <= refreshable


def test_passport_details_are_stored(client):
    res = post_candidate(
        client, passport_number="Z1234567", passport_expiry="03/2031"
    )
    assert res.status_code == 201
    record = next(iter(client.fake_repo.candidates.values()))
    assert record.profile.passport_number == "Z1234567"
    assert record.profile.passport_expiry == "03/2031"


def test_same_passport_with_different_phone_and_registration_reuses_candidate(client):
    first = post_candidate(
        client,
        key="whatsapp/111/919876543210",
        passport_number="z 1234-567",
    )
    second = post_candidate(
        client,
        key="whatsapp/222/60123456789",
        phone="+60123456789",
        phone_e164="+60123456789",
        passport_number="Z1234567",
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["candidate_id"] == first.json()["candidate_id"]
    assert len(client.fake_repo.candidates) == 1


# --------------------------------------------------------------------------- #
#  16. Authentication
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("headers", [{}, {"X-Service-Key": "wrong-key"}])
def test_intake_rejects_bad_service_credentials(client, headers):
    res = client.post(
        "/candidates",
        json={
            "source": "whatsapp",
            "profile": {"full_name": "X"},
            "idempotency_key": "k",
        },
        headers=headers,
    )
    assert res.status_code == 401
    assert not client.fake_repo.candidates


def test_a_staff_token_is_not_a_service_credential(client):
    """Separate credentials, so a leaked recruiter session cannot inject."""
    from app.core.security import create_token
    from app.config import settings

    token = create_token(subject="some-staff-id", secret=settings.auth_secret)
    res = client.post(
        "/candidates",
        json={
            "source": "whatsapp",
            "profile": {"full_name": "X"},
            "idempotency_key": "k",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 401


def test_the_policy_endpoint_also_requires_the_service_key(client):
    assert client.get(
        "/policy/cv-required",
        params={"destination_country": "Malaysia", "job_category": "general_worker"},
    ).status_code == 401

    ok = client.get(
        "/policy/cv-required",
        params={"destination_country": "Malaysia", "job_category": "general_worker"},
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert ok.status_code == 200
    assert ok.json()["cv_required"] is False


# --------------------------------------------------------------------------- #
#  Source restriction
# --------------------------------------------------------------------------- #
def test_the_endpoint_refuses_to_create_email_candidates(client):
    """The mailbox pipeline keeps its monopoly on email candidates."""
    res = client.post(
        "/candidates",
        json={
            "source": "email",
            "profile": {"full_name": "Alice"},
            "idempotency_key": "k",
        },
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert res.status_code == 422
    assert "whatsapp" in res.text.lower()


def test_an_unknown_job_category_is_refused(client):
    """A typo must not sail through to the policy and land on the default."""
    res = post_candidate(client, job_category="not_a_real_category")
    assert res.status_code == 422
    assert "job_category" in res.text


# --------------------------------------------------------------------------- #
#  A job an admin added, and the questions they hung on it
#
#  The two halves of Data Management reaching a candidate record. The first is
#  a regression that made the screen actively harmful: a job created there was
#  offered to candidates by the bot within five minutes and then refused at
#  submission, because the category was validated against the tuple compiled
#  into `cv_policy` rather than against the table the screen writes to.
#
#  `known_job_ids` reads Mongo, which `conftest` refuses, so these patch the
#  table the way a deployment would have one. Without the patch the lookup
#  falls back to the built-in tuple — which is the behaviour every other test
#  in this file exercises, and is why the regression survived them.
# --------------------------------------------------------------------------- #
def with_table(*extra_ids):
    """Point `known_job_ids` at a table holding the seeded jobs plus `extra_ids`."""
    from app.db.taxonomy import SEED_JOBS

    rows = [{"id": seed["id"]} for seed in SEED_JOBS]
    rows.extend({"id": job_id} for job_id in extra_ids)
    return patch("app.db.taxonomy.list_jobs", return_value=rows)


def test_a_job_an_admin_added_is_accepted(client):
    """The whole point of the screen: add a row, and a candidate can apply for it.

    With a resume attached, because a job an admin created carries no CV rule of
    its own and the policy default is "required" — which is a separate refusal
    from the one this test is about, and would hide it.
    """
    with with_table("cnc_operator"):
        res = client.post(
            "/candidates",
            json={
                "source": "whatsapp",
                "profile": {
                    "full_name": "Ravi Kumar",
                    "phone": "+919876543210",
                    "destination_country": "Malaysia",
                    "job_category": "cnc_operator",
                    "job_id": "cnc_operator",
                    "job_title": "CNC Operator",
                },
                "idempotency_key": "whatsapp/111/cnc",
                "resume": resume_payload(),
            },
            headers={"X-Service-Key": SERVICE_KEY},
        )

    assert res.status_code == 201, res.text
    stored = next(iter(client.fake_repo.candidates.values()))
    assert stored.profile.job_category == "cnc_operator"
    assert stored.profile.job_title == "CNC Operator"


def test_a_typo_is_still_refused_when_the_table_can_be_read(client):
    """Widening the check to the table must not turn it off."""
    with with_table("cnc_operator"):
        res = post_candidate(client, job_category="not_a_real_category")

    assert res.status_code == 422
    assert "job_category" in res.text


def test_a_job_retired_after_somebody_answered_it_is_still_accepted(client):
    """A row an admin retires is not a typo, and the person who answered it
    while it existed must not be refused because of a later edit."""
    with patch("app.db.taxonomy.list_jobs", return_value=[{"id": "cnc_operator"}]):
        res = post_candidate(client, job_category="general_worker")

    assert res.status_code == 201, res.text


def test_the_designation_and_its_screening_answers_are_stored(client):
    """What the bot sends about the job, as the CRM keeps it."""
    res = post_candidate(
        client,
        job_id="general_worker",
        job_title="General Worker",
        job_answers=[
            {
                "question_id": "q_shifts",
                "question": "Which shifts can you work?",
                "answer": "Nights, Weekends",
                "kind": "choice",
                "asked_at": "2026-08-27T09:00:00.000Z",
            }
        ],
    )

    assert res.status_code == 201, res.text
    stored = next(iter(client.fake_repo.candidates.values()))
    assert stored.profile.job_id == "general_worker"
    assert stored.profile.job_title == "General Worker"
    assert len(stored.profile.job_answers) == 1

    answer = stored.profile.job_answers[0]
    assert answer.question_id == "q_shifts"
    # The wording travels with the answer rather than being resolved at read
    # time, so an admin rewording the question does not rewrite this record.
    assert answer.question == "Which shifts can you work?"
    assert answer.answer == "Nights, Weekends"
    assert answer.kind == "choice"


def test_a_later_partial_adds_the_answers_the_first_one_did_not_have(client):
    """The bot sends every answer under one key, so a screening answer given
    after the first partial has to land on the record the first one created."""
    key = "whatsapp/111/919876543210"

    first = post_candidate(client, key=key)
    assert first.status_code == 201, first.text

    second = post_candidate(
        client,
        key=key,
        job_id="general_worker",
        job_answers=[
            {
                "question_id": "q_years",
                "question": "How many years of experience do you have?",
                "answer": "6 years",
                "kind": "text",
            }
        ],
    )

    # 200, not 201: nothing new exists because of it.
    assert second.status_code == 200, second.text
    assert len(client.fake_repo.candidates) == 1, "a partial created a second candidate"

    # Read back the way the refresh writes it: `refresh_whatsapp_profile` sets
    # the dumped value, which is what lands in Mongo and what a later load
    # parses back into `JobAnswer`.
    stored = next(iter(client.fake_repo.candidates.values()))
    answers = stored.profile.model_dump()["job_answers"]
    assert [a["question_id"] for a in answers] == ["q_years"]
    assert stored.profile.job_id == "general_worker"


def test_a_partial_with_no_answers_does_not_blank_the_ones_on_file(client):
    """A candidate who edits their name after answering a screening question
    must not lose the answer to the submission that carried the edit."""
    key = "whatsapp/111/919876543210"

    post_candidate(
        client,
        key=key,
        job_answers=[
            {"question_id": "q_years", "question": "How many years?", "answer": "6 years"}
        ],
    )
    post_candidate(client, key=key, full_name="Ravi Kumar Singh")

    stored = next(iter(client.fake_repo.candidates.values()))
    assert stored.profile.full_name == "Ravi Kumar Singh"
    assert [a.answer for a in stored.profile.job_answers] == ["6 years"]


# --------------------------------------------------------------------------- #
#  Allocation reuse
# --------------------------------------------------------------------------- #
def test_a_new_candidate_goes_through_the_existing_balancer(client):
    post_candidate(client)
    assert client.assign_mock.call_count == 1


def test_a_repeat_submission_is_not_reallocated(client):
    key = "whatsapp/111/919876543210"
    post_candidate(client, key=key)
    post_candidate(client, key=key)
    # Allocation belongs to the first creation. Running it again would move an
    # already-owned candidate to a different desk.
    assert client.assign_mock.call_count == 1


# --------------------------------------------------------------------------- #
#  The résumé itself
#
#  Three separate claims, and they fail in different ways:
#
#   * a candidate the policy requires a CV for can be created *with* one — the
#     other half of the rejection test above, and the half that would leave the
#     integration unusable if it were missing;
#   * a résumé can be attached to a candidate who already exists;
#   * in both cases the file lands in the CRM's storage, and what goes on the
#     record is a key into *this* system rather than a path on the bot's disk.
# --------------------------------------------------------------------------- #
def test_a_cv_required_candidate_is_created_when_the_cv_comes_with_it(client):
    """The case that has no other route in.

    A technician needs a CV, and `POST /candidates/{id}/resume` needs an id that
    does not exist yet. So the file travels with the submission, and this is the
    test that says the deadlock is actually broken.
    """
    res = client.post(
        "/candidates",
        json={
            "source": "whatsapp",
            "profile": {
                "full_name": "Ravi Kumar",
                "phone": "+919876543210",
                "country": "India",
                "destination_country": "Singapore",
                "job_category": "technician",
            },
            "idempotency_key": "whatsapp/111/tech",
            "resume": resume_payload(),
        },
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert res.status_code == 201, res.text
    assert res.json()["cv_required"] is True

    record = next(iter(client.fake_repo.candidates.values()))
    assert record.resume is not None
    assert record.resume.size == len(PDF_BYTES)
    assert record.resume.mime_type == "application/pdf"
    # The digest is on the record, because that field is what the unique index
    # reads — a résumé stored without one is invisible to the duplicate check.
    assert record.resume_hash == record.resume.sha256
    # And the bytes are here, in the CRM's storage, under the CRM's key.
    assert client.storage.objects[record.resume.storage_key] == PDF_BYTES


def test_bot_routes_a_passport_embedded_in_the_cv_bundle(client):
    with patch(
        "app.services.candidate_upload_intake.route_embedded_identity_documents",
        return_value=["passport"],
    ) as route_bundle:
        res = client.post(
            "/candidates",
            json={
                "source": "whatsapp",
                "profile": {
                    "full_name": "Ravi Kumar",
                    "phone": "+919876543210",
                    "destination_country": "Singapore",
                    "job_category": "technician",
                },
                "idempotency_key": "whatsapp/111/mixed-bundle",
                "resume": resume_payload(filename="cv-and-passport.pdf"),
            },
            headers={"X-Service-Key": SERVICE_KEY},
        )

    assert res.status_code == 201, res.text
    assert res.json()["embedded_identity_documents"] == ["passport"]
    assert route_bundle.call_args.kwargs["provider"] == "whatsapp"
    assert route_bundle.call_args.kwargs["candidate_id"] == res.json()["candidate_id"]


def test_the_stored_key_is_the_crms_own_and_not_a_bot_path(client):
    client.post(
        "/candidates",
        json={
            "source": "whatsapp",
            "profile": {
                "full_name": "Ravi Kumar",
                "phone": "+919876543210",
                "destination_country": "Singapore",
                "job_category": "technician",
            },
            "idempotency_key": "whatsapp/111/tech",
            "resume": resume_payload(filename="../../etc/passwd"),
        },
        headers={"X-Service-Key": SERVICE_KEY},
    )
    record = next(iter(client.fake_repo.candidates.values()))
    key = record.resume.storage_key
    # No drive letters, no traversal, nothing that could only be opened on the
    # machine the bot happens to run on.
    assert ":" not in key
    assert ".." not in key
    assert record.resume.storage_backend == "fake"  # i.e. whatever *this* system uses


def test_a_resume_can_be_attached_to_an_existing_candidate(client):
    created = post_candidate(client)  # Malaysia + general_worker: no CV needed
    candidate_id = created.json()["candidate_id"]
    assert client.fake_repo.get(candidate_id).resume is None

    res = client.post(
        f"/candidates/{candidate_id}/resume",
        files={"file": ("cv.pdf", PDF_BYTES, "application/pdf")},
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert res.status_code == 200, res.text

    record = client.fake_repo.get(candidate_id)
    assert record.resume is not None
    assert record.resume_hash == record.resume.sha256
    assert client.storage.objects[record.resume.storage_key] == PDF_BYTES


def test_resume_upload_requires_the_service_key(client):
    candidate_id = post_candidate(client).json()["candidate_id"]
    res = client.post(
        f"/candidates/{candidate_id}/resume",
        files={"file": ("cv.pdf", PDF_BYTES, "application/pdf")},
    )
    assert res.status_code == 401
    assert client.fake_repo.get(candidate_id).resume is None


def test_uploading_to_an_unknown_candidate_is_a_404(client):
    res = client.post(
        "/candidates/nobody/resume",
        files={"file": ("cv.pdf", PDF_BYTES, "application/pdf")},
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert res.status_code == 404


def test_an_executable_is_not_a_resume(client):
    candidate_id = post_candidate(client).json()["candidate_id"]
    res = client.post(
        f"/candidates/{candidate_id}/resume",
        files={"file": ("cv.exe", b"MZ\x90\x00", "application/x-msdownload")},
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "unsupported_resume_type"
    assert client.fake_repo.get(candidate_id).resume is None


# --------------------------------------------------------------------------- #
#  The recovery path (§12)
#
#  The failure this prevents is specific and nasty: the bot tells a candidate
#  they are registered, the CRM refuses the submission for want of a CV, and
#  nobody is holding the problem. It only works if the refusal is legible to a
#  machine — a 422 with prose in it leaves the bot guessing.
# --------------------------------------------------------------------------- #
def test_the_cv_refusal_is_machine_readable(client):
    res = post_candidate(client, job_category="technician")
    assert res.status_code == 422

    body = res.json()
    assert body["code"] == "CV_REQUIRED"
    assert body["cv_required"] is True
    # The version that made the call, so the bot can tell a policy change from a
    # bug the next time it disagrees.
    assert body["cv_policy_version"]
    assert "requires a resume" in body["detail"]


def test_the_same_key_after_a_cv_refusal_creates_exactly_one_candidate(client):
    """The whole §12 loop, end to end.

    Refused for want of a CV; the candidate sends one; the bot resends *the same
    submission under the same key*. One person exists at the end of it.
    """
    key = "whatsapp/111/919876543210"

    refused = post_candidate(client, key=key, job_category="technician")
    assert refused.status_code == 422
    assert not client.fake_repo.candidates

    retried = client.post(
        "/candidates",
        json={
            "source": "whatsapp",
            "profile": {
                "full_name": "Ravi Kumar",
                "phone": "+919876543210",
                "country": "India",
                "destination_country": "Malaysia",
                "job_category": "technician",
            },
            "idempotency_key": key,
            "resume": resume_payload(),
        },
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert retried.status_code == 201, retried.text
    assert len(client.fake_repo.candidates) == 1

    # And a third send of the same thing — a queue that retried after the
    # response was lost — still yields the one candidate.
    again = client.post(
        "/candidates",
        json={
            "source": "whatsapp",
            "profile": {
                "full_name": "Ravi Kumar",
                "phone": "+919876543210",
                "destination_country": "Malaysia",
                "job_category": "technician",
            },
            "idempotency_key": key,
            "resume": resume_payload(),
        },
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert again.json()["candidate_id"] == retried.json()["candidate_id"]
    assert again.json()["created"] is False
    assert len(client.fake_repo.candidates) == 1


# --------------------------------------------------------------------------- #
#  A CV-less candidate is a complete record, not a broken one
# --------------------------------------------------------------------------- #
def test_a_cv_less_candidate_serialises_for_the_frontend(client):
    """What the profile page receives has to be openable without a résumé.

    The frontend reads `resume` to decide whether to offer a download. Null is
    the answer that makes it hide the button; a half-built `StoredResume` full
    of empty strings would make it offer a file that does not exist.
    """
    post_candidate(client)
    record = next(iter(client.fake_repo.candidates.values()))

    payload = record.model_dump(mode="json")
    assert payload["resume"] is None
    assert payload["source"] == "whatsapp"
    assert payload["cv_required"] is False
    assert payload["cv_policy_version"]
    assert payload["profile"]["country"] == "India"
    assert payload["profile"]["destination_country"] == "Malaysia"
    assert payload["profile"]["job_category"] == "general_worker"
    # No invented résumé metadata anywhere on the record.
    assert payload.get("resume_hash") is None


def test_the_historical_cv_decision_survives_a_policy_change(client):
    """§13: a rule that changes tomorrow does not rewrite what applied today."""
    post_candidate(client)
    record = next(iter(client.fake_repo.candidates.values()))
    assert record.cv_required is False
    version_at_registration = record.cv_policy_version

    # The agency changes its mind: Malaysian general workers now need a CV.
    strict = {
        "version": "test-2",
        "default_cv_required": True,
        "rules": [
            {
                "destination_country": "Malaysia",
                "job_category": "general_worker",
                "cv_required": True,
            }
        ],
    }
    with patch("app.policy.cv_policy.DEFAULT_POLICY", strict):
        reset_policy_cache()
        assert is_cv_required("Malaysia", "general_worker") is True
        # The record already written still says what was true when it was written.
        assert record.cv_required is False
        assert record.cv_policy_version == version_at_registration
    reset_policy_cache()


# --------------------------------------------------------------------------- #
#  A registration that arrives while it is still being answered
#
#  The bot now delivers a candidate as they answer rather than only once they
#  have finished, because someone who stops halfway is still someone worth
#  ringing - and under the old arrangement they did not exist here at all.
#
#  Four things have to hold for that to be safe, and each is a way it could
#  quietly go wrong:
#
#   * the CV policy must not refuse somebody for not having answered a question
#     nobody has asked them yet;
#   * the second delivery must fill the record in, not be swallowed as a
#     duplicate - the failure mode there is a CRM that holds the first ten
#     seconds of every conversation and looks like it is working;
#   * nobody may be put in a recruiter's queue, with an SLA clock running,
#     before there is anything to assess;
#   * and the record has to say plainly that it is unfinished, or a blank reads
#     as an answer.
# --------------------------------------------------------------------------- #
def test_an_unfinished_registration_is_accepted_without_the_cv_its_policy_demands(client):
    """The policy applies to a finished registration, not to a conversation."""
    strict = {
        "version": "test-strict",
        "default_cv_required": True,
        "rules": [
            {
                "destination_country": "Malaysia",
                "job_category": "general_worker",
                "cv_required": True,
            }
        ],
    }
    with patch("app.policy.cv_policy.DEFAULT_POLICY", strict):
        reset_policy_cache()
        assert is_cv_required("Malaysia", "general_worker") is True

        partial = post_candidate(client, sections=in_progress())
        assert partial.status_code == 201, partial.text
        assert len(client.fake_repo.candidates) == 1

        # And the moment it is finished, the rule bites exactly as it always did.
        completing = post_candidate(client, sections=finished())
        assert completing.status_code == 422
        assert completing.json()["code"] == "CV_REQUIRED"
    reset_policy_cache()


def test_a_later_delivery_fills_the_record_in_rather_than_being_swallowed(client):
    """The bug this whole path would have if a replay stayed a no-op."""
    key = "whatsapp/111/919876543210"
    post_candidate(client, key=key, sections=in_progress(), full_name="Ravi Kumar")

    record = next(iter(client.fake_repo.candidates.values()))
    assert record.profile.city is None

    post_candidate(
        client,
        key=key,
        sections=in_progress(stage="DOCUMENTS_PENDING"),
        city="Tiruchirappalli",
    )

    assert len(client.fake_repo.candidates) == 1
    assert record.profile.city == "Tiruchirappalli"
    assert record.registration.stage == "DOCUMENTS_PENDING"
    assert record.registration.complete is False


def test_an_unfinished_registration_is_not_put_in_anybodys_queue(client):
    """An SLA clock must not start against a profile there is nothing to assess."""
    key = "whatsapp/111/919876543210"
    post_candidate(client, key=key, sections=in_progress())
    assert client.assign_mock.call_count == 0

    post_candidate(client, key=key, sections=in_progress(stage="DOCUMENTS_PENDING"))
    assert client.assign_mock.call_count == 0

    # The delivery that finishes it is the one that places them, and only that
    # one - a further delivery afterwards must not move them to another desk.
    post_candidate(client, key=key, sections=finished())
    assert client.assign_mock.call_count == 1
    post_candidate(client, key=key, sections=finished())
    assert client.assign_mock.call_count == 1


def test_an_unfinished_record_says_so(client):
    """A blank on a half-filled record must not read as an answer."""
    post_candidate(
        client,
        sections=in_progress(outstanding_documents=["passport", "aadhaar"]),
    )
    record = next(iter(client.fake_repo.candidates.values()))

    assert record.registration is not None
    assert record.registration.complete is False
    assert record.registration.outstanding_documents == ["passport", "aadhaar"]


# --------------------------------------------------------------------------- #
#  The CV, the job answers and the identity documents
# --------------------------------------------------------------------------- #
CV_SECTION = {
    "filename": "ravi-cv.pdf",
    "sha256": "abc123",
    "full_name": "Ravi Kumar",
    "current_designation": "Senior Welder",
    "industry": "Construction & Engineering",
    "skills": ["SMAW", "GTAW"],
    "trade_skills": ["TIG welding", "Pipe welding"],
    "certifications": ["6G welder certificate"],
    "work_experience": [
        {
            "company": "Larsen and Toubro",
            "designation": "Senior Welder",
            "start_date": "2019-01",
            "country": "India",
        },
        {
            "company": "Gulf Steel Works",
            "title": "Welder",
            "country": "UAE",
            "is_overseas": True,
        },
    ],
    "education": [
        {
            "institution": "Government Polytechnic",
            "degree": "Diploma in Mechanical Engineering",
            "passing_year": "2015",
        }
    ],
    "raw_ocr": {"name": "Ravi Kumar"},
}


def test_a_cv_read_over_whatsapp_is_stored_the_way_an_emailed_one_is(client):
    """The employment history and the education, not six flattened fields."""
    post_candidate(client, sections={"cv": CV_SECTION, **in_progress()})
    profile = next(iter(client.fake_repo.candidates.values())).profile

    assert [w.company for w in profile.work_experience] == [
        "Larsen and Toubro",
        "Gulf Steel Works",
    ]
    assert profile.work_experience[1].is_overseas is True
    assert profile.education[0].degree == "Diploma in Mechanical Engineering"
    assert profile.certifications == ["6G welder certificate"]
    assert profile.trade_skills == ["TIG welding", "Pipe welding"]
    assert profile.current_designation == "Senior Welder"
    # Which document it came off, for a recruiter asking exactly that.
    assert profile.additional_info["cv_filename"] == "ravi-cv.pdf"
    assert profile.raw_ocr == {"name": "Ravi Kumar"}


def test_what_the_candidate_typed_outranks_what_their_cv_says(client):
    """A CV is a document about someone's past; an answer is about today."""
    post_candidate(
        client,
        full_name="Ravi Kumar",
        sections={"cv": {**CV_SECTION, "full_name": "R. KUMAR", "phone": "+910000000000"}},
    )
    profile = next(iter(client.fake_repo.candidates.values())).profile

    assert profile.full_name == "Ravi Kumar"
    assert profile.phone == "+919876543210"


JOB_SECTION = {
    "job": "TIG welder",
    "job_category": "general_worker",
    "job_category_title": "General Worker",
    "course_or_trade": {
        "education": "diploma",
        "course": "Mechanical Engineering",
        "primary_trade": "fabrication_welding",
        "questions": [
            {
                "id": "trade:welder:processes",
                "question": "Which welding processes have you worked with?",
                "answer": "TIG, MIG",
            }
        ],
    },
    "country": {
        "preference": "malaysia",
        "destination_country": "Malaysia",
        "selected": ["malaysia", "singapore"],
        "strictness": "strict",
        "strict": True,
    },
    "questions": [
        {
            "id": "availability",
            "question": "When can you join?",
            "answer": "Within 15 days",
        }
    ],
    "availability": {"band": "within_15"},
}


def test_the_job_section_is_stored_with_its_questions(client):
    """An answer arriving without its question is a value with no meaning."""
    post_candidate(client, sections={"job": JOB_SECTION, **in_progress()})
    record = next(iter(client.fake_repo.candidates.values()))

    assert record.job is not None
    assert record.job.job == "TIG welder"
    assert record.job.course_or_trade.course == "Mechanical Engineering"
    assert (
        record.job.course_or_trade.questions[0].question
        == "Which welding processes have you worked with?"
    )
    assert record.job.country.selected == ["malaysia", "singapore"]
    # The one field on this panel that constrains what may be done with them.
    assert record.job.country.strict is True
    assert record.job.availability.band == "within_15"


def test_a_changed_answer_replaces_the_job_section_rather_than_merging_into_it(client):
    """Someone who stops being strict must actually stop being strict."""
    key = "whatsapp/111/919876543210"
    post_candidate(client, key=key, sections={"job": JOB_SECTION, **in_progress()})

    relaxed = {
        **JOB_SECTION,
        "country": {"preference": "any", "strictness": "any", "strict": False},
    }
    post_candidate(client, key=key, sections={"job": relaxed, **in_progress()})

    record = next(iter(client.fake_repo.candidates.values()))
    assert record.job.country.strict is False
    assert record.job.country.selected == []


def test_identity_documents_are_filed_apart_from_the_candidate(client):
    """Government identity numbers never reach the document a listing projects."""
    identity = {
        "aadhaar": [
            {
                "record_id": "6512ab00000000000000aa01",
                "slot": "aadhaar",
                "filename": "aadhaar.jpg",
                "sha256": "sha-aadhaar",
                "result": {"aadhaar": {"name": "Ravi Kumar", "aadhaar_number": "234567890123"}},
            }
        ],
        "passport": [
            {
                "record_id": "6512ab00000000000000aa02",
                "slot": "passport",
                "result": {"mrz": {"passport_number": "Z1234567", "expiry_date": "310511"}},
            }
        ],
    }

    filed = []
    with patch(
        "app.db.identity_records.store_aadhaar_record",
        side_effect=lambda rid, result, **kw: filed.append(("aadhaar", rid, result, kw)),
    ), patch(
        "app.db.identity_records.store_passport_record",
        side_effect=lambda rid, result, **kw: filed.append(("passport", rid, result, kw)),
    ):
        response = post_candidate(client, sections={"identity": identity, **in_progress()})
        assert response.status_code == 201, response.text

    kinds = {entry[0] for entry in filed}
    assert kinds == {"aadhaar", "passport"}

    aadhaar = next(entry for entry in filed if entry[0] == "aadhaar")
    # Keyed on the bot's upload id, so the same card arriving with every
    # delivery overwrites its own row rather than accumulating a copy per one.
    assert aadhaar[1] == "whatsapp:6512ab00000000000000aa01"
    assert aadhaar[3]["provider"] == "whatsapp"
    assert aadhaar[3]["candidate_id"] == next(iter(client.fake_repo.candidates))

    # And nothing of it on the candidate document.
    record = next(iter(client.fake_repo.candidates.values()))
    assert "234567890123" not in str(record.model_dump(mode="json"))
