"""What a candidate list row is, and what it deliberately is not.

The list endpoint used to serve whole documents. Each one carries the OCR
provider's verbatim response twice — once as `raw_ocr`, once mirrored under
`profile.raw_ocr` — so a page of 200 moved megabytes out of Atlas and through
Pydantic every few seconds, none of which any row displayed. These tests pin the
projection: the heavy fields must be absent from a row, and the query must be
what leaves them behind, rather than Python trimming a document it already paid
to fetch.
"""
from __future__ import annotations

from app.core.crm_ids import candidate_code
from app.db.repository import CandidateRepository


# --------------------------------------------------------------------------- #
#  A collection stand-in that applies an inclusion projection like Mongo does
# --------------------------------------------------------------------------- #
def _project(doc: dict, projection: dict | None) -> dict:
    if not projection:
        return dict(doc)

    out: dict = {}
    for path in projection:
        parts = path.split(".")
        value = doc
        for part in parts:
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value is None:
            continue
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return out


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs
        self.sorted_by = None
        self.skipped = 0
        self.limited = None

    def sort(self, field, direction):
        self.sorted_by = (field, direction)
        return self

    def skip(self, n):
        self.skipped = n
        return self

    def limit(self, n):
        self.limited = n
        return self

    def __iter__(self):
        docs = self.docs[self.skipped:]
        if self.limited is not None:
            docs = docs[: self.limited]
        return iter(docs)


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.last_projection = None
        self.last_cursor = None

    def find(self, query=None, projection=None):
        self.last_projection = projection
        self.last_cursor = FakeCursor([_project(d, projection) for d in self.docs])
        return self.last_cursor


OCR_PAYLOAD = {
    "text": "x" * 50_000,
    "pages": [{"page_number": 1, "text": "x" * 50_000}],
}

DOC = {
    "_id": "candidate-alice",
    "status": "ingested",
    "resume_hash": "abc123",
    "created_at": "2026-08-01T00:00:00Z",
    "raw_ocr": OCR_PAYLOAD,
    "profile": {
        "is_resume": True,
        "confidence": 0.91,
        "full_name": "Alice Smith",
        "email": "alice@example.com",
        "phone": "+1 555-0100",
        "skills": ["Python"],
        "education": [{"institution": "State University"}],
        "raw_ocr": OCR_PAYLOAD,
    },
    "resume": {
        "original_filename": "alice.pdf",
        "mime_type": "application/pdf",
        "size": 1024,
        "sha256": "abc123",
        "storage_backend": "gridfs",
        "storage_key": "2026/08/alice.pdf",
        "extraction_method": "pdf_text",
        "ocr_used": False,
    },
    "source_email": {
        "message_id": "msg-1",
        "thread_id": "thread-1",
        "from_addr": "alice@example.com",
        "from_name": "Alice Smith",
        "to_addr": "recruitment@agency.com",
        "subject": "Resume",
    },
}


def _repo(docs=None):
    collection = FakeCollection(docs if docs is not None else [DOC])
    return CandidateRepository(collection=collection), collection


# --------------------------------------------------------------------------- #
#  The list view
# --------------------------------------------------------------------------- #
def test_a_list_row_carries_neither_copy_of_the_ocr_payload():
    """Both copies, or the projection has only halved the problem."""
    repo, _ = _repo()

    row = repo.list_summaries()[0]

    assert "raw_ocr" not in row
    assert "raw_ocr" not in row["profile"]


def test_a_list_row_still_carries_what_the_directory_shows():
    repo, _ = _repo()

    row = repo.list_summaries()[0]

    assert row["profile"]["full_name"] == "Alice Smith"
    assert row["profile"]["email"] == "alice@example.com"
    assert row["profile"]["confidence"] == 0.91
    assert row["profile"]["skills"] == ["Python"]
    assert row["resume"]["original_filename"] == "alice.pdf"
    assert row["source_email"]["from_addr"] == "alice@example.com"
    assert row["source_email"]["to_addr"] == "recruitment@agency.com"
    assert row["status"] == "ingested"


def test_a_list_row_leaves_detail_only_fields_to_the_detail_endpoint():
    """Anything a row does not display is a reason to open the candidate, not
    payload to send 200 copies of."""
    repo, _ = _repo()

    row = repo.list_summaries()[0]

    assert "education" not in row["profile"]
    assert "storage_key" not in row["resume"]


def test_a_list_row_names_its_id_the_way_a_record_does():
    """The frontend reads `id`; Mongo stores `_id`. A row must not leak `_id`,
    or every consumer needs a second spelling for the same field."""
    repo, _ = _repo()

    row = repo.list_summaries()[0]

    assert row["id"] == "candidate-alice"
    assert "_id" not in row


def test_the_database_does_the_projecting():
    """Trimming in Python would still pay to fetch every OCR payload — the whole
    cost this exists to avoid."""
    repo, collection = _repo()

    repo.list_summaries()

    assert collection.last_projection, "the query must carry a projection"
    assert "raw_ocr" not in collection.last_projection
    assert "profile.raw_ocr" not in collection.last_projection


def test_paging_and_ordering_reach_the_query():
    repo, collection = _repo()

    repo.list_summaries(limit=10, skip=20)

    assert collection.last_cursor.sorted_by == ("created_at", -1)
    assert collection.last_cursor.skipped == 20
    assert collection.last_cursor.limited == 10


# --------------------------------------------------------------------------- #
#  The minimal view
# --------------------------------------------------------------------------- #
def test_the_minimal_view_is_flat_and_exactly_the_agreed_fields():
    repo, _ = _repo()

    row = repo.list_summaries(minimal=True)[0]

    assert row == {
        "id": "candidate-alice",
        "candidate_code": candidate_code("candidate-alice"),
        "full_name": "Alice Smith",
        "email": "alice@example.com",
        "phone": "+1 555-0100",
        "status": "ingested",
        "confidence": 0.91,
        "created_at": "2026-08-01T00:00:00Z",
    }


def test_the_minimal_view_survives_a_record_with_almost_nothing_in_it():
    """An early-stage or partially-parsed record must still list, with holes
    rather than a 500."""
    repo, _ = _repo([{"_id": "sparse", "status": "needs_review"}])

    row = repo.list_summaries(minimal=True)[0]

    assert row["id"] == "sparse"
    assert row["full_name"] is None
    assert row["confidence"] is None
