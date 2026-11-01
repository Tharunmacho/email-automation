"""New job orders go to the Sourcing Hub's active agents on WhatsApp, once.

The CRM half: who is messaged, what travels to the bot, and that a job order is
announced exactly once. The bot half (template, line, phone format) is tested
in the bot repo's `src/agent-broadcast-test.ts`.
"""
from __future__ import annotations

from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

# Module scope, deliberately: see the note in `tests/test_b2b_enquiries.py`.
from app.api.routes import app as fastapi_app
from app import agent_whatsapp
from app.agent_whatsapp import (
    AGENT_JOB_ORDER_PATH,
    BROADCAST_COLLECTION,
    agent_recipients,
    broadcast_job_order,
)
from app.config import settings

ORDER = {
    "id": "ORD-123-4567",
    "title": "Pipe Fitter",
    "client": "Secret Client LLC",
    "destinationCountry": "Saudi Arabia",
    "headcount": 12,
    "salary": "SAR 1800",
    "minExperience": "3 years",
    "description": "Interviews in Chennai on 10 Nov. Bring originals.",
    "status": "OPEN",
    "website": {"show": True, "posterUrl": "/public/job-posters/0123456789abcdef0123456789abcdef.jpg"},
}

AGENTS = [
    {"id": "AGT-1", "name": "Gulf Manpower", "type": "agent", "status": "ACTIVE",
     "contact": "Imran", "phone": "98765 43210",
     "contacts": [{"name": "Imran", "phone": "98765 43210", "email": ""},
                  {"name": "Second", "phone": "91234 56789", "email": ""}]},
    # Older record: no `contacts`, only the top-level mirror.
    {"id": "AGT-2", "name": "Chennai Agency", "type": "agent", "status": "ACTIVE",
     "contact": "", "phone": "+971 50 123 4567"},
    # Same number as AGT-1, written differently: one message, not two.
    {"id": "AGT-3", "name": "Duplicate", "type": "agent", "status": "ACTIVE",
     "contact": "Imran", "phone": "+91-98765-43210"},
    {"id": "AGT-4", "name": "Pending", "type": "agent", "status": "PENDING", "phone": "9000000001"},
    {"id": "AGT-5", "name": "Inactive", "type": "agent", "status": "INACTIVE", "phone": "9000000002"},
    {"id": "AGT-6", "name": "No phone", "type": "agent", "status": "ACTIVE", "phone": ""},
    {"id": "CLI-1", "name": "A client", "type": "client", "status": "ACTIVE", "phone": "9000000003"},
    {"id": "ASC-1", "name": "An association", "type": "association", "status": "ACTIVE",
     "phone": "9000000004"},
]


@pytest.fixture
def db():
    database = mongomock.MongoClient(tz_aware=True)["agent_whatsapp_test"]
    database["sourcing_clients"].insert_many([dict(a) for a in AGENTS])
    return database


def _enabled():
    return patch.multiple(
        settings,
        agent_job_order_whatsapp=True,
        wa_bot_url="https://bot.example.com/",
        wa_bot_api_key="secret",
    )


# --------------------------------------------------------------------------- #
#  Who is messaged
# --------------------------------------------------------------------------- #
def test_only_active_agents_with_a_phone_are_picked_once_per_number(db):
    assert agent_recipients(db) == [
        {"agent_id": "AGT-1", "agent_name": "Imran", "phone": "98765 43210"},
        {"agent_id": "AGT-2", "agent_name": "Chennai Agency", "phone": "+971 50 123 4567"},
    ]


# --------------------------------------------------------------------------- #
#  Sending
# --------------------------------------------------------------------------- #
def test_switched_off_sends_nothing_and_claims_nothing(db):
    with patch.object(agent_whatsapp, "_post") as post:
        assert broadcast_job_order(ORDER, database=db)["reason"] == "disabled"
    post.assert_not_called()
    assert db[BROADCAST_COLLECTION].count_documents({}) == 0


