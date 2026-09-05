import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.routes import app
from app.db import bot_suppression_numbers as suppression

SERVICE_KEY = "bot-suppression-test-key"


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient(tz_aware=True)["resume_ats_test"]
    monkeypatch.setattr(suppression, "get_db", lambda: database)
    suppression.ensure_bot_suppression_indexes()
    return database


def test_add_list_and_delete_suppressed_number(db):
    item = suppression.add_number(
        "+91 90000 00000", "Former staff", "admin@example.com"
    )

    assert suppression.list_numbers()[0]["phone"] == "+91 90000 00000"
    assert suppression.list_numbers()[0]["label"] == "Former staff"
    assert db[suppression.COLLECTION].find_one({"phone_key": "9000000000"})
    assert suppression.delete_number(item["id"]) is True
    assert suppression.list_numbers() == []


def test_same_number_in_another_format_is_rejected(db):
    suppression.add_number("+91 90000 00000")

    with pytest.raises(ValueError, match="already suppressed"):
        suppression.add_number("90000-00000")


def test_bot_directory_is_normalized_and_requires_service_key(db, monkeypatch):
    suppression.add_number("+91 90000 00000")
    suppression.add_number("+60 12-345 6789")
    monkeypatch.setattr("app.config.settings.whatsapp_service_key", SERVICE_KEY)
    client = TestClient(app)

    unauthorized = client.get("/bot-suppression-directory")
    assert unauthorized.status_code == 401

    response = client.get(
        "/bot-suppression-directory",
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert response.status_code == 200
    assert response.json() == {
        "numbers": ["0123456789", "9000000000"],
        "count": 2,
    }
