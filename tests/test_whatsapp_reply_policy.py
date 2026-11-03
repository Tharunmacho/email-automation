"""Inbound WhatsApp senders the recruitment bot must leave unanswered."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.routes import app

SERVICE_KEY = "reply-policy-test-key"


class FakeUsers:
    def __init__(self, *phones: str):
        self.members = [SimpleNamespace(phone=phone) for phone in phones]
        self.include_inactive = None

    def list_all(self, include_inactive=True):
        self.include_inactive = include_inactive
        return self.members


class FakeSourcingCollection:
    def __init__(self, *phones: str | dict):
        self.rows = [phone if isinstance(phone, dict) else {"phone": phone} for phone in phones]

    def find(self, query, projection):
        assert query == {}
        assert projection == {"_id": 0, "type": 1, "phone": 1, "contacts.phone": 1}
        return list(self.rows)


class FakeSuppressionCollection:
    def __init__(self, *phones: str):
        from app.db.dedup import normalize_phone

        self.keys = {normalize_phone(phone) for phone in phones}

    def count_documents(self, query, limit=0):
        return int(query.get("phone_key") in self.keys)


def call_policy(phone: str, *, staff=(), sourcing=(), suppressed=(), service_key=SERVICE_KEY):
    users = FakeUsers(*staff)
    database = {
        "sourcing_clients": FakeSourcingCollection(*sourcing),
        "bot_suppression_numbers": FakeSuppressionCollection(*suppressed),
    }
    with (
        patch("app.api.routes.users", users),
        patch("app.services.whatsapp_reply_policy.get_db", return_value=database),
        patch("app.config.settings.whatsapp_service_key", SERVICE_KEY),
    ):
        response = TestClient(app).post(
            "/whatsapp/reply-policy",
            json={"phone": phone},
            headers={"X-Service-Key": service_key},
        )
    return response, users


def test_sourcing_hub_number_is_ignored_across_phone_formatting():
    response, _ = call_policy(
        "919876543210",
        sourcing=("+91 98765 43210", "+60 12-345 6789"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "should_reply": False,
        "action": "ignore",
        "reason": "sourcing_contact_number",
        "sourcing_type": "unknown",
    }


def test_every_sourcing_contact_number_is_ignored():
    response, _ = call_policy(
        "+91 97777 77777",
        sourcing=({
            "phone": "+91 96666 66666",
            "contacts": [
                {"name": "Primary HR", "phone": "+91 96666 66666"},
                {"name": "Second HR", "phone": "+91 97777 77777"},
            ],
        },),
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "sourcing_contact_number"


CLIENT_WITH_TWO_CONTACTS = {
    "type": "client",
    "phone": "+91 95555 55555",
    "contacts": [
        {"name": "Owner", "phone": "+91 95555 55555"},
        {"name": "Site HR", "phone": "+971 50 123 4567"},
    ],
}
CLIENT_WITH_ONE_CONTACT = {
    "type": "Client",
    "phone": "+91 94444 44444",
    "contacts": [{"name": "Owner", "phone": "+91 94444 44444"}],
}


def test_every_client_contact_is_ignored_with_one_contact_or_several():
    for phone in ("919555555555", "971501234567", "9444444444"):
        response, _ = call_policy(
            phone, sourcing=(CLIENT_WITH_TWO_CONTACTS, CLIENT_WITH_ONE_CONTACT)
        )

        assert response.status_code == 200
        assert response.json() == {
            "should_reply": False,
            "action": "ignore",
            "reason": "sourcing_contact_number",
            "sourcing_type": "client",
        }, phone


def test_agent_numbers_are_reported_as_agents_even_when_also_on_a_client_row():
    agent = {"type": "agent", "phone": "+91 93333 33333", "contacts": []}
    shared = {"type": "client", "phone": "+91 93333 33333"}

    response, _ = call_policy("919333333333", sourcing=(shared, agent))

    assert response.json()["reason"] == "sourcing_contact_number"
    assert response.json()["sourcing_type"] == "agent"


def test_suppression_wins_over_sourcing_and_staff_matches():
    agent = {"type": "agent", "phone": "+91 93333 33333"}

    for phone, staff in (("919333333333", ()), ("919222222222", ("+91 92222 22222",))):
        response, _ = call_policy(
            phone,
            staff=staff,
            sourcing=(agent,),
            suppressed=("9333333333", "+91 92222 22222"),
        )

        assert response.json() == {
            "should_reply": False,
            "action": "ignore",
            "reason": "suppressed_number",
        }, phone


def test_staff_number_is_ignored_including_inactive_accounts():
    response, users = call_policy("98765-43210", staff=("+91 98765 43210",))

    assert response.status_code == 200
    assert response.json()["should_reply"] is False
    assert response.json()["reason"] == "internal_user_number"
    assert users.include_inactive is True


def test_unknown_external_number_can_continue_to_the_bot():
    response, _ = call_policy(
        "+91 90000 00000",
        staff=("+91 91111 11111",),
        sourcing=("+91 92222 22222",),
    )

    assert response.status_code == 200
    assert response.json() == {
        "should_reply": True,
        "action": "continue",
        "reason": "external_sender",
    }


def test_admin_suppressed_number_is_ignored_across_formatting():
    response, _ = call_policy(
        "919000000000",
        suppressed=("+91 90000 00000",),
    )

    assert response.status_code == 200
    assert response.json() == {
        "should_reply": False,
        "action": "ignore",
        "reason": "suppressed_number",
    }


def test_malformed_number_is_ignored_instead_of_treated_as_external():
    response, _ = call_policy("N/A")

    assert response.status_code == 200
    assert response.json()["should_reply"] is False
    assert response.json()["reason"] == "invalid_sender_number"


def test_policy_lookup_failure_is_fail_closed():
    class BrokenUsers:
        def list_all(self, include_inactive=True):
            raise RuntimeError("database unavailable")

    database = {
        "sourcing_clients": FakeSourcingCollection(),
        "bot_suppression_numbers": FakeSuppressionCollection(),
    }
    with (
        patch("app.api.routes.users", BrokenUsers()),
        patch("app.services.whatsapp_reply_policy.get_db", return_value=database),
        patch("app.config.settings.whatsapp_service_key", SERVICE_KEY),
    ):
        response = TestClient(app).post(
            "/whatsapp/reply-policy",
            json={"phone": "+91 98765 43210"},
            headers={"X-Service-Key": SERVICE_KEY},
        )

    assert response.status_code == 200
    assert response.json()["should_reply"] is False
    assert response.json()["reason"] == "policy_lookup_unavailable"


def test_reply_policy_requires_the_bot_service_key():
    response, _ = call_policy("+91 90000 00000", service_key="wrong-key")
    assert response.status_code == 401
