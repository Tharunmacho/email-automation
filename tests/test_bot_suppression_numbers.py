import mongomock
import pytest

from app.db import bot_suppression_numbers as suppression


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