def test_switch_on_without_a_bot_sends_nothing(db):
    with patch.multiple(settings, agent_job_order_whatsapp=True, wa_bot_url="", wa_bot_api_key=""), \
         patch.object(agent_whatsapp, "_post") as post:
        assert broadcast_job_order(ORDER, database=db)["reason"] == "disabled"
    post.assert_not_called()


def test_each_agent_gets_the_order_and_its_remarks_but_not_the_client(db):
    with _enabled(), patch.object(agent_whatsapp, "_post", return_value=True) as post:
        result = broadcast_job_order(ORDER, database=db)

    assert result == {"sent": 2, "recipients": 2, "failed_agent_ids": []}
    paths = {call.args[0] for call in post.call_args_list}
    assert paths == {AGENT_JOB_ORDER_PATH}
    payloads = [call.args[1] for call in post.call_args_list]
    assert payloads[0] == {
        "job_order_id": "ORD-123-4567",
        "title": "Pipe Fitter",
        "destination_country": "Saudi Arabia",
        "headcount": 12,
        "salary": "SAR 1800",
        "min_experience": "3 years",
        "poster_url": "/public/job-posters/0123456789abcdef0123456789abcdef.jpg",
        # No terms on this order: every key still travels, blank.
        "industry": "",
        "gender": "",
        "process_time": "",
        "b2b_rate": "",
        "staff_name": "",
        "staff_phone": "",
        "remarks": "Interviews in Chennai on 10 Nov. Bring originals.",
        "agent_id": "AGT-1",
        "agent_name": "Imran",
        "phone": "98765 43210",
    }
    for payload in payloads:
        assert "Secret Client" not in str(payload)
    assert all(call.kwargs.get("require_sent") is True for call in post.call_args_list)


def test_the_orders_agent_terms_travel_with_it(db):
    order = {
        **ORDER,
        "industry": "Automotive",
        "terms": {
            "gender": "Male & Female",
            "processTime": "6-8 months",
            "b2bRate": "4.9 with Ticket",
            # Asked for once, then dropped from the form: no longer sent.
            "advancePayment": "€946",
            "documents": ["CV"],
        },
    }
    with _enabled(), patch.object(agent_whatsapp, "_post", return_value=True) as post:
        broadcast_job_order(order, database=db)
    payload = post.call_args_list[0].args[1]
    assert {key: payload[key] for key in (
        "industry", "gender", "process_time", "b2b_rate",
    )} == {
        "industry": "Automotive",
        "gender": "Male & Female",
        "process_time": "6-8 months",
        "b2b_rate": "4.9 with Ticket",
    }
    assert not {"advance_payment", "documents"} & set(payload)


def test_the_assigned_staff_member_travels_as_the_contact(db):
    order = {**ORDER, "assignedStaff": {"id": "u-7", "name": "Rafi", "phone": "+91 98849 49735"}}
    with _enabled(), patch.object(agent_whatsapp, "_post", return_value=True) as post:
        broadcast_job_order(order, database=db)
    payload = post.call_args_list[0].args[1]
    assert (payload["staff_name"], payload["staff_phone"]) == ("Rafi", "+91 98849 49735")
    assert "u-7" not in str(payload)


def test_the_poster_on_the_order_is_the_messages_picture(db):
    order = {**ORDER, "posterUrl": "/public/job-posters/" + "f" * 32 + ".png"}
    with _enabled(), patch.object(agent_whatsapp, "_post", return_value=True) as post:
        broadcast_job_order(order, database=db)
    # The order's own poster wins over the old `website.posterUrl`.
    assert post.call_args_list[0].args[1]["poster_url"] == "/public/job-posters/" + "f" * 32 + ".png"


@pytest.mark.parametrize("terms", ["not a dict", None, {"gender": None, "processTime": "  ", "b2bRate": None}])
def test_malformed_terms_are_sent_as_blanks_not_errors(db, terms):
    order = {**ORDER, "terms": terms}
    with _enabled(), patch.object(agent_whatsapp, "_post", return_value=True) as post:
        result = broadcast_job_order(order, database=db)
    assert result["sent"] == 2
    payload = post.call_args_list[0].args[1]
    assert (payload["gender"], payload["process_time"], payload["b2b_rate"]) == ("", "", "")


