"""Shared avatars and durable manual-reassignment remarks."""
import base64
from io import BytesIO
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient
from PIL import Image

from app.api import routes
from app.core.security import create_token
from app.db.repository import CandidateRepository
from app.db.users import User, UserRepository


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_token(user.id, routes.settings.auth_secret)}"}


def test_profile_photo_is_shared_with_user_management_and_only_updates_self():
    database = mongomock.MongoClient()["profile_photo_test"]
    users = UserRepository(collection=database["users"])
    admin = users.create("admin@example.com", "password123", "Admin", "admin")
    staff = users.create("staff@example.com", "password123", "Staff", "staff")
    image_bytes = BytesIO()
    Image.new("RGB", (256, 256), "blue").save(image_bytes, format="JPEG")
    photo = "data:image/jpeg;base64," + base64.b64encode(image_bytes.getvalue()).decode()

    with patch("app.api.routes.users", users):
        http = TestClient(routes.app)
        invalid = http.put("/auth/me/photo", headers=_auth(staff), json={"photo": "data:text/html;base64,abc"})
        saved = http.put("/auth/me/photo", headers=_auth(staff), json={"photo": photo})
        accounts = http.get("/users", headers=_auth(admin))
        own = http.get("/auth/me", headers=_auth(staff))

    assert invalid.status_code == 400
    assert saved.status_code == 200
    assert own.json()["user"]["profile_photo"] == photo
    assert next(item for item in accounts.json()["items"] if item["id"] == staff.id)["profile_photo"] == photo
    assert users.get(admin.id).profile_photo == ""


def test_reassignment_remark_is_saved_with_actor_and_survives_repeat_request():
    database = mongomock.MongoClient(tz_aware=True)["assignment_remark_test"]
    database["candidates"].insert_one({
        "_id": "candidate-1",
        "profile": {"full_name": "Candidate One"},
        "source": "manual",
        "assigned_staff_id": "old-staff",
        "assigned_staff_name": "Old Owner",
    })
    repository = CandidateRepository(collection=database["candidates"])
    new_owner = User(id="new-staff", email="new@example.com", name="New Owner", role="staff")

    class StaffLookup:
        def get(self, staff_id):
            return new_owner if staff_id == new_owner.id else None

    with patch("app.api.routes.users", StaffLookup()), patch("app.api.routes.repo", return_value=repository), patch(
        "app.api.routes.notify_candidate_assigned"
    ), patch("app.api.routes.relay_assignment", return_value=True):
        first = routes.assign_candidate_route(
            "candidate-1",
            routes.AssignRequest(staff_id=new_owner.id, remarks="  Covering leave  "),
            _admin={"id": "admin-1", "name": "Admin"},
        )
        repeated = routes.assign_candidate_route(
            "candidate-1",
            routes.AssignRequest(staff_id=new_owner.id, remarks="Ignored"),
            _admin={"id": "admin-1", "name": "Admin"},
        )

    record = repository.get("candidate-1")
    assert first["status"] == "assigned"
    assert repeated["status"] == "unchanged"
    assert record.latest_assignment_remark == "Covering leave"
    assert len(record.assignment_history) == 1
    event = record.assignment_history[0]
    assert event["from_staff_name"] == "Old Owner"
    assert event["to_staff_name"] == "New Owner"
    assert event["by_user_name"] == "Admin"
    assert event["remarks"] == "Covering leave"
    assert event["at"] is not None
