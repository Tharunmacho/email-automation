"""Downloading the Aadhaar or passport itself, not just what was read off it.

The identity rows have always said *what* an extractor read — a number, an
MRZ, a checksum verdict. A documentation officer chasing a misread digit needs
the page, and until now the only way to it was to download the whole 59-page
application bundle and go looking for page 54.

Two things are being claimed here, and the second is the one worth having
tests for:

* The page comes back, correctly cut, whichever way the document arrived.
* Cutting it out of the bundle does not open a door round the rules that
  already govern these documents — whose candidate it is, and who is allowed to
  see an Aadhaar number.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.core.models import CandidateProfile, CandidateRecord, SourceEmail, StoredResume

BUNDLE_SHA = "sha-of-the-bundle"


def a_bundle(pages: int = 60) -> bytes:
    """A PDF whose every page says which page it is, so a subset is checkable."""
    import fitz

    doc = fitz.open()
    try:
        for number in range(1, pages + 1):
            doc.new_page().insert_text((72, 100), f"PAGE {number}")
        return doc.tobytes()
    finally:
        doc.close()


def text_of(pdf: bytes) -> list[str]:
    import fitz

    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return [page.get_text().strip() for page in doc]


def make_record(
    candidate_id: str, staff_id: str | None, *, sha: str = BUNDLE_SHA
) -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id,
        profile=CandidateProfile(is_resume=True, confidence=0.9, full_name="Nasim Shah"),
        resume=StoredResume(
            original_filename="application.pdf",
            mime_type="application/pdf",
            size=4096,
            sha256=sha,
            storage_backend="local",
            storage_key=f"2026/08/{candidate_id}_application.pdf",
        ),
        source_email=SourceEmail(message_id="m1", thread_id="t1", from_addr="a@x.com"),
        status="ingested",
        assigned_staff_id=staff_id,
    )


class Repo:
    def __init__(self, records):
        self.records = {r.id: r for r in records}

    def get(self, candidate_id):
        return self.records.get(candidate_id)


class Storage:
    """Whatever was put in it, by key. Stands in for GridFS and local disk."""

    name = "local"

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.reads: list[str] = []

    def load(self, key: str) -> bytes:
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]


def aadhaar_row(**overrides) -> dict:
    row = {
        "_id": "rec-aadhaar",
        "document_type": "aadhaar",
        "candidate_id": "cand-mine",
        "aadhaar_number": "123412349017",
        "masked_aadhaar_number": "XXXXXXXX9017",
        "raw": {"aadhaar": {"aadhaar_number": "123412349017"}},
        "updated_at": datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc),
        "source": {"filename": "application.pdf", "pages": [54], "sha256": BUNDLE_SHA},
    }
    row.update(overrides)
    return row


def passport_row(**overrides) -> dict:
    row = {
        "_id": "rec-passport",
        "document_type": "passport",
        "candidate_id": "cand-mine",
        "passport_number": "Z1234567",
        "raw": {"mrz": {"passport_number": "Z1234567"}},
        "updated_at": datetime(2026, 8, 13, 9, 31, tzinfo=timezone.utc),
        "source": {"filename": "application.pdf", "pages": [55], "sha256": BUNDLE_SHA},
    }
    row.update(overrides)
    return row


@pytest.fixture
def api():
    """A signed-in client over one candidate whose bundle is really in storage."""
    repo = Repo([make_record("cand-mine", "staff-1"), make_record("cand-theirs", "staff-2")])
    storage = Storage(
        {
            "2026/08/cand-mine_application.pdf": a_bundle(),
            "2026/08/cand-theirs_application.pdf": a_bundle(),
        }
    )
    rows = {"aadhaar": [aadhaar_row()], "passport": [passport_row()]}

    def find_one(candidate_id, document_type, record_id):
        for row in rows.get(document_type, []):
            if row["_id"] == record_id and row["candidate_id"] == candidate_id:
                return row
        return None

    def sign_in_as(role: str, user_id: str = "staff-1"):
        app.dependency_overrides[current_user] = lambda: {
            "id": user_id,
            "email": f"{user_id}@x.com",
            "name": user_id,
            "role": role,
            "pages": ["candidates", "settings"],
        }

    with patch("app.api.routes.repo", return_value=repo), patch(
        "app.db.identity_records.find_one", find_one
    ), patch("app.db.identity_records.find_for_candidate", lambda cid: rows), patch(
        "app.services.identity_files.get_storage_backend", return_value=storage
    ):
        client = TestClient(app)
        client.sign_in_as = sign_in_as  # type: ignore[attr-defined]
        client.rows = rows  # type: ignore[attr-defined]
        client.storage = storage  # type: ignore[attr-defined]
        try:
            yield client
        finally:
            app.dependency_overrides.pop(current_user, None)


def url(document_type: str, record_id: str, candidate_id: str = "cand-mine") -> str:
    return f"/candidates/{candidate_id}/identity/{document_type}/{record_id}/file"


# --------------------------------------------------------------------------- #
#  The page comes back
# --------------------------------------------------------------------------- #
def test_the_passport_page_is_cut_out_of_the_bundle(api):
    """One page, and the right one.

    Nothing stored the passport separately — the pipeline stores the
    attachment and records which page held the passport. This is that record
    being honoured: page 55 of the bundle, on demand, as its own PDF.
    """
    api.sign_in_as("staff", "staff-1")
    response = api.get(url("passport", "rec-passport"))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert text_of(response.content) == ["PAGE 55"]


def test_the_download_is_named_after_the_document_and_its_pages(api):
    """A recruiter with four of these in a downloads folder has to be able to
    tell them apart without opening them."""
    api.sign_in_as("staff", "staff-1")
    disposition = api.get(url("passport", "rec-passport")).headers["content-disposition"]

    assert "application_passport_p55.pdf" in disposition
    # Both header forms, so a browser that ignores either still gets a name.
    assert "filename*=UTF-8" + chr(39) * 2 + "application_passport_p55.pdf" in disposition


def test_a_document_spanning_two_pages_comes_back_as_both(api):
    api.sign_in_as("staff", "staff-1")
    api.rows["passport"][0]["source"]["pages"] = [55, 56]

    response = api.get(url("passport", "rec-passport"))
    assert text_of(response.content) == ["PAGE 55", "PAGE 56"]


def test_a_file_stored_against_the_record_is_served_instead_of_the_bundle(api):
    """The shape a document that arrived on its own takes — one scan, one
    upload, nothing to cut. The bundle is not touched."""
    api.sign_in_as("staff", "staff-1")
    api.storage.files["identity/passport.jpg"] = b"jpeg-bytes"
    api.rows["passport"][0]["file"] = {
        "storage_backend": "local",
        "storage_key": "identity/passport.jpg",
        "filename": "passport-front.jpg",
        "mime_type": "image/jpeg",
    }

    response = api.get(url("passport", "rec-passport"))

    assert response.status_code == 200
    assert response.content == b"jpeg-bytes"
    assert response.headers["content-type"].startswith("image/jpeg")
    assert "passport-front.jpg" in response.headers["content-disposition"]
    assert api.storage.reads == ["identity/passport.jpg"]


def test_a_scan_that_is_the_whole_file_is_served_whole(api):
    """A phone photograph of a passport is one image and all of it is the
    document. `subset_pdf` says so by returning None, and the answer is the
    original bytes rather than a failure."""
    api.sign_in_as("staff", "staff-1")
    api.storage.files["2026/08/cand-mine_application.pdf"] = b"not-a-pdf"
    api.rows["passport"][0]["source"]["pages"] = []

    response = api.get(url("passport", "rec-passport"))

    assert response.status_code == 200
    assert response.content == b"not-a-pdf"


# --------------------------------------------------------------------------- #
#  It does not go round the rules these documents already have
# --------------------------------------------------------------------------- #
def test_an_aadhaar_scan_is_refused_to_a_recruiter(api):
    """The card *is* the number, and the number is masked for anyone who is not
    an administrator. Serving the scan would hand back exactly what the masking
    withholds — a hole in the rule, not a feature."""
    api.sign_in_as("staff", "staff-1")
    response = api.get(url("aadhaar", "rec-aadhaar"))

    assert response.status_code == 403
    assert "administrator" in response.json()["detail"]


def test_an_administrator_is_served_the_aadhaar_scan(api):
    api.sign_in_as("admin", "admin-1")
    response = api.get(url("aadhaar", "rec-aadhaar"))

    assert response.status_code == 200
    assert text_of(response.content) == ["PAGE 54"]


def test_another_staff_members_candidate_is_a_404(api):
    """404 rather than 403, exactly as every other candidate route: a 403 would
    confirm the record exists, which is the fact the isolation rule withholds."""
    api.sign_in_as("staff", "staff-1")
    assert api.get(url("passport", "rec-passport", "cand-theirs")).status_code == 404


def test_a_record_id_from_another_candidate_does_not_resolve(api):
    """The candidate id and the record id are checked together. Holding a
    record id must not be enough to read a document off someone else's file."""
    api.sign_in_as("admin", "admin-1")
    api.rows["passport"].append(passport_row(_id="rec-elsewhere", candidate_id="cand-theirs"))

    assert api.get(url("passport", "rec-elsewhere")).status_code == 404


