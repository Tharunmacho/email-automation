"""An Aadhaar or passport emailed as its own attachment beside the CV is
downloadable from the profile, even though no file block was written onto the
identity record when it was filed."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.services import identity_files

RESUME = SimpleNamespace(storage_key="cv-key", storage_backend="gridfs", sha256="cv-sha",
                         original_filename="cv.pdf", mime_type="application/pdf")
CANDIDATE = SimpleNamespace(resume=RESUME)
PASSPORT = {"_id": "row-1", "document_type": "passport",
            "source": {"provider": "email", "sha256": "scan-sha", "filename": "passport.jpg", "pages": []}}


class Store:
    def __init__(self, row):
        self.row = row

    def get(self, _id):
        return self.row


def test_the_sibling_attachment_is_served_when_its_row_matches():
    row = SimpleNamespace(storage_key="scan-key", sha256="scan-sha", filename="passport.jpg")
    with patch("app.db.ingestion_state.IngestionStateStore", return_value=Store(row)), \
         patch.object(identity_files, "_load", return_value=b"jpeg") as load:
        assert identity_files.available(CANDIDATE, PASSPORT)
        scan = identity_files.load(CANDIDATE, PASSPORT)
    load.assert_called_with(identity_files.settings.storage_backend, "scan-key")
    assert scan.data == b"jpeg" and scan.mime_type == "image/jpeg"


def test_a_row_with_a_different_fingerprint_is_not_served():
    row = SimpleNamespace(storage_key="other-key", sha256="different", filename="x.jpg")
    with patch("app.db.ingestion_state.IngestionStateStore", return_value=Store(row)):
        assert not identity_files.available(CANDIDATE, PASSPORT)


def test_legacy_whatsapp_records_never_serve_the_cv_as_the_scan():
    legacy = {"_id": "whatsapp:abc", "document_type": "passport", "source": {"provider": "whatsapp"}}
    assert not identity_files.available(CANDIDATE, legacy)
