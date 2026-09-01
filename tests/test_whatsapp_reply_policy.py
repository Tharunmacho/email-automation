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
    def __init__(self, *phones: str):
        self.rows = [{"phone": phone} for phone in phones]

    def find(self, query, projection):
        assert query == {}
        assert projection == {"_id": 0, "phone": 1}
        return list(self.rows)


def call_policy(phone: str, *, staff=(), sourcing=(), service_key=SERVICE_KEY):
    users = FakeUsers(*staff)
    database = {"sourcing_clients": FakeSourcingCollection(*sourcing)}
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
    }


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


def test_malformed_number_is_ignored_instead_of_treated_as_external():
    response, _ = call_policy("N/A")

    assert response.status_code == 200
    assert response.json()["should_reply"] is False
    assert response.json()["reason"] == "invalid_sender_number"


def test_policy_lookup_failure_is_fail_closed():
    class BrokenUsers:
        def list_all(self, include_inactive=True):
            raise RuntimeError("database unavailable")

    with (
        patch("app.api.routes.users", BrokenUsers()),
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
