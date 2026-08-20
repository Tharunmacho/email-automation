"""The role boundary as the HTTP API actually enforces it.

The unit tests cover the scoping helper; these cover the thing that matters —
that a staff member calling the real routes cannot reach another person's
candidate or the admin's console.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app, current_user
from app.core.models import CandidateProfile, CandidateRecord, SourceEmail, StoredResume


def make_record(candidate_id: str, name: str, staff_id: str | None) -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id,
        profile=CandidateProfile(is_resume=True, confidence=0.9, full_name=name, email=f"{candidate_id}@x.com"),
        resume=StoredResume(
            original_filename=f"{candidate_id}.pdf",
            mime_type="application/pdf",
            size=1024,
            sha256=f"hash-{candidate_id}",
            storage_backend="local",
            storage_key=f"k/{candidate_id}",
        ),
        source_email=SourceEmail(
            message_id=f"m-{candidate_id}",
            thread_id=f"t-{candidate_id}",
            from_addr=f"{candidate_id}@x.com",
        ),
        status="ingested",
        assigned_staff_id=staff_id,
    )


class ScopedRepo:
    """A repository that honours `staff_id` exactly as the real one does."""

    def __init__(self, records):
        self.records = {r.id: r for r in records}
        self.viewed = []
        self.evaluations = []

    def _scope(self, staff_id):
        rows = list(self.records.values())
        return [r for r in rows if r.assigned_staff_id == staff_id] if staff_id else rows

    def get(self, candidate_id):
        return self.records.get(candidate_id)

    def count(self, query=None, staff_id=None):
        return len(self._scope(staff_id))

    def list_summaries(self, limit=50, skip=0, minimal=False, query=None, staff_id=None):
        return [
            {"id": r.id, "profile": {"full_name": r.profile.full_name}, "assigned_staff_id": r.assigned_staff_id}
            for r in self._scope(staff_id)[skip : skip + limit]
        ]

    def mark_viewed(self, candidate_id, staff_id=None):
        self.viewed.append((candidate_id, staff_id))
        return True

    def save_evaluation(self, candidate_id, staff_id, status, score, notes):
        self.evaluations.append((candidate_id, staff_id, status, score, notes))
        return self.records.get(candidate_id)

    def workload_counts(self):
        return {}


@pytest.fixture
def api():
    """A client whose signed-in role each test chooses."""
    repo = ScopedRepo(
        [
            make_record("cand-mine", "Mine Candidate", "staff-1"),
            make_record("cand-theirs", "Their Candidate", "staff-2"),
        ]
    )

    def sign_in_as(role: str, user_id: str = "staff-1"):
        app.dependency_overrides[current_user] = lambda: {
            "id": user_id,
            "email": f"{user_id}@x.com",
            "name": user_id,
            "role": role,
        }

    with patch("app.api.routes.repo", return_value=repo):
        client = TestClient(app)
        client.sign_in_as = sign_in_as  # type: ignore[attr-defined]
        client.repo = repo  # type: ignore[attr-defined]
        try:
            yield client
        finally:
            app.dependency_overrides.pop(current_user, None)


# --------------------------------------------------------------------------- #
#  Listing
# --------------------------------------------------------------------------- #
def test_staff_listing_shows_only_their_own(api):
    api.sign_in_as("staff", "staff-1")
    body = api.get("/candidates").json()
    assert [row["id"] for row in body["items"]] == ["cand-mine"]
    assert body["total"] == 1


def test_admin_listing_shows_everything(api):
    api.sign_in_as("admin", "admin-1")
    body = api.get("/candidates").json()
    assert {row["id"] for row in body["items"]} == {"cand-mine", "cand-theirs"}


# --------------------------------------------------------------------------- #
#  Detail
# --------------------------------------------------------------------------- #
def test_staff_can_open_their_own_candidate(api):
    api.sign_in_as("staff", "staff-1")
    assert api.get("/candidates/cand-mine").status_code == 200


def test_staff_gets_404_not_403_for_someone_elses_candidate(api):
    """403 would confirm the record exists, which is the fact the isolation rule
    is there to withhold — the id must be indistinguishable from a missing one."""
    api.sign_in_as("staff", "staff-1")
    response = api.get("/candidates/cand-theirs")
    assert response.status_code == 404
    assert api.get("/candidates/does-not-exist").status_code == 404


def test_staff_cannot_download_someone_elses_resume(api):
    api.sign_in_as("staff", "staff-1")
    assert api.get("/candidates/cand-theirs/resume").status_code == 404


# --------------------------------------------------------------------------- #
#  Admin-only routes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/staff"),
        ("get", "/staff/workload"),
        ("post", "/staff"),
        ("delete", "/staff/someone"),
        ("post", "/candidates/rebalance"),
        ("post", "/candidates/cand-mine/assign"),
        ("get", "/sla/alerts"),
        ("get", "/sla/breaches"),
        ("post", "/sla/scan"),
    ],
)
def test_staff_are_refused_every_admin_route(api, method, path):
    api.sign_in_as("staff", "staff-1")
    # Only POST carries a body; the client rejects `json=` on GET and DELETE.
    kwargs = {"json": {}} if method == "post" else {}
    response = getattr(api, method)(path, **kwargs)
    assert response.status_code == 403, (
        f"{method.upper()} {path} answered {response.status_code}, not 403"
    )


# --------------------------------------------------------------------------- #
#  Evaluation
# --------------------------------------------------------------------------- #
def test_marking_viewed_is_scoped_to_the_caller(api):
    """The staff id has to reach the repository, or the query would stamp a
    profile belonging to someone else."""
    api.sign_in_as("staff", "staff-1")
    assert api.post("/candidates/cand-mine/view").status_code == 200
    assert api.repo.viewed == [("cand-mine", "staff-1")]


def test_admin_marking_viewed_is_not_scoped(api):
    api.sign_in_as("admin", "admin-1")
    api.post("/candidates/cand-mine/view")
    assert api.repo.viewed == [("cand-mine", None)]


def test_staff_cannot_evaluate_someone_elses_candidate(api):
    api.sign_in_as("staff", "staff-1")
    response = api.post("/candidates/cand-theirs/evaluate", json={"status": "shortlisted", "score": 4})
    assert response.status_code == 404
    assert api.repo.evaluations == []


def test_evaluation_records_status_score_and_notes(api):
    api.sign_in_as("staff", "staff-1")
    response = api.post(
        "/candidates/cand-mine/evaluate",
        json={"status": "shortlisted", "score": 4, "notes": "Strong fabrication background."},
    )
    assert response.status_code == 200
    assert api.repo.evaluations == [
        ("cand-mine", "staff-1", "shortlisted", 4, "Strong fabrication background.")
    ]


def test_unknown_evaluation_status_is_rejected(api):
    api.sign_in_as("staff", "staff-1")
    response = api.post("/candidates/cand-mine/evaluate", json={"status": "maybe-ish"})
    assert response.status_code == 400
    assert api.repo.evaluations == []


@pytest.mark.parametrize("score", [0, 6, -1])
def test_score_outside_one_to_five_is_rejected(api, score):
    api.sign_in_as("staff", "staff-1")
    response = api.post("/candidates/cand-mine/evaluate", json={"status": "shortlisted", "score": score})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
#  Demo accounts
# --------------------------------------------------------------------------- #
def test_demo_endpoint_offers_only_the_super_admin(api):
    """The login screen advertises one way in, and it is the admin dashboard.

    The staff account still exists and still signs in — see the seeding tests
    below — it is just not published to anyone who loads the login page.
    """
    body = api.get("/auth/demo-accounts").json()
    assert body["enabled"] is True
    by_role = {a["role"]: a for a in body["accounts"]}
    assert set(by_role) == {"admin"}
    from app.config import settings
    assert by_role["admin"]["email"] == settings.admin_email
    # A button that fills a blank password would fail the sign-in it triggers.
    assert all(a["password"] for a in body["accounts"])


def test_demo_endpoint_goes_quiet_when_demo_mode_is_off(api, monkeypatch):
    """Turning demo mode off must stop the credentials being published — the
    endpoint is unauthenticated, and one of the two accounts is an admin."""
    from app.api import routes

    monkeypatch.setattr(routes.settings, "demo_accounts_enabled", False)
    body = api.get("/auth/demo-accounts").json()
    assert body == {"enabled": False, "accounts": []}


def test_demo_seeding_creates_one_account_per_role():
    """Both roles have to exist, because the staff account is the only thing
    that proves the isolation — there is nothing to isolate without one."""
    from app.db.users import ADMIN_ROLE, STAFF_ROLE, ensure_demo_accounts

    created_docs = []

    class FakeRepo:
        def find_by_email(self, email):
            return next((d for d in created_docs if d["email"] == email), None)

        def create(self, email, password, name, role, **_kwargs):
            created_docs.append({"email": email, "role": role, "name": name})
            return None

    with patch("app.db.users.UserRepository", FakeRepo):
        created = ensure_demo_accounts("a@x.com", "pw1", "s@x.com", "pw2")

    assert created == ["a@x.com", "s@x.com"]
    assert [d["role"] for d in created_docs] == [ADMIN_ROLE, STAFF_ROLE]


def test_demo_seeding_never_resets_an_existing_account():
    """An operator who changes the demo password keeps that change across
    restarts — seeding creates, it does not overwrite."""
    from app.db.users import ensure_demo_accounts

    class FakeRepo:
        def find_by_email(self, email):
            return {"email": email}  # everything already exists

        def create(self, **_kwargs):  # pragma: no cover - must never run
            raise AssertionError("seeding overwrote an existing account")

    with patch("app.db.users.UserRepository", FakeRepo):
        assert ensure_demo_accounts("a@x.com", "pw1", "s@x.com", "pw2") == []


# --------------------------------------------------------------------------- #
#  Evaluation statuses
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["shortlisted", "interviewing", "rejected"])
def test_the_three_statuses_the_evaluator_offers_are_accepted(api, status):
    api.sign_in_as("staff", "staff-1")
    response = api.post("/candidates/cand-mine/evaluate", json={"status": status, "score": 3})
    assert response.status_code == 200, f"{status} was rejected by the API"


# --------------------------------------------------------------------------- #
#  WebSocket auth
# --------------------------------------------------------------------------- #
def test_websocket_refuses_a_connection_with_no_token(api):
    """The socket must be rejected before accept(), so an unauthenticated client
    never reaches a state where it could be sent an event."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with api.websocket_connect("/ws?token=") as socket:
            socket.receive_text()
    assert excinfo.value.code == 4401


def test_websocket_refuses_a_forged_token(api):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with api.websocket_connect("/ws?token=not.a.real.token") as socket:
            socket.receive_text()
