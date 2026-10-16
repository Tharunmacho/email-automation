"""Bot-written contacts must remain readable during candidate deduplication."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.core.models import CandidateContact, CandidateRecord


def test_legacy_contacts_read_as_canonical_without_mutating_input():
    first_seen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    last_seen = datetime(2026, 1, 2, tzinfo=timezone.utc)
    legacy = {
        "number": "+91 98765 43210",
        "normalized": "919876543210",
        "e164": "+919876543210",
        "sources": ["whatsapp", "resume"],
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
    }
    original = deepcopy(legacy)

    contact = CandidateContact.model_validate(legacy)

    assert contact.model_dump() == {
        "value": "+91 98765 43210",
        "key": "9876543210",
        "e164": "+919876543210",
        "sources": ["whatsapp", "resume"],
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
    }
    assert legacy == original


@pytest.mark.parametrize("normalized", [None, "", "not a phone", "919999999999"])
def test_legacy_key_comes_from_number_even_if_normalized_is_stale(normalized):
    contact = CandidateContact.model_validate({
        "number": "9876543210", "normalized": normalized,
    })

    assert contact.key == "9876543210"
    assert contact.e164 is None


def test_number_only_contact_gets_derived_key():
    contact = CandidateContact.model_validate({"number": "+971 50 123 4567"})

    assert contact.value == "+971 50 123 4567"
    assert contact.key == "1501234567"


def test_canonical_fields_take_precedence_over_legacy_fields():
    contact = CandidateContact.model_validate({
        "value": "+91 98765 43210", "key": "stored-canonical-key",
        "number": "9999999999", "normalized": "919999999999",
    })

    assert contact.value == "+91 98765 43210"
    assert contact.key == "stored-canonical-key"


def test_canonical_contact_keeps_existing_serialization():
    canonical = {
        "value": "+91 98765 43210", "key": "9876543210",
        "e164": "+919876543210", "sources": ["manual"],
        "first_seen_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "last_seen_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }

    assert CandidateContact.model_validate(canonical).model_dump() == canonical


def test_partial_legacy_contact_derives_key_from_canonical_value():
    contact = CandidateContact.model_validate({
        "value": "9876543210", "number": "9999999999", "normalized": "919999999999",
    })

    assert contact.key == "9876543210"


@pytest.mark.parametrize("data", [
    {},
    {"number": None, "normalized": "919876543210"},
    {"number": 919876543210, "normalized": "919876543210"},
    {"number": "not a phone", "normalized": "919876543210"},
    {"number": "123456", "key": "123456"},
    {"normalized": "919876543210"},
    {"value": "9876543210"},
    {"value": None, "key": "9876543210", "number": "9876543210"},
    {"value": "9876543210", "key": None, "number": "9876543210"},
])
def test_malformed_or_explicitly_invalid_contacts_still_fail_validation(data):
    with pytest.raises(ValidationError):
        CandidateContact.model_validate(data)


def test_candidate_from_mongo_reads_mixed_contacts_and_roundtrips_canonical_shape():
    doc = {
        "_id": "legacy-whatsapp-candidate",
        "source": "whatsapp",
        "cv_required": False,
        "profile": {"full_name": "Legacy Candidate", "phone": "9876543210"},
        "phone_key": "9876543210",
        "contacts": [
            {"number": "919876543210", "normalized": "919876543210", "sources": ["whatsapp"]},
            {"value": "9999999999", "key": "9999999999", "sources": ["resume"]},
        ],
    }
    original = deepcopy(doc)

    candidate = CandidateRecord.from_mongo(doc)

    assert candidate.contacts[0].key == candidate.phone_key
    assert candidate.contacts[0].value == "919876543210"
    assert candidate.contacts[0].sources == ["whatsapp"]
    assert candidate.contacts[1].value == "9999999999"
    assert doc == original
    stored = candidate.to_mongo()
    assert stored["contacts"][0] == {
        "value": "919876543210", "key": "9876543210", "sources": ["whatsapp"],
    }
    assert CandidateRecord.from_mongo(stored).contacts == candidate.contacts


def test_candidate_from_mongo_does_not_discard_invalid_legacy_contacts():
    doc = {
        "_id": "malformed-candidate", "source": "whatsapp", "cv_required": False,
        "profile": {}, "contacts": [{"number": "not a phone", "normalized": "919876543210"}],
    }

    with pytest.raises(ValidationError) as error:
        CandidateRecord.from_mongo(doc)

    assert ("contacts", 0, "value") in {entry["loc"] for entry in error.value.errors()}
