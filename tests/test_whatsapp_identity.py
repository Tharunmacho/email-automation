"""The Aadhaar and passport a candidate sends over WhatsApp, all the way through.

The email pipeline has always filed these: a bundle arrives, the classifier
finds the passport on page 55, `multipass` writes the record. A WhatsApp
candidate sends the same two documents as two photographs, and the intake model
is an allow-list that named no `identity` — so they were dropped at the door and
a recruiter opening the profile saw nothing at all.

What has to be true now, and each of these is a way it would be wrong rather
than merely absent:

* **The record carries what was read, not a summary of it.** A passport row with
  a number on it and no expiry date is worse than no row: overseas placement
  turns on the expiry, and a recruiter who can see the passport is on file will
  not go looking for the date somewhere else.
* **The file is there and it is the right one.** The download endpoint serves
  whatever the record points at. A scan filed against the wrong candidate is
  not a bug that shows up as an error — it shows up as a recruiter reading
  somebody else's passport under this candidate's name.
* **A document sent on Friday reaches the candidate created on Tuesday.**
  Documents are the last thing a registration collects, so the late upload is
  the normal case, not the edge one.
* **The email path does not change.** It has no `file` block, its documents are
  pages of a stored bundle, and both still work.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app as fastapi_app, current_user
from app.core.models import CandidateProfile, CandidateRecord, SourceEmail, StoredResume

SERVICE_KEY = "test-service-key-12345"

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes-for-a-passport-page"
OTHER_JPEG = b"\xff\xd8\xff\xe0a-completely-different-photograph"


# --------------------------------------------------------------------------- #
#  Doubles
# --------------------------------------------------------------------------- #
class FakeRepo:
    def __init__(self):
        self.candidates: dict[str, CandidateRecord] = {}

    def find_by_idempotency_key(self, key):
        if not key:
            return None
        return next((c for c in self.candidates.values() if c.idempotency_key == key), None)

    def find_by_email_or_phone(self, email_key, phone_key):
        for record in self.candidates.values():
            if phone_key and record.phone_key == phone_key:
                return record
        return None

    def find_by_resume_hash(self, resume_hash):
        return None

    def get(self, candidate_id):
        return self.candidates.get(candidate_id)

    def insert(self, record: CandidateRecord) -> str:
        existing = self.find_by_idempotency_key(record.idempotency_key)
        if existing:
            return existing.id
        self.candidates[record.id] = record
        return record.id

    def refresh_whatsapp_profile(self, candidate_id, profile):
        record = self.candidates.get(candidate_id)
        if record:
            record.profile = profile
        return True

    def adopt_idempotency_key(self, candidate_id, key):
        record = self.candidates.get(candidate_id)
        if record and not record.idempotency_key:
            record.idempotency_key = key
            return True
        return False

    def attach_resume(self, candidate_id, resume):
        record = self.candidates.get(candidate_id)
        if record:
            record.resume = resume
            record.resume_hash = resume.sha256
        return bool(record)


class FakeCollection:
    """A dictionary that upserts and matches the way the two callers need.

    Only the operators `identity_records` actually uses: `$set` and
    `$setOnInsert`, matched on `_id` and optionally `candidate_id`. Enough to
    prove the natural key does what it claims — a re-send overwrites its own
    row — without a database.
    """

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def _matches(self, doc: dict, query: dict) -> bool:
        return all(doc.get(key) == value for key, value in query.items())

    def find_one(self, query):
        for doc in self.docs.values():
            if self._matches(doc, query):
                return dict(doc)
        return None

    def find(self, query):
        return [dict(d) for d in self.docs.values() if self._matches(d, query)]

    def update_one(self, query, update, upsert=False):
        existing = self.find_one(query)
        if existing is None and not upsert:
            return type("Result", (), {"matched_count": 0})()
        doc = dict(existing or {})
        if existing is None:
            doc.update(update.get("$setOnInsert", {}))
            doc["_id"] = query["_id"]
        doc.update(update.get("$set", {}))
        self.docs[doc["_id"]] = doc
        return type("Result", (), {"matched_count": 0 if existing is None else 1})()


class FakeStorage:
    name = "fake"

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.writes: list[str] = []

    def save(self, key: str, data: bytes, content_type=None) -> str:
        self.objects[key] = data
        self.writes.append(key)
        return key

    def load(self, key: str) -> bytes:
        return self.objects[key]


# --------------------------------------------------------------------------- #
#  Payloads, in the shape the bot actually builds them
# --------------------------------------------------------------------------- #
def passport_document(**overrides) -> dict:
    """One passport, with the extractor's payload untouched inside it.

    Deliberately a full payload rather than a number: the claim under test is
    that everything read survives the crossing, and a fixture carrying one
    field could not tell the difference.
    """
    document = {
        "record_id": "66b1f0c2e4b0a1d2c3e4f501",
        "slot": "passport",
        "filename": "passport.jpg",
        "mime_type": "image/jpeg",
        "sha256": "digest-of-the-passport-photo",
        "message_id": "wamid.HBgMOTE5ODc2NTQzMjEw",
        "uploaded_at": "2026-08-27T09:00:00.000Z",
        "extracted_at": "2026-08-27T09:00:11.000Z",
        "result": {
            "mrz": {
                "passport_number": "Z1234567",
                "surname": "Shah",
                "given_names": "Nasim",
                "nationality": "IND",
                "issuing_country": "IND",
                "date_of_birth": "1994-02-17",
                "sex": "M",
                "expiry_date": "2031-03-14",
                "date_of_issue": "2021-03-15",
                "personal_number": "PN99",
                "all_check_digits_valid": True,
            },
            "fields": {"place_of_issue": "CHENNAI"},
            "mrz_source": "td3",
            "raw_mrz": "P<INDSHAH<<NASIM<<<<<<<<<<<<<<<<<<<<<<<<<<<<",
            "confidence": 0.97,
            "warnings": ["the second line was read at low contrast"],
        },
    }
    document.update(overrides)
    return document


def aadhaar_document(**overrides) -> dict:
    document = {
        "record_id": "66b1f0c2e4b0a1d2c3e4f502",
        "slot": "aadhaar",
        "filename": "aadhaar-front.jpg",
        "mime_type": "image/jpeg",
        "sha256": "digest-of-the-aadhaar-photo",
        "message_id": "wamid.HBgMOTE5ODc2NTQzMjEx",
        "result": {
            "aadhaar": {
                "name": "Nasim Shah",
                "aadhaar_number": "123412349017",
                "aadhaar_number_valid": True,
                "date_of_birth": "1994-02-17",
                "gender": "M",
                "mobile_number": "9876543210",
                "address": "Vill Chaturbuhjwa, West Champaran, Bihar",
                "care_of": "S/O Imran Shah",
                "pincode": "845449",
                "vid": "9876543210987654",
                "enrollment_id": "1234/56789/01234",
                "document_side": "front",
            },
            "warnings": [],
        },
    }
    document.update(overrides)
    return document


def submission(*, key="whatsapp/111/919876543210", identity=None, **profile_over) -> dict:
    body = {
        "source": "whatsapp",
        "profile": {
            "full_name": "Nasim Shah",
            "phone": "+919876543210",
            "phone_e164": "+919876543210",
            "country": "India",
            "destination_country": "Malaysia",
            "job_category": "general_worker",
        },
        "idempotency_key": key,
    }
    body["profile"].update(profile_over)
    if identity is not None:
        body["identity"] = identity
    return body


@pytest.fixture
def api():
    """The CRM with its database replaced and nothing else changed.

    `TestClient(app)` rather than the context manager, because entering it
    fires the startup event and reaches the real Mongo that `conftest` refuses.
    """
    repo = FakeRepo()
    storage = FakeStorage()
    aadhaar = FakeCollection()
    passport = FakeCollection()

    def sign_in_as(role: str, user_id: str = "staff-1"):
        fastapi_app.dependency_overrides[current_user] = lambda: {
            "id": user_id,
            "email": f"{user_id}@x.com",
            "name": user_id,
            "role": role,
        }

    with patch("app.api.routes.ensure_indexes"), patch(
        "app.config.settings.whatsapp_service_key", SERVICE_KEY
    ), patch("app.api.routes.repo", return_value=repo), patch(
        "app.services.resume_store.get_storage_backend", return_value=storage
    ), patch(
        "app.services.identity_files.get_storage_backend", return_value=storage
    ), patch(
        "app.db.identity_records.get_aadhaar_collection", return_value=aadhaar
    ), patch(
        "app.db.identity_records.get_passport_collection", return_value=passport
    ), patch(
        "app.services.candidate_intake.assign_candidate"
    ):
        client = TestClient(fastapi_app)
        client.fake_repo = repo  # type: ignore[attr-defined]
        client.storage = storage  # type: ignore[attr-defined]
        client.aadhaar = aadhaar  # type: ignore[attr-defined]
        client.passport = passport  # type: ignore[attr-defined]
        client.sign_in_as = sign_in_as  # type: ignore[attr-defined]
        try:
            yield client
        finally:
            fastapi_app.dependency_overrides.pop(current_user, None)


def post(api, body: dict):
    return api.post("/candidates", json=body, headers={"X-Service-Key": SERVICE_KEY})


def allocate(api, candidate_id: str, staff_id: str = "staff-1") -> str:
    """Put the candidate on a recruiter's desk, as the balancer would.

    `assign_candidate` is mocked out in the fixture, so a candidate created
    here has no owner — and a staff member is served 404 for a record that is
    not theirs, correctly. Any test that looks at the profile as a recruiter
    rather than an administrator has to say whose it is.
    """
    api.fake_repo.candidates[candidate_id].assigned_staff_id = staff_id
    return candidate_id


def upload(api, candidate_id, document_type, record_id, data=JPEG, name="passport.jpg",
           mime="image/jpeg"):
    return api.post(
        f"/candidates/{candidate_id}/identity/{document_type}/{record_id}/file",
        files={"file": (name, data, mime)},
        headers={"X-Service-Key": SERVICE_KEY},
    )


# --------------------------------------------------------------------------- #
#  1 & 2 — the record, and everything that was read onto it
# --------------------------------------------------------------------------- #
def test_a_passport_sent_over_whatsapp_becomes_a_crm_identity_record(api):
    response = post(api, submission(identity={"passport": [passport_document()]}))

    assert response.status_code == 201, response.text
    assert response.json()["identity_documents"] == [
        {
            "document_type": "passport",
            "record_id": "66b1f0c2e4b0a1d2c3e4f501",
            "stored": True,
        }
    ]
    assert len(api.passport.docs) == 1


def test_every_field_the_extractor_read_is_stored(api):
    """Not just the number.

    The projection is `store_passport_record`, which the email pipeline also
    feeds — so this is the same set of fields a recruiter already sees on an
    emailed passport, arriving by a different door.
    """
    post(api, submission(identity={"passport": [passport_document()]}))
    stored = next(iter(api.passport.docs.values()))

    assert stored["passport_number"] == "Z1234567"
    assert stored["surname"] == "Shah"
    assert stored["given_names"] == "Nasim"
    assert stored["nationality"] == "IND"
    assert stored["date_of_birth"] == "1994-02-17"
    assert stored["sex"] == "M"
    # The one overseas placement turns on.
    assert stored["expiry_date"] == "2031-03-14"
    assert stored["date_of_issue"] == "2021-03-15"
    assert stored["personal_number"] == "PN99"
    # The passport's own integrity test, and the page fields the MRZ cannot
    # encode.
    assert stored["check_digits_valid"] is True
    assert stored["printed_fields"] == {"place_of_issue": "CHENNAI"}
    assert stored["confidence"] == 0.97
    assert stored["warnings"] == ["the second line was read at low contrast"]
    # And the payload itself, so a mapping bug is recoverable without asking
    # the candidate for their passport again.
    assert stored["raw"]["mrz"]["passport_number"] == "Z1234567"


def test_an_aadhaar_is_projected_and_masked_the_same_way(api):
    post(api, submission(identity={"aadhaar": [aadhaar_document()]}))
    stored = next(iter(api.aadhaar.docs.values()))

    assert stored["aadhaar_number"] == "123412349017"
    # Derived here rather than trusted from the payload, so every screen that
    # only needs to show *which* card this is has something safe to show.
    assert stored["masked_aadhaar_number"] == "XXXXXXXX9017"
    assert stored["aadhaar_number_valid"] is True
    assert stored["address"] == "Vill Chaturbuhjwa, West Champaran, Bihar"
    assert stored["care_of"] == "S/O Imran Shah"
    assert stored["pincode"] == "845449"
    assert stored["enrollment_id"] == "1234/56789/01234"
    assert stored["document_side"] == "front"


def test_both_documents_travel_on_one_submission(api):
    body = submission(
        identity={"aadhaar": [aadhaar_document()], "passport": [passport_document()]}
    )
    response = post(api, body)

    assert {e["document_type"] for e in response.json()["identity_documents"]} == {
        "aadhaar",
        "passport",
    }
    assert len(api.aadhaar.docs) == 1
    assert len(api.passport.docs) == 1


# --------------------------------------------------------------------------- #
#  4 — provenance, and the candidate it belongs to
# --------------------------------------------------------------------------- #
def test_provenance_says_which_conversation_and_which_message(api):
    """The same `source` block the email path fills with a Gmail message id.

    "Where did this passport come from" has to have an answer on both paths, or
    the answer is worth nothing on either.
    """
    post(api, submission(identity={"passport": [passport_document()]}))
    source = next(iter(api.passport.docs.values()))["source"]

    assert source["provider"] == "whatsapp"
    # Read out of the idempotency key: `whatsapp/{account}/{wa_id}`.
    assert source["account_id"] == "111"
    assert source["message_id"] == "wamid.HBgMOTE5ODc2NTQzMjEw"
    assert source["attachment_id"] == "66b1f0c2e4b0a1d2c3e4f501"
    assert source["filename"] == "passport.jpg"
    assert source["sha256"] == "digest-of-the-passport-photo"
    # A WhatsApp upload is one document in one file. Claiming a page would be
    # inventing one — the pages list is how the email path says "page 55 of the
    # bundle".
    assert source["pages"] == []


def test_the_record_is_filed_against_the_candidate_the_intake_resolved(api):
    response = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = response.json()["candidate_id"]

    assert next(iter(api.passport.docs.values()))["candidate_id"] == candidate_id


def test_a_document_already_filed_under_someone_else_is_refused(api):
    """The record id is stable so a re-send overwrites its own row. That same
    stability means a document arriving under a second candidate would *move*
    rather than duplicate — silently, with no trace of where it had been."""
    first = post(api, submission(identity={"passport": [passport_document()]}))
    owner = first.json()["candidate_id"]

    second = post(
        api,
        submission(
            key="whatsapp/111/919000000001",
            phone="+919000000001",
            phone_e164="+919000000001",
            identity={"passport": [passport_document()]},
        ),
    )

    assert second.status_code == 201
    entry = second.json()["identity_documents"][0]
    assert entry["stored"] is False
    assert entry["skipped"] == "belongs to another candidate"
    # Still the first candidate's, untouched.
    assert next(iter(api.passport.docs.values()))["candidate_id"] == owner


def test_one_document_failing_does_not_take_the_other_down(api):
    """A payload the projection chokes on is logged, not raised — the candidate
    is already written and a bad Aadhaar must not undo a registration."""
    broken = aadhaar_document(result={"aadhaar": "not-a-mapping"})
    response = post(
        api, submission(identity={"aadhaar": [broken], "passport": [passport_document()]})
    )

    assert response.status_code == 201
    entries = {e["document_type"]: e for e in response.json()["identity_documents"]}
    assert entries["aadhaar"]["stored"] is False
    assert entries["passport"]["stored"] is True


# --------------------------------------------------------------------------- #
#  5 & 6 — the late upload, and the candidate it has to find
# --------------------------------------------------------------------------- #
def test_a_document_sent_later_lands_on_the_candidate_already_on_file(api):
    """Documents are the last thing a registration collects, so this is the
    normal case. One candidate, not two."""
    first = post(api, submission())
    assert first.status_code == 201
    candidate_id = first.json()["candidate_id"]

    later = post(api, submission(identity={"passport": [passport_document()]}))

    # 200, not 201: nothing new exists because of it.
    assert later.status_code == 200, later.text
    assert later.json()["candidate_id"] == candidate_id
    assert len(api.fake_repo.candidates) == 1, "a late document created a second candidate"
    assert next(iter(api.passport.docs.values()))["candidate_id"] == candidate_id


def test_a_document_reaches_a_candidate_found_by_phone_rather_than_key(api):
    """The agency runs several WhatsApp lines. Somebody who registered on one
    and sent their passport from another is one person, and their document has
    to reach the record that already exists — which the phone, not the key, is
    what finds."""
    first = post(api, submission(key="whatsapp/101/919876543210"))
    candidate_id = first.json()["candidate_id"]

    later = post(
        api,
        submission(
            key="whatsapp/606/919876543210",
            identity={"passport": [passport_document()]},
        ),
    )

    assert later.status_code == 200
    assert later.json()["candidate_id"] == candidate_id
    assert len(api.fake_repo.candidates) == 1
    assert next(iter(api.passport.docs.values()))["candidate_id"] == candidate_id


def test_the_passport_number_still_reaches_the_profile_beside_the_record(api):
    """Two destinations, one document, and both still work.

    The number and the expiry go on the candidate profile, because overseas
    placement is decided on them and the profile is what a recruiter reads
    first. The scan and everything else the MRZ carried go to the identity
    collection, because that is where a government identifier belongs. Filing
    the record must not have quietly taken over the profile field, and the
    profile field must not have become the only place the passport lands.
    """
    first = post(api, submission())
    candidate_id = first.json()["candidate_id"]

    later = post(
        api,
        submission(
            identity={"passport": [passport_document()]},
            passport_number="Z1234567",
            passport_expiry="2031-03-14",
        ),
    )

    assert later.status_code == 200
    assert later.json()["candidate_id"] == candidate_id
    assert len(api.fake_repo.candidates) == 1

    profile = api.fake_repo.candidates[candidate_id].profile
    assert profile.passport_number == "Z1234567"
    assert profile.passport_expiry == "2031-03-14"

    stored = next(iter(api.passport.docs.values()))
    assert stored["candidate_id"] == candidate_id
    assert stored["passport_number"] == "Z1234567"


def test_resending_the_same_document_overwrites_its_own_row(api):
    """A partial sync runs on every answered question, so the same passport is
    offered over and over. Twenty submissions, one row."""
    post(api, submission(identity={"passport": [passport_document()]}))
    post(api, submission(identity={"passport": [passport_document()]}))
    post(api, submission(identity={"passport": [passport_document()]}))

    assert len(api.passport.docs) == 1


def test_a_later_extraction_updates_the_row_it_already_wrote(api):
    """OCR that improves on a second read must correct the record rather than
    add a second one beside it."""
    post(api, submission(identity={"passport": [passport_document()]}))

    better = passport_document()
    better["result"]["mrz"]["expiry_date"] = "2031-03-19"
    post(api, submission(identity={"passport": [better]}))

    assert len(api.passport.docs) == 1
    assert next(iter(api.passport.docs.values()))["expiry_date"] == "2031-03-19"


# --------------------------------------------------------------------------- #
#  3 — the file itself
# --------------------------------------------------------------------------- #
def test_the_scan_is_stored_and_hung_on_the_right_record(api):
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]

    response = upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file"]["filename"] == "passport.jpg"
    assert body["file"]["mime_type"] == "image/jpeg"
    assert body["file"]["size"] == len(JPEG)

    stored = next(iter(api.passport.docs.values()))
    # A key into *this* system's storage, holding the bytes that were sent.
    assert api.storage.objects[stored["file"]["storage_key"]] == JPEG
    assert stored["file"]["sha256"] == body["file"]["sha256"]


def test_the_uploaded_scan_is_what_the_download_endpoint_serves(api):
    """The whole point of the round trip: a recruiter clicking Download gets
    the photograph the candidate sent."""
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]
    upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")

    allocate(api, candidate_id)
    api.sign_in_as("staff", "staff-1")
    served = api.get(
        f"/candidates/{candidate_id}/identity/passport/66b1f0c2e4b0a1d2c3e4f501/file"
    )

    assert served.status_code == 200, served.text
    assert served.content == JPEG
    assert served.headers["content-type"].startswith("image/jpeg")
    assert "passport.jpg" in served.headers["content-disposition"]


def test_the_profile_screen_is_told_the_scan_is_there(api):
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]

    allocate(api, candidate_id)
    api.sign_in_as("staff", "staff-1")
    before = api.get(f"/candidates/{candidate_id}/identity").json()["passport"][0]
    assert before["file_available"] is False, "no scan yet, so no button"

    upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")
    after = api.get(f"/candidates/{candidate_id}/identity").json()["passport"][0]

    assert after["file_available"] is True
    assert after["file"] == {
        "filename": "passport.jpg",
        "mime_type": "image/jpeg",
        "size": len(JPEG),
        "sha256": after["file"]["sha256"],
    }
    assert "storage_key" not in after["file"]


def test_the_same_bytes_are_not_stored_twice(api):
    """A re-upload of a file already on the record writes nothing.

    Not an optimisation: a partial sync runs on every answered question, and a
    new object per question for one passport is what this prevents.
    """
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]

    upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")
    upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")
    upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")

    assert len(api.storage.writes) == 1


def test_a_replacement_scan_is_stored(api):
    """The other half of the previous test: a candidate who re-sends a clearer
    photograph must get the clearer photograph."""
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]

    upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")
    upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501", data=OTHER_JPEG)

    assert len(api.storage.writes) == 2
    allocate(api, candidate_id)
    api.sign_in_as("staff", "staff-1")
    served = api.get(
        f"/candidates/{candidate_id}/identity/passport/66b1f0c2e4b0a1d2c3e4f501/file"
    )
    assert served.content == OTHER_JPEG


def test_a_scan_that_is_already_the_candidates_resume_is_not_copied(api):
    """One PDF that is both the CV and the passport page is one document in
    this system. The identity record points at the copy that is already there
    rather than storing a second."""
    from app.db.dedup import sha256_hex

    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]
    record = api.fake_repo.candidates[candidate_id]
    record.resume = StoredResume(
        original_filename="application.pdf",
        mime_type="application/pdf",
        size=len(JPEG),
        sha256=sha256_hex(JPEG),
        storage_backend="fake",
        storage_key="2026/08/already-here.pdf",
    )
    api.storage.objects["2026/08/already-here.pdf"] = JPEG

    response = upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")

    assert response.status_code == 200
    assert response.json()["file"]["shared_with_resume"] is True
    assert api.storage.writes == [], "the same file was stored a second time"

    # And it still downloads — `load` reads whatever key the record names.
    allocate(api, candidate_id)
    api.sign_in_as("staff", "staff-1")
    served = api.get(
        f"/candidates/{candidate_id}/identity/passport/66b1f0c2e4b0a1d2c3e4f501/file"
    )
    assert served.content == JPEG


def test_a_scan_for_a_record_that_has_not_arrived_yet_is_a_404(api):
    """This attaches a file to a document; it does not create one. The bot's
    next sync sends the submission and the file, in that order."""
    created = post(api, submission())
    candidate_id = created.json()["candidate_id"]

    response = upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")

    assert response.status_code == 404
    assert response.json()["code"] == "identity_record_not_found"


def test_a_scan_cannot_be_hung_on_another_candidates_record(api):
    """Holding a record id is not authorisation. The pair is."""
    first = post(api, submission(identity={"passport": [passport_document()]}))
    other = post(
        api,
        submission(
            key="whatsapp/111/919000000002",
            phone="+919000000002",
            phone_e164="+919000000002",
        ),
    )

    response = upload(
        api, other.json()["candidate_id"], "passport", "66b1f0c2e4b0a1d2c3e4f501"
    )

    assert response.status_code == 404
    assert first.json()["candidate_id"] != other.json()["candidate_id"]


def test_a_document_type_the_system_does_not_file_is_a_404(api):
    created = post(api, submission())
    assert upload(api, created.json()["candidate_id"], "pan_card", "x").status_code == 404


def test_a_file_that_is_not_a_scan_is_refused(api):
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]

    response = upload(
        api,
        candidate_id,
        "passport",
        "66b1f0c2e4b0a1d2c3e4f501",
        data=b"MZ\x90\x00an-executable",
        name="passport.exe",
        mime="application/x-msdownload",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_identity_document_type"


def test_an_empty_file_is_refused(api):
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]

    response = upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501", data=b"")

    assert response.status_code == 422
    assert response.json()["code"] == "empty_identity_document"


def test_the_upload_endpoint_needs_the_service_key(api):
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]

    response = api.post(
        f"/candidates/{candidate_id}/identity/passport/66b1f0c2e4b0a1d2c3e4f501/file",
        files={"file": ("passport.jpg", JPEG, "image/jpeg")},
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
#  7 & 8 — who may download what does not change
# --------------------------------------------------------------------------- #
def test_a_recruiter_may_download_a_whatsapp_passport(api):
    created = post(api, submission(identity={"passport": [passport_document()]}))
    candidate_id = created.json()["candidate_id"]
    upload(api, candidate_id, "passport", "66b1f0c2e4b0a1d2c3e4f501")

    allocate(api, candidate_id)
    api.sign_in_as("staff", "staff-1")
    response = api.get(
        f"/candidates/{candidate_id}/identity/passport/66b1f0c2e4b0a1d2c3e4f501/file"
    )
    assert response.status_code == 200


def test_an_aadhaar_scan_stays_admin_only_however_it_arrived(api):
    """The card is the number, and the number is masked for anyone who is not
    an administrator. A second door into these collections must not be a way
    round that."""
    created = post(api, submission(identity={"aadhaar": [aadhaar_document()]}))
    candidate_id = created.json()["candidate_id"]
    upload(
        api, candidate_id, "aadhaar", "66b1f0c2e4b0a1d2c3e4f502",
        name="aadhaar-front.jpg",
    )
    path = f"/candidates/{candidate_id}/identity/aadhaar/66b1f0c2e4b0a1d2c3e4f502/file"

    allocate(api, candidate_id)
    api.sign_in_as("staff", "staff-1")
    refused = api.get(path)
    assert refused.status_code == 403
    assert api.get(f"/candidates/{candidate_id}/identity").json()["aadhaar"][0][
        "file_available"
    ] is False

    api.sign_in_as("admin", "admin-1")
    allowed = api.get(path)
    assert allowed.status_code == 200
    assert allowed.content == JPEG


# --------------------------------------------------------------------------- #
#  9 — the email path is untouched
# --------------------------------------------------------------------------- #
def test_an_email_candidate_cannot_be_given_a_scan_through_this_door(api):
    """Their identity documents are pages of the bundle they were ingested
    from, cut out on demand. Writing a second copy over the top would leave the
    record disagreeing with the file it names."""
    record = CandidateRecord(
        id="email-1",
        source="email",
        profile=CandidateProfile(is_resume=True, confidence=0.9, full_name="Emailed Person"),
        # The model requires one, and that is the fact this test rests on: an
        # email candidate *is* the file they were ingested from.
        resume=StoredResume(
            original_filename="application.pdf",
            mime_type="application/pdf",
            size=len(JPEG),
            sha256="digest-of-the-bundle",
            storage_backend="fake",
            storage_key="2026/08/email-1_application.pdf",
        ),
        source_email=SourceEmail(
            message_id="msg-1", thread_id="thr-1", from_addr="nasim@example.com"
        ),
        status="ingested",
    )
    api.fake_repo.candidates["email-1"] = record
    api.passport.docs["rec-email"] = {
        "_id": "rec-email",
        "document_type": "passport",
        "candidate_id": "email-1",
        "source": {"filename": "application.pdf", "pages": [55]},
    }

    response = upload(api, "email-1", "passport", "rec-email")
    assert response.status_code == 409


def test_the_email_pipeline_still_writes_records_with_no_file_block(api):
    """`file` is optional and defaults to absent, so `multipass` calls the two
    writers exactly as it always has."""
    from app.db.identity_records import store_passport_record

    store_passport_record(
        "rec-email",
        {"mrz": {"passport_number": "Z7654321"}},
        candidate_id="email-1",
        provider="email",
        message_id="msg-1",
        attachment_id="att-1",
        filename="application.pdf",
        pages=[55],
        collection=api.passport,
    )

    stored = api.passport.docs["rec-email"]
    assert stored["passport_number"] == "Z7654321"
    assert stored["source"]["pages"] == [55]
    assert "file" not in stored


# --------------------------------------------------------------------------- #
#  The contract itself
# --------------------------------------------------------------------------- #
def test_a_submission_with_no_identity_section_still_works(api):
    """Every bot older than this change, and every candidate who has sent no
    documents."""
    response = post(api, submission())

    assert response.status_code == 201
    assert response.json()["identity_documents"] == []


def test_fields_the_crm_does_not_name_are_dropped_rather_than_stored(api):
    """`extra="ignore"` on the document model, doing the same work it does on
    the profile: a field the bot adds without a matching CRM change does not
    silently become a column."""
    document = passport_document()
    document["something_the_crm_never_agreed_to"] = "value"

    response = post(api, submission(identity={"passport": [document]}))

    assert response.status_code == 201
    assert "something_the_crm_never_agreed_to" not in next(iter(api.passport.docs.values()))


def test_a_document_with_no_record_id_is_skipped_not_guessed_at(api):
    """`record_id` is the natural key, and a document without one has no row it
    could overwrite next time — so it is not written. It is also not a reason
    to refuse the registration it arrived on."""
    response = post(api, submission(identity={"passport": [passport_document(record_id="")]}))

    assert response.status_code == 201, response.text
    assert response.json()["identity_documents"] == [
        {"document_type": "passport", "record_id": "", "stored": False, "skipped": "no record id"}
    ]
    assert api.passport.docs == {}


def test_a_payload_the_projection_cannot_read_costs_one_document(api):
    """An extractor returning something unexpected must not 422 the submission.

    The profile is the part a recruiter can act on, and refusing it because an
    OCR service changed its output shape would take a real candidate off the
    desk over a field nobody reads.
    """
    response = post(
        api,
        submission(
            identity={
                "passport": [passport_document(result="the service returned a sentence")],
                "aadhaar": [aadhaar_document()],
            }
        ),
    )

    assert response.status_code == 201, response.text
    entries = {e["document_type"]: e for e in response.json()["identity_documents"]}
    assert entries["passport"]["stored"] is False
    assert entries["aadhaar"]["stored"] is True
    assert api.fake_repo.candidates, "the candidate was refused over a bad document"


def test_a_document_with_no_extraction_yet_is_still_a_row(api):
    """A file the OCR has not reached is described with an empty payload rather
    than held back — the bot only sends one once something was read, but the
    contract must not fall over if that changes."""
    response = post(
        api, submission(identity={"passport": [passport_document(result=None)]})
    )

    assert response.status_code == 201, response.text
    assert response.json()["identity_documents"][0]["stored"] is True
    stored = next(iter(api.passport.docs.values()))
    assert stored["passport_number"] is None
    assert stored["source"]["attachment_id"] == "66b1f0c2e4b0a1d2c3e4f501"