def test_an_unknown_document_type_is_a_404(api):
    api.sign_in_as("admin", "admin-1")
    assert api.get(url("pan_card", "rec-passport")).status_code == 404


def test_a_bundle_that_is_not_the_one_the_document_was_read_from_is_refused(api):
    """Provenance, enforced rather than assumed.

    The pages are only meaningful against the file they were read off. If the
    candidate's stored file has since become a different one, page 55 is not
    the passport any more — and serving it anyway would put a stranger's page
    under this candidate's name.
    """
    api.sign_in_as("staff", "staff-1")
    api.rows["passport"][0]["source"]["sha256"] = "a-different-file"

    response = api.get(url("passport", "rec-passport"))
    assert response.status_code == 404
    assert "not the one this document was read from" in response.json()["detail"]


def test_a_bundle_missing_from_storage_says_so(api):
    api.sign_in_as("staff", "staff-1")
    del api.storage.files["2026/08/cand-mine_application.pdf"]

    response = api.get(url("passport", "rec-passport"))
    assert response.status_code == 404
    assert "not in storage" in response.json()["detail"]


# --------------------------------------------------------------------------- #
#  What the profile screen is told before it draws a button
# --------------------------------------------------------------------------- #
def test_the_listing_says_which_documents_can_be_downloaded(api):
    """A button that can only 404 tells a recruiter something untrue, so the
    server answers the question rather than the browser guessing at it."""
    api.sign_in_as("staff", "staff-1")
    body = api.get("/candidates/cand-mine/identity").json()

    assert body["passport"][0]["file_available"] is True
    # Refused to this caller, so not offered to them either.
    assert body["aadhaar"][0]["file_available"] is False


