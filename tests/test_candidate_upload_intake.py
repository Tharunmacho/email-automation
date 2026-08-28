from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.models import CandidateProfile, ExtractedDocument, StoredResume, WorkExperience
from app.services.candidate_upload_intake import (
    CandidateUploadError,
    UploadedDocument,
    intake_uploaded_candidate,
)


class MemoryRepository:
    def __init__(self):
        self.records = {}

    def find_by_resume_hash(self, _digest):
        return None

    def find_by_email_or_phone(self, _email, _phone):
        return None

    def insert(self, record):
        self.records[record.id] = record
        return record.id

    def get(self, candidate_id):
        return self.records.get(candidate_id)

    def delete(self, candidate_id):
        return self.records.pop(candidate_id, None) is not None


class ParsedResume:
    def __init__(self, source="veris_ocr_api"):
        self.source = source

    def parse_file(self, _data, _filename):
        profile = CandidateProfile(
            is_resume=True,
            confidence=0.93,
            full_name="  Meera Nair  ",
            email=" meera@example.com ",
            phone=" +91 98765 43210 ",
            skills=["Wiring"],
            work_experience=[
                WorkExperience(company="Acme", designation="Electrician", provider_noise="drop me")
            ],
            additional_info={"extraction_source": self.source, "unwanted": "drop me"},
            raw_ocr={"pages": [{"text": "sensitive provider text"}]},
            provider_only_field="drop me",
        )
        return profile, ExtractedDocument(
            text="resume text",
            method="veris_resume_api",
            ocr_used=True,
            char_count=11,
        )


def upload(name, data=b"bytes", mime="application/pdf"):
    return UploadedDocument(data=data, filename=name, mime_type=mime)


def stored_resume(**_kwargs):
    return StoredResume(
        original_filename="meera.pdf",
        mime_type="application/pdf",
        size=12,
        sha256="resume-hash",
        storage_backend="local",
        storage_key="2026/08/meera.pdf",
    )


def test_upload_intake_routes_each_identity_file_to_veris_and_keeps_only_declared_profile_fields():
    repository = MemoryRepository()
    aadhaar_result = {
        "aadhaar": {
            "name": "Meera Nair",
            "aadhaar_number": "123412349017",
            "masked_aadhaar_number": "XXXXXXXX9017",
            "aadhaar_number_valid": True,
            "address": "Kochi",
        },
        "provider_debug": "not public",
    }
    passport_result = {
        "mrz": {
            "passport_number": "Z1234567",
            "surname": "NAIR",
            "given_names": "MEERA",
            "expiry_date": "2034-05-01",
            "all_check_digits_valid": True,
        },
        "confidence": 0.99,
        "raw_mrz": "not public",
    }

    def run_job(_data, _filename, mode, _key, **_kwargs):
        result = aadhaar_result if mode == "aadhaar" else passport_result
        return SimpleNamespace(job_id=f"job-{mode}"), SimpleNamespace(
            succeeded=True,
            timed_out=False,
            job_id=f"job-{mode}",
            result=result,
        )

    with patch("app.services.candidate_upload_intake.settings.veris_ocr_api_key", "test-key"), \
         patch("app.services.candidate_upload_intake.ocr_gateway.run_job", side_effect=run_job) as gateway, \
         patch("app.services.candidate_upload_intake.store_resume", side_effect=stored_resume), \
         patch("app.services.candidate_upload_intake.identity_files.store", return_value={"storage_key": "id/file"}), \
         patch("app.services.candidate_upload_intake.identity_records.store_aadhaar_record") as store_aadhaar, \
         patch("app.services.candidate_upload_intake.identity_records.store_passport_record") as store_passport:
        result = intake_uploaded_candidate(
            resume=upload("meera.pdf", b"resume"),
            aadhaar=upload("aadhaar.jpg", b"aadhaar", "image/jpeg"),
            passport=upload("passport.jpg", b"passport", "image/jpeg"),
            repository=repository,
            admin_id="admin-1",
            parser=ParsedResume(),
        )

    assert [call.args[2] for call in gateway.call_args_list] == ["aadhaar", "passport"]
    assert result.candidate.profile.full_name == "Meera Nair"
    assert result.candidate.profile.raw_ocr is None
    assert result.candidate.profile.additional_info == {}
    assert "provider_only_field" not in (result.candidate.profile.model_extra or {})
    assert "provider_noise" not in (result.candidate.profile.work_experience[0].model_extra or {})
    assert result.candidate.profile.passport_number == "Z1234567"
    assert result.candidate.passport_key == "Z1234567"
    assert result.candidate.resume.ocr_used is True
    assert result.candidate.cv_required is True

    assert set(result.identity) == {"aadhaar", "passport"}
    assert "provider_debug" not in result.identity["aadhaar"][0]
    assert "raw_mrz" not in result.identity["passport"][0]
    assert result.identity["passport"][0]["check_digits_valid"] is True
    assert store_aadhaar.call_args.kwargs["provider"] == "manual_upload"
    assert store_passport.call_args.kwargs["account_id"] == "admin-1"


