"""The staff dropdown on the job order dialogs: GET /job-orders/assignees.

A job order is assigned to one staff member, picked by name and phone number.
The Job Orders page reaches this list without the Staff or Users pages, so it
carries only what the dropdown shows.
"""
from __future__ import annotations

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


def test_the_list_needs_the_job_orders_page(api):
    # Like every page-guarded route, missing access reads as "no such endpoint".
    api.sign_in(["candidates"])
    assert api.get("/job-orders/assignees").status_code == 404
