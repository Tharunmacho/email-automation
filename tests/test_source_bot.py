"""Receiving WhatsApp line attribution stays distinct from candidate contact data."""
import mongomock

from app.api.routes import WhatsAppCandidateIn
from app.db.repository import CandidateRepository, LIST_PROJECTION


def test_candidate_payload_accepts_the_bot_display_number_alias():
    payload = WhatsAppCandidateIn.model_validate({
        "source": "whatsapp",
        "bot_number": "+91 90000 00000",
        "idempotency_key": "whatsapp/123456/user-1",
        "profile": {"full_name": "Candidate"},
    })
    assert payload.source_bot_number == "+91 90000 00000"


def test_source_bot_is_persisted_and_present_in_list_projection():
    collection = mongomock.MongoClient()["source-bot"]["candidates"]
    collection.insert_one({"_id": "candidate-1"})
    CandidateRepository(collection=collection).set_source_bot(
        "candidate-1", number="+91 90000 00000", phone_number_id="123456"
    )
    stored = collection.find_one({"_id": "candidate-1"})
    assert stored["source_bot_number"] == "+91 90000 00000"
    assert stored["source_bot_id"] == "123456"
    assert LIST_PROJECTION["source_bot_number"] == 1
    assert LIST_PROJECTION["source_bot_id"] == 1