def test_an_order_without_a_poster_sends_a_blank_poster_for_the_bot_to_default(db):
    order = {key: value for key, value in ORDER.items() if key != "website"}
    with _enabled(), patch.object(agent_whatsapp, "_post", return_value=True) as post:
        broadcast_job_order(order, database=db)
    assert post.call_args_list[0].args[1]["poster_url"] == ""


def test_a_job_order_is_announced_only_once(db):
    with _enabled(), patch.object(agent_whatsapp, "_post", return_value=True) as post:
        broadcast_job_order(ORDER, database=db)
        second = broadcast_job_order(ORDER, database=db)
    assert second == {"sent": 0, "reason": "already_sent"}
    assert post.call_count == 2  # both from the first run


def test_an_order_that_is_not_open_is_not_announced(db):
    with _enabled(), patch.object(agent_whatsapp, "_post") as post:
        result = broadcast_job_order({**ORDER, "status": "FILLED"}, database=db)
    assert result["reason"] == "not_open"
    post.assert_not_called()


def test_failed_sends_are_recorded_and_do_not_stop_the_rest(db):
    with _enabled(), patch.object(agent_whatsapp, "_post", side_effect=[False, True]):
        result = broadcast_job_order(ORDER, database=db)
    assert result["sent"] == 1
    assert result["failed_agent_ids"] == ["AGT-1"]
    record = db[BROADCAST_COLLECTION].find_one({"_id": "ORD-123-4567"})
    assert record["sent"] == 1 and record["recipients"] == 2
    assert record["failed_agent_ids"] == ["AGT-1"]


def test_a_database_error_is_not_an_exception(db):
    with _enabled(), patch.object(agent_whatsapp, "agent_recipients", side_effect=RuntimeError("boom")):
        assert broadcast_job_order(ORDER, database=db) == {"sent": 0, "reason": "error"}


# --------------------------------------------------------------------------- #
#  When it runs: only for a new job order
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(db):
    user = {"id": "u1", "sub": "u1", "email": "rafi@agency.test", "name": "Rafi",
            "role": "admin", "pages": ["job-orders", "b2b-enquiries"]}
    with patch("app.api.routes.ensure_indexes"), \
         patch("app.db.mongo.get_db", return_value=db), \
         patch("app.db.b2b_enquiries.get_db", return_value=db):
        from app.api.routes import current_user

        fastapi_app.dependency_overrides[current_user] = lambda: user
        yield TestClient(fastapi_app)
        fastapi_app.dependency_overrides.clear()


def test_creating_a_job_order_starts_the_broadcast_and_resaving_does_not(client):
    with patch.object(agent_whatsapp, "start_job_order_broadcast") as start:
        assert client.post("/job-orders", json=ORDER).status_code == 200
        assert client.post("/job-orders", json={**ORDER, "headcount": 15}).status_code == 200
        assert client.put(f"/job-orders/{ORDER['id']}", json=ORDER).status_code == 200
    start.assert_called_once()
    assert start.call_args.args[0]["id"] == ORDER["id"]


def test_converting_a_b2b_enquiry_starts_the_broadcast(client, db):
    from app.db import b2b_enquiries

    db[b2b_enquiries.COLLECTION].insert_one({
        "id": "ENQ-1", "status": "reviewing", "party_type": "client",
        "contact_name": "Ravi", "phone": "9876500000",
    })
    with patch.object(agent_whatsapp, "start_job_order_broadcast") as start:
        response = client.post("/b2b-enquiries/ENQ-1/convert", json={
            "title": "Structural Welder", "client": "Ravi Manpower Services",
            "headcount": 40, "due_date": "2026-03-01", "destination_country": "Qatar",
        })
    assert response.status_code == 201, response.text
    start.assert_called_once()
    assert start.call_args.args[0]["status"] == "OPEN"