def test_upload_intake_refuses_resume_parser_fallback_instead_of_saving_lower_quality_data():
    repository = MemoryRepository()
    with patch("app.services.candidate_upload_intake.settings.veris_ocr_api_key", "test-key"):
        with pytest.raises(CandidateUploadError) as raised:
            intake_uploaded_candidate(
                resume=upload("meera.pdf", b"resume"),
                repository=repository,
                admin_id="admin-1",
                parser=ParsedResume(source="heuristic_fallback"),
            )

    assert raised.value.code == "resume_veris_failed"
    assert raised.value.status_code == 502
    assert repository.records == {}


def test_upload_intake_rejects_failed_passport_checksum_before_creating_candidate():
    repository = MemoryRepository()
    outcome = SimpleNamespace(
        succeeded=True,
        timed_out=False,
        job_id="job-passport",
        result={
            "mrz": {
                "passport_number": "Z1234567",
                "all_check_digits_valid": False,
            }
        },
    )
    with patch("app.services.candidate_upload_intake.settings.veris_ocr_api_key", "test-key"), \
         patch("app.services.candidate_upload_intake.ocr_gateway.run_job", return_value=(None, outcome)):
        with pytest.raises(CandidateUploadError) as raised:
            intake_uploaded_candidate(
                resume=upload("meera.pdf", b"resume"),
                passport=upload("passport.jpg", b"passport", "image/jpeg"),
                repository=repository,
                admin_id="admin-1",
                parser=ParsedResume(),
            )

    assert raised.value.code == "invalid_passport_mrz"
    assert repository.records == {}


def test_upload_intake_rolls_back_candidate_and_files_when_identity_filing_fails():
    repository = MemoryRepository()
    outcome = SimpleNamespace(
        succeeded=True,
        timed_out=False,
        job_id="job-passport",
        result={
            "mrz": {
                "passport_number": "Z1234567",
                "all_check_digits_valid": True,
            }
        },
    )
    backend = MagicMock()
    with patch("app.services.candidate_upload_intake.settings.veris_ocr_api_key", "test-key"), \
         patch("app.services.candidate_upload_intake.ocr_gateway.run_job", return_value=(None, outcome)), \
         patch("app.services.candidate_upload_intake.store_resume", side_effect=stored_resume), \
         patch(
             "app.services.candidate_upload_intake.identity_files.store",
             return_value={"storage_backend": "local", "storage_key": "identity/passport.jpg"},
         ), \
         patch(
             "app.services.candidate_upload_intake.identity_records.store_passport_record",
             side_effect=RuntimeError("identity database unavailable"),
         ), \
         patch("app.services.candidate_upload_intake.identity_records.delete_for_candidate") as delete_identity, \
         patch("app.services.candidate_upload_intake.get_storage_backend", return_value=backend):
        with pytest.raises(CandidateUploadError) as raised:
            intake_uploaded_candidate(
                resume=upload("meera.pdf", b"resume"),
                passport=upload("passport.jpg", b"passport", "image/jpeg"),
                repository=repository,
                admin_id="admin-1",
                parser=ParsedResume(),
            )

    assert raised.value.code == "document_storage_failed"
    assert repository.records == {}
    delete_identity.assert_called_once()
    assert {call.args[0] for call in backend.delete.call_args_list} == {
        "identity/passport.jpg",
        "2026/08/meera.pdf",
    }
