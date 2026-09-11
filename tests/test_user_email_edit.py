"""Editable sign-in emails retain account identity and existing API guards."""
from unittest.mock import patch

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app.api import routes
from app.core.security import create_token
from app.db.users import UserRepository

PASSWORD = "Original Password 123"


@pytest.fixture
def user_api():
    collection = mongomock.MongoClient()["user_email_edit"]["users"]
    collection.create_index("email", unique=True)
    repository = UserRepository(collection=collection)
    accounts = {
        role: repository.create(email=f"{role}@example.com", password=PASSWORD, name=role.title(), role=role)
        for role in ["admin", "manager", "staff"]
    }
    with patch("app.api.routes.users", repository):
        yield TestClient(routes.app), repository, accounts


def authorization(account):
    token = create_token(account.id, routes.settings.auth_secret)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("role", ["admin", "manager", "staff"])
def test_admin_can_update_normalized_email_without_changing_account(user_api, role):
    http, repository, accounts = user_api
    target = accounts[role]
    before = repository._coll.find_one({"_id": target.id})

    response = http.patch(
        f"/users/{target.id}",
        headers=authorization(accounts["admin"]),
        json={"email": f"  New.{role.title()}@Example.COM  "},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["user"]["email"] == f"new.{role}@example.com"
    assert body["user"]["id"] == target.id
    after = repository._coll.find_one({"_id": target.id})
    for key in ["password_hash", "role", "name", "active", "page_grants", "staff_code"]:
        assert after.get(key) == before.get(key)
    assert repository.authenticate(target.email, PASSWORD) is None
    assert repository.authenticate(f"new.{role}@example.com", PASSWORD).id == target.id
    # Tokens are tied to the stable account id, including an admin editing self.
    assert http.get("/auth/me", headers=authorization(target)).json()["user"]["email"] == f"new.{role}@example.com"
    assert repository.count_active_admins() == 1


def test_duplicate_email_is_rejected_without_partial_edits(user_api):
    http, repository, accounts = user_api
    target = accounts["staff"]
    repository.update_user(accounts["manager"].id, active=False)
    response = http.patch(
        f"/users/{target.id}",
        headers=authorization(accounts["admin"]),
        json={"email": " MANAGER@Example.com ", "name": "Must not be saved"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "A user with email manager@example.com already exists."
    assert repository.get(target.id).email == target.email
    assert repository.get(target.id).name == target.name


def test_unique_index_race_returns_friendly_duplicate_error(user_api):
    http, repository, accounts = user_api
    with patch.object(repository._coll, "update_one", side_effect=DuplicateKeyError("E11000 duplicate email")):
        response = http.patch(
            f"/users/{accounts['staff'].id}",
            headers=authorization(accounts["admin"]),
            json={"email": "raced@example.com"},
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "A user with email raced@example.com already exists."
    assert repository.get(accounts["staff"].id).email == "staff@example.com"


@pytest.mark.parametrize("email", ["", "   "])
def test_blank_email_is_invalid_and_preserves_account(user_api, email):
    http, repository, accounts = user_api
    response = http.patch(
        f"/users/{accounts['staff'].id}",
        headers=authorization(accounts["admin"]),
        json={"email": email},
    )
    assert response.status_code == 422
    assert repository.get(accounts["staff"].id).email == "staff@example.com"


def test_omitted_or_unchanged_email_preserves_existing_edits(user_api):
    http, repository, accounts = user_api
    target = accounts["staff"]
    for payload in [{"name": "Renamed"}, {"email": " STAFF@EXAMPLE.COM "}]:
        response = http.patch(f"/users/{target.id}", headers=authorization(accounts["admin"]), json=payload)
        assert response.status_code == 200
    assert repository.get(target.id).name == "Renamed"
    assert repository.get(target.id).email == "staff@example.com"


@pytest.mark.parametrize("role", ["manager", "staff"])
@pytest.mark.parametrize("self_edit", [False, True])
def test_accounts_without_user_management_permission_cannot_edit_email(user_api, role, self_edit):
    http, repository, accounts = user_api
    actor = accounts[role]
    target = actor if self_edit else accounts["admin"]
    response = http.patch(
        f"/users/{target.id}", headers=authorization(actor), json={"email": "unauthorized@example.com"},
    )
    assert response.status_code == 404
    assert repository.get(target.id).email == target.email


@pytest.mark.parametrize("role", ["manager", "staff"])
def test_existing_explicit_user_management_grants_still_authorize_edit(user_api, role):
    http, repository, accounts = user_api
    actor = accounts[role]
    repository.update_user(actor.id, page_grants=["users"])
    response = http.patch(
        f"/users/{actor.id}", headers=authorization(actor), json={"email": "authorized@example.com"},
    )
    assert response.status_code == 200
    assert repository.get(actor.id).email == "authorized@example.com"
    assert repository.get(actor.id).role == role


def test_email_edit_does_not_bypass_last_admin_guard(user_api):
    http, repository, accounts = user_api
    admin = accounts["admin"]
    response = http.patch(
        f"/users/{admin.id}",
        headers=authorization(admin),
        json={"email": "changed@example.com", "role": "staff"},
    )
    assert response.status_code == 409
    assert repository.get(admin.id).email == admin.email
    assert repository.get(admin.id).role == "admin"