def test_an_administrator_is_offered_the_aadhaar(api):
    api.sign_in_as("admin", "admin-1")
    body = api.get("/candidates/cand-mine/identity").json()

    assert body["aadhaar"][0]["file_available"] is True


def test_a_document_with_nothing_behind_it_is_not_offered(api):
    api.sign_in_as("staff", "staff-1")
    api.rows["passport"][0]["source"]["sha256"] = "a-different-file"

    body = api.get("/candidates/cand-mine/identity").json()
    assert body["passport"][0]["file_available"] is False


def test_the_listing_never_hands_out_a_storage_key(api):
    """A key is an implementation detail and a thing to probe. The name, type
    and size are what a recruiter is shown before clicking."""
    api.sign_in_as("admin", "admin-1")
    api.rows["passport"][0]["file"] = {
        "storage_backend": "local",
        "storage_key": "identity/passport.jpg",
        "filename": "passport-front.jpg",
        "mime_type": "image/jpeg",
        "size": 11,
    }

    block = api.get("/candidates/cand-mine/identity").json()["passport"][0]["file"]

    assert block == {"filename": "passport-front.jpg", "mime_type": "image/jpeg", "size": 11}
    assert "storage_key" not in block


# --------------------------------------------------------------------------- #
#  Writing the file down in the first place
# --------------------------------------------------------------------------- #
class RecordingCollection:
    def __init__(self):
        self.updates = []

    def update_one(self, query, update, upsert=False):
        self.updates.append(update["$set"])


def test_a_redelivery_without_the_bytes_does_not_delete_the_scan():
    """Every write is a `$set` upsert and a redelivered email re-runs the
    extraction with no file to hand. A `"file": None` in that payload would
    remove a scan a recruiter can currently download, on a pass that changed
    nothing else."""
    from app.db.identity_records import store_passport_record

    coll = RecordingCollection()
    stored = {"storage_backend": "local", "storage_key": "identity/p.jpg"}
    store_passport_record("rec-1", {"mrz": {}}, file=stored, collection=coll)
    store_passport_record("rec-1", {"mrz": {}}, collection=coll)

    assert coll.updates[0]["file"] == stored
    assert "file" not in coll.updates[1]


def test_the_file_block_is_stored_as_given():
    from app.db.identity_records import store_aadhaar_record

    coll = RecordingCollection()
    stored = {
        "storage_backend": "gridfs",
        "storage_key": "2026/08/cand_aadhaar.jpg",
        "filename": "aadhaar.jpg",
        "mime_type": "image/jpeg",
        "size": 2048,
        "sha256": "abc123",
    }
    store_aadhaar_record("rec-1", {"aadhaar": {}}, file=stored, collection=coll)

    assert coll.updates[0]["file"] == stored
