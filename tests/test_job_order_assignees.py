"""Who handles a Sourcing Hub agent: the staff dropdowns and assigning agents.

Every agent is handled by one staff member, picked by name and phone number
(GET /job-orders/assignees, a path kept from when job orders had a staff
member). The Sourcing Hub reaches this list without the Staff or Users pages,
so it carries only what the dropdown shows. Existing agents are assigned in
bulk with POST /sourcing-clients/handling-staff.
"""
from __future__ import annotations

from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.db.users import UserRepository


@pytest.fixture
def api(monkeypatch):
    store = UserRepository(mongomock.MongoClient(tz_aware=True)["crm"]["users"])
    store.create("admin@x.com", "pw-123456", name="Admin", role="admin", phone="+91 90000 00001")
    store.create("zara@x.com", "pw-123456", name="Zara", role="staff", phone="+91 98849 49735")
    store.create("arun@x.com", "pw-123456", name="Arun", role="staff", phone="")
    store.create("mona@x.com", "pw-123456", name="Mona", role="manager", phone="+91 90000 00002")
    gone = store.create("gone@x.com", "pw-123456", name="Gone", role="staff", phone="+91 90000 00003")
    store.update_user(gone.id, active=False)
    store.create("driver@x.com", "pw-123456", name="Driver", role="staff", phone="+91 90000 00004", crm_access=False)
    monkeypatch.setattr(routes, "users", store)

    def sign_in(pages):
        routes.app.dependency_overrides[routes.current_user] = lambda: {
            "id": "u-1", "email": "rec@x.com", "name": "Recruiter", "role": "staff",
            "pages": pages, "actions": [],
        }

    client = TestClient(routes.app)
    client.sign_in = sign_in  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        routes.app.dependency_overrides.pop(routes.current_user, None)


def test_active_staff_and_managers_who_use_the_crm_are_listed_by_name(api):
    api.sign_in(["job-orders"])
    response = api.get("/job-orders/assignees")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [(item["name"], item["phone"], item["role"]) for item in items] == [
        ("Arun", "", "staff"),
        ("Mona", "+91 90000 00002", "manager"),
        ("Zara", "+91 98849 49735", "staff"),
    ]


def test_the_list_carries_only_what_the_dropdown_shows(api):
    api.sign_in(["job-orders"])
    for item in api.get("/job-orders/assignees").json()["items"]:
        assert set(item) == {"id", "name", "phone", "role"}


def test_the_list_needs_the_job_orders_or_sourcing_page(api):
    # Like every page-guarded route, missing access reads as "no such endpoint".
    api.sign_in(["candidates"])
    assert api.get("/job-orders/assignees").status_code == 404
    # The Sourcing Hub assigns agents to staff, so it reads the list too.
    api.sign_in(["sourcing"])
    assert api.get("/job-orders/assignees").status_code == 200


# --------------------------------------------------------------------------- #
#  Assigning agents in bulk
# --------------------------------------------------------------------------- #
@pytest.fixture
def sourcing_db():
    database = mongomock.MongoClient(tz_aware=True)["crm"]
    database["sourcing_clients"].insert_many([
        {"id": "AGT-1", "name": "Gulf Manpower", "type": "agent", "status": "ACTIVE",
         "phone": "98765 43210", "address": "Chennai"},
        {"id": "AGT-2", "name": "Chennai Agency", "type": "agent", "status": "ACTIVE",
         "handlingStaff": {"id": "old", "name": "Old", "phone": "1"}},
        {"id": "CLI-1", "name": "A client", "type": "client", "status": "ACTIVE"},
    ])
    with patch("app.db.mongo.get_db", return_value=database):
        yield database


def _staff_id(api, name):
    return next(item["id"] for item in api.get("/job-orders/assignees").json()["items"] if item["name"] == name)


def test_agents_are_assigned_to_staff_and_nothing_else_on_them_changes(api, sourcing_db):
    api.sign_in(["sourcing"])
    zara, mona = _staff_id(api, "Zara"), _staff_id(api, "Mona")

    response = api.post("/sourcing-clients/handling-staff", json={"assignments": [
        {"agent_id": "AGT-1", "staff_id": zara},
        {"agent_id": "AGT-2", "staff_id": mona},
        # Not an agent, and not a record: left alone and reported.
        {"agent_id": "CLI-1", "staff_id": zara},
        {"agent_id": "AGT-404", "staff_id": zara},
    ]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [record["id"] for record in body["updated"]] == ["AGT-1", "AGT-2"]
    assert body["skipped"] == ["CLI-1", "AGT-404"]
    agent = sourcing_db["sourcing_clients"].find_one({"id": "AGT-1"}, {"_id": 0})
    assert agent["handlingStaff"] == {"id": zara, "name": "Zara", "phone": "+91 98849 49735"}
    assert agent["address"] == "Chennai" and agent["phone"] == "98765 43210"
    assert sourcing_db["sourcing_clients"].find_one({"id": "AGT-2"})["handlingStaff"]["name"] == "Mona"
    assert "handlingStaff" not in sourcing_db["sourcing_clients"].find_one({"id": "CLI-1"})


def test_only_staff_the_dropdown_offers_can_be_assigned(api, sourcing_db):
    api.sign_in(["sourcing"])
    gone = routes.users.find_by_email("gone@x.com")["_id"]
    response = api.post("/sourcing-clients/handling-staff", json={"assignments": [
        {"agent_id": "AGT-1", "staff_id": gone},
    ]})
    assert response.status_code == 422
    assert "handlingStaff" not in sourcing_db["sourcing_clients"].find_one({"id": "AGT-1"})


def test_assigning_agents_needs_the_sourcing_page(api, sourcing_db):
    api.sign_in(["job-orders"])
    response = api.post("/sourcing-clients/handling-staff", json={"assignments": [
        {"agent_id": "AGT-1", "staff_id": "anyone"},
    ]})
    assert response.status_code == 404
