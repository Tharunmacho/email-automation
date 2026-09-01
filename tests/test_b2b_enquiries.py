"""B2B enquiries: the rules that must hold, stated as tests.

An enquiry is the other thing the WhatsApp bot collects. A candidate answers
questions about themselves; an *agent* describes a vacancy and asks the agency
to fill it. The cases here are the ways that second flow can go wrong, and each
group exists because of a specific failure:

* **Separation** — an enquiry must not become a candidate, and a candidate must
  not become an enquiry. They are two collections with two shapes, and the
  whole reason for the second one is that the first was the wrong home.
* **Idempotency** — a retried submission must resolve to the enquiry it already
  created, not to a second vacancy. Unlike the candidate intake, the key is per
  *enquiry*: an agent raises many, and every one of them is real.
* **Authority** — `converted` must mean a job order exists. A status a caller
  can set on its own is a status that lies, and the lie is a job the agency
  either raises twice or never raises at all.
* **Credentials** — the bot's key opens the bot's endpoint and nothing else. A
  recruiter's session opens the screen's endpoints and cannot be a service key.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Imported at module scope, deliberately — see the same note in
# `tests/test_whatsapp_intake.py`. `app/api/routes.py` builds a
# `UserRepository()` at import time, which opens a Mongo connection, and
# `tests/conftest.py` refuses connections once its autouse fixture is active.
from app.api.routes import app as fastapi_app
from app.db import b2b_enquiries

SERVICE_KEY = "test-service-key-12345"


# --------------------------------------------------------------------------- #
#  Doubles
# --------------------------------------------------------------------------- #
class FakeCollection:
    """An in-memory collection that models the constraint that matters.

    Specifically the unique index on `idempotency_key`: `insert_one` refuses a
    second document carrying a key it already holds, the way MongoDB would.
    Without that, the concurrency test would pass against a dictionary and
    prove nothing about the database it is meant to describe.
    """

    def __init__(self, docs=None):
        self.docs: list[dict] = list(docs or [])

    # ---- reads ---- #
    def find_one(self, query, projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return {k: v for k, v in doc.items() if k != "_id"}
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        matched = [
            {k: v for k, v in doc.items() if k != "_id"}
            for doc in self.docs
            if all(doc.get(k) == v for k, v in query.items())
        ]
        return _FakeCursor(matched)

    def aggregate(self, pipeline):
        counts: dict[str, int] = {}
        for doc in self.docs:
            counts[doc.get("status")] = counts.get(doc.get("status"), 0) + 1
        return [{"_id": status, "n": n} for status, n in counts.items()]

    # ---- writes ---- #
    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError

        key = doc.get("idempotency_key")
        if key and any(d.get("idempotency_key") == key for d in self.docs):
            raise DuplicateKeyError("idempotency_key")
        self.docs.append(doc)
        return type("R", (), {"inserted_id": doc.get("id")})()

    def update_one(self, query, update):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                doc.update(update.get("$set", {}))
                return type("R", (), {"matched_count": 1, "modified_count": 1})()
        return type("R", (), {"matched_count": 0, "modified_count": 0})()

    def delete_one(self, query):
        for i, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in query.items()):
                self.docs.pop(i)
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()


class _FakeCursor(list):
    """Enough of a cursor for `.sort().limit()` to chain the way the code uses it."""

    def sort(self, key, direction=1):
        self.sort_key = key
        return _FakeCursor(
            sorted(self, key=lambda d: str(d.get(key) or ""), reverse=direction < 0)
        )

    def limit(self, n):
        return _FakeCursor(self[:n])


@pytest.fixture
def enquiries():
    """The enquiry collection, and the sourcing collection it matches against."""
    return FakeCollection()


@pytest.fixture
def sourcing():
    return FakeCollection()


@pytest.fixture
def client(enquiries, sourcing):
    """A TestClient with both credentials configured and Mongo kept out of it.

    `TestClient(app)` rather than `with TestClient(app)`: entering the context
    manager fires FastAPI's startup event, which calls `ensure_indexes` and
    reaches the real database. The rest of the suite constructs it the same way
    for the same reason.
    """
    fake_db = {"b2b_enquiries": enquiries, "sourcing_clients": sourcing, "job_orders": FakeCollection()}

    admin = {
        "id": "admin-1",
        "sub": "admin-1",
        "email": "admin@agency.test",
        "name": "admin",
        "role": "admin",
        "pages": ["b2b-enquiries"],
    }

    with patch("app.api.routes.ensure_indexes"), \
         patch("app.config.settings.whatsapp_service_key", SERVICE_KEY), \
         patch("app.db.b2b_enquiries.get_db", return_value=fake_db), \
         patch("app.db.mongo.get_db", return_value=fake_db):
        # `current_user` resolves a session into a user dict. Overridden rather
        # than issued a real token: what these tests are about is what the
        # endpoints do once someone is through the door, and minting a JWT to
        # get there would test `create_token` a fourteenth time.
        #
        # It has to be `current_user` and not `require_admin`, which these
        # endpoints stopped using when they moved behind
        # `require_page("b2b-enquiries")`. `require_page` is a factory, so every
        # `Depends(require_page(...))` holds a *different* closure and none of
        # them is a key anything can override — but they all resolve through
        # `current_user`, which is the one seam that works for all of them.
        # Overriding the old dependency simply stopped having any effect, and
        # the endpoints answered 401 to a suite that believed it was signed in.
        from app.api.routes import current_user

        fastapi_app.dependency_overrides[current_user] = lambda: admin
        c = TestClient(fastapi_app)
        c.enquiries = enquiries
        c.sourcing = sourcing
        c.job_orders = fake_db["job_orders"]
        yield c
        fastapi_app.dependency_overrides.clear()


def bot_payload(**overrides) -> dict:
    body = {
        "idempotency_key": "whatsapp/111/919876543210/enq-1",
        "party_type": "agent",
        "company_name": "Ravi Manpower Services",
        "contact_name": "Ravi Kumar",
        "phone": "+919876543210",
        "email": "ravi@manpower.test",
        "country": "India",
        "requirement": "Need 40 welders for a Qatar site, joining before Ramadan.",
        "job_title": "Structural Welder",
        "headcount": 40,
        "destination_country": "Qatar",
        "needed_by": "before Ramadan",
    }
    body.update(overrides)
    return body


def post_enquiry(client, **overrides):
    return client.post(
        "/b2b-enquiries",
        json=bot_payload(**overrides),
        headers={"X-Service-Key": SERVICE_KEY},
    )


# --------------------------------------------------------------------------- #
#  Credentials
# --------------------------------------------------------------------------- #
def test_bot_endpoint_refuses_a_missing_service_key(client):
    """No credential is refused for having no credential, before validation.

    The dependency resolves before the body does, so a caller with no key never
    gets a 422 describing the shape of an endpoint it may not call.
    """
    response = client.post("/b2b-enquiries", json=bot_payload())
    assert response.status_code == 401


def test_bot_endpoint_refuses_a_wrong_service_key(client):
    response = client.post(
        "/b2b-enquiries", json=bot_payload(), headers={"X-Service-Key": "not-the-key"}
    )
    assert response.status_code == 401


def test_service_key_does_not_open_the_recruiter_endpoints(client):
    """The bot files enquiries; it does not read the agency's pipeline.

    `GET /b2b-enquiries` returns every company that has ever been in touch and
    what they are hiring for. The bot has no use for that, and a key that opened
    it would make one stolen secret a competitor's client list.
    """
    from app.api.routes import current_user

    # The admin override is what the other tests ride in on. Lifted here so the
    # real dependency runs and the service key is judged on its own merits.
    #
    # It must name the same dependency the fixture overrode. While this popped
    # `require_admin` — a key nothing had been registered under since these
    # endpoints moved to `require_page` — the lift was a no-op, and the 401 this
    # asserts came from the *fixture* failing to sign anybody in rather than
    # from the service key being turned away. The security claim was true; this
    # test was not the reason to believe it.
    fastapi_app.dependency_overrides.pop(current_user, None)
    response = client.get("/b2b-enquiries", headers={"X-Service-Key": SERVICE_KEY})
    assert response.status_code in (401, 403)


# --------------------------------------------------------------------------- #
#  Intake
# --------------------------------------------------------------------------- #
def test_bot_files_an_enquiry(client):
    response = post_enquiry(client)

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["enquiry_id"].startswith("ENQ-")
    assert body["status"] == "new"

    stored = client.enquiries.docs[0]
    assert stored["company_name"] == "Ravi Manpower Services"
    assert stored["headcount"] == 40
    assert stored["destination_country"] == "Qatar"
    assert stored["source"] == "whatsapp"


def test_an_enquiry_is_not_a_candidate(client):
    """The two flows do not share a collection.

    This is the whole reason the feature exists: filing a company as a candidate
    would put it in a recruiter's review queue and allocate it to a staff member
    as if it were a person.
    """
    with patch("app.api.routes.repo") as candidate_repo:
        post_enquiry(client)
        candidate_repo.assert_not_called()


def test_contact_name_is_required(client):
    """The one field a row cannot be rendered without.

    Everything else is optional on purpose — an agent who says "40 welders for
    Qatar" and leaves has raised a real enquiry — but a row nobody can be called
    back on is not one a recruiter can act on.
    """
    payload = bot_payload()
    del payload["contact_name"]
    response = client.post(
        "/b2b-enquiries", json=payload, headers={"X-Service-Key": SERVICE_KEY}
    )
    assert response.status_code == 422


def test_a_sparse_enquiry_is_still_accepted(client):
    """Refusing this would lose the enquiry rather than improve it."""
    response = client.post(
        "/b2b-enquiries",
        json={
            "idempotency_key": "whatsapp/111/60123456789/enq-9",
            "contact_name": "Unknown caller",
            "requirement": "wants people for Malaysia, will send details",
        },
        headers={"X-Service-Key": SERVICE_KEY},
    )
    assert response.status_code == 201
    stored = client.enquiries.docs[0]
    # Absent, not zero: a job order raised for 0 seats is FILLED the moment it
    # exists and vanishes from the list it was raised to appear on.
    assert stored["headcount"] is None
    assert stored["company_name"] == ""


def test_unparseable_headcount_is_dropped_not_guessed(client):
    response = post_enquiry(client, headcount=None)
    assert response.status_code == 201
    assert client.enquiries.docs[0]["headcount"] is None


def test_extra_fields_from_the_bot_are_dropped_at_the_door(client):
    """The allow-list is doing real work.

    The bot's own record carries Aadhaar and PAN numbers for candidates. This
    system has no screen that shows them and no workflow that reads them, and a
    model that accepted whatever arrived would store them the first time a
    mapping bug sent them.
    """
    response = post_enquiry(client, aadhaar_number="1234 5678 9012", pan="ABCDE1234F")
    assert response.status_code == 201
    stored = client.enquiries.docs[0]
    assert "aadhaar_number" not in stored
    assert "pan" not in stored


# --------------------------------------------------------------------------- #
#  Idempotency
# --------------------------------------------------------------------------- #
def test_a_retry_returns_the_same_enquiry(client):
    """A bot that timed out and resent must not raise the vacancy twice."""
    first = post_enquiry(client)
    second = post_enquiry(client)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert first.json()["enquiry_id"] == second.json()["enquiry_id"]
    assert len(client.enquiries.docs) == 1


def test_the_same_agent_may_raise_a_second_enquiry(client):
    """Keyed per enquiry, not per sender.

    This is the one thing that differs from the candidate intake, whose key is
    the WhatsApp user. An agent raises a requirement in March and another in
    June, and both are real work.
    """
    post_enquiry(client, idempotency_key="whatsapp/111/919876543210/enq-1")
    post_enquiry(
        client,
        idempotency_key="whatsapp/111/919876543210/enq-2",
        job_title="Scaffolder",
        headcount=12,
    )

    assert len(client.enquiries.docs) == 2
    assert {d["job_title"] for d in client.enquiries.docs} == {
        "Structural Welder",
        "Scaffolder",
    }


def test_a_race_resolves_to_one_enquiry(client, enquiries):
    """The lookup cannot win the race it exists for; the unique index can.

    Two retries arriving together both read an empty collection and both try to
    insert. The second insert raises, and `record_enquiry` resolves it by
    returning the enquiry the winner created rather than propagating the error.
    """
    original_find_one = enquiries.find_one
    calls = {"n": 0}

    def blind_first_lookup(query, projection=None):
        # The first caller looks and sees nothing — which is true at the moment
        # it looks. The second one has already inserted by the time it lands.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_find_one(query, projection)

    enquiries.docs.append(
        b2b_enquiries.build_document(bot_payload(), source="whatsapp")
    )
    enquiries.find_one = blind_first_lookup

    doc, created = b2b_enquiries.record_enquiry(bot_payload(), source="whatsapp")

    assert created is False
    assert doc["id"] == enquiries.docs[0]["id"]
    assert len(enquiries.docs) == 1


# --------------------------------------------------------------------------- #
#  Matching against the Sourcing Hub
# --------------------------------------------------------------------------- #
def test_a_known_agent_is_named(client, sourcing):
    """An enquiry from a number already on file arrives carrying that name."""
    sourcing.docs.append(
        {"id": "AGT-101-2233", "name": "Ravi Manpower Services", "phone": "+91 98765 43210"}
    )

    post_enquiry(client)

    stored = client.enquiries.docs[0]
    assert stored["sourcing_client_id"] == "AGT-101-2233"
    assert stored["sourcing_client_name"] == "Ravi Manpower Services"


def test_an_unknown_sender_is_not_added_to_the_hub(client, sourcing):
    """Matching is a label, never an account.

    A number that messaged the bot is not a party the agency has agreed to work
    with. Creating one would fill the Sourcing Hub with strangers, and the
    person who has to clean that up is the one this screen was built for.
    """
    post_enquiry(client)

    assert sourcing.docs == []
    assert client.enquiries.docs[0]["sourcing_client_id"] is None


def test_a_failing_match_does_not_fail_the_intake(client, sourcing):
    """A display label is not worth losing an enquiry over."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("sourcing collection unavailable")

    sourcing.find = explode

    response = post_enquiry(client)
    assert response.status_code == 201
    assert client.enquiries.docs[0]["sourcing_client_id"] is None


# --------------------------------------------------------------------------- #
#  The recruiter's side
# --------------------------------------------------------------------------- #
def test_listing_reports_counts_over_the_whole_collection(client):
    post_enquiry(client, idempotency_key="k1")
    post_enquiry(client, idempotency_key="k2")
    client.enquiries.docs[1]["status"] = "closed"

    response = client.get("/b2b-enquiries?status=new")
    body = response.json()

    assert response.status_code == 200
    # Filtered to one row, but the tabs still say what is behind them.
    assert len(body["items"]) == 1
    assert body["counts"]["new"] == 1
    assert body["counts"]["closed"] == 1


def test_a_partial_update_does_not_blank_the_rest(client):
    """The failure a partial update written against a full model produces."""
    post_enquiry(client)
    enquiry_id = client.enquiries.docs[0]["id"]

    response = client.patch(f"/b2b-enquiries/{enquiry_id}", json={"status": "reviewing"})

    assert response.status_code == 200
    stored = client.enquiries.docs[0]
    assert stored["status"] == "reviewing"
    assert stored["requirement"].startswith("Need 40 welders")
    assert stored["headcount"] == 40


def test_moving_an_enquiry_records_who_moved_it(client):
    post_enquiry(client)
    enquiry_id = client.enquiries.docs[0]["id"]

    client.patch(f"/b2b-enquiries/{enquiry_id}", json={"status": "reviewing"})

    # From the session, not the body — an audit field a caller can set is not one.
    assert client.enquiries.docs[0]["handled_by"] == "admin@agency.test"
    assert client.enquiries.docs[0]["handled_at"] is not None


def test_converted_cannot_be_set_by_hand(client):
    """`converted` means a job order exists.

    A caller that can write the word without writing the order produces an
    enquiry that reads finished and points at nothing — a dead end on the screen
    and a job somebody raises a second time.
    """
    post_enquiry(client)
    enquiry_id = client.enquiries.docs[0]["id"]

    response = client.patch(f"/b2b-enquiries/{enquiry_id}", json={"status": "converted"})

    assert response.status_code == 422
    assert client.enquiries.docs[0]["status"] == "new"
    assert client.enquiries.docs[0]["converted_job_order_id"] is None


def test_converting_raises_a_job_order_and_stamps_the_enquiry(client):
    post_enquiry(client)
    enquiry_id = client.enquiries.docs[0]["id"]

    response = client.post(
        f"/b2b-enquiries/{enquiry_id}/convert",
        json={
            "title": "Structural Welder",
            "client": "Ravi Manpower Services",
            "headcount": 40,
            "due_date": "2026-03-01",
        },
    )

    assert response.status_code == 201
    order = response.json()["job_order"]
    assert order["headcount"] == 40
    assert order["status"] == "OPEN"
    # The order points back at the conversation that produced it.
    assert order["sourceEnquiryId"] == enquiry_id

    stored = client.enquiries.docs[0]
    assert stored["status"] == "converted"
    assert stored["converted_job_order_id"] == order["id"]
    assert len(client.job_orders.docs) == 1


def test_converting_twice_is_refused(client):
    """The second call raises a second requisition for one vacancy, and the
    recruiter who made it has no way of knowing — both orders look real."""
    post_enquiry(client)
    enquiry_id = client.enquiries.docs[0]["id"]
    payload = {"title": "Structural Welder", "client": "Ravi Manpower Services", "headcount": 40}

    first = client.post(f"/b2b-enquiries/{enquiry_id}/convert", json=payload)
    second = client.post(f"/b2b-enquiries/{enquiry_id}/convert", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409
    assert len(client.job_orders.docs) == 1


def test_converting_refuses_an_empty_headcount(client):
    """A job order for zero seats is FILLED the moment it is raised — see
    `deriveStatus` in the Job Orders screen — so it would vanish from the very
    list it was raised to appear on."""
    post_enquiry(client)
    enquiry_id = client.enquiries.docs[0]["id"]

    response = client.post(
        f"/b2b-enquiries/{enquiry_id}/convert",
        json={"title": "Welder", "client": "Ravi Manpower Services", "headcount": 0},
    )

    assert response.status_code == 422
    assert client.job_orders.docs == []


def test_converting_an_enquiry_that_does_not_exist(client):
    response = client.post(
        "/b2b-enquiries/ENQ-NOPE/convert",
        json={"title": "Welder", "client": "Someone", "headcount": 2},
    )
    assert response.status_code == 404
    assert client.job_orders.docs == []


def test_a_manual_enquiry_is_not_marked_as_the_bots(client):
    """A phone call logged by hand is already a summary; something the bot took
    is verbatim. A recruiter reads the two differently, so the record says
    which."""
    response = client.post(
        "/b2b-enquiries/manual",
        json={
            "contact_name": "Jane Doe",
            "company_name": "Gulf Steel Works",
            "requirement": "Called about 12 scaffolders.",
        },
    )

    assert response.status_code == 201
    assert client.enquiries.docs[0]["source"] == "manual"


def test_deleting_an_enquiry(client):
    post_enquiry(client)
    enquiry_id = client.enquiries.docs[0]["id"]

    assert client.delete(f"/b2b-enquiries/{enquiry_id}").status_code == 200
    assert client.enquiries.docs == []
    assert client.delete(f"/b2b-enquiries/{enquiry_id}").status_code == 404


# --------------------------------------------------------------------------- #
#  Normalisation
# --------------------------------------------------------------------------- #
def test_an_unknown_party_type_widens_to_client():
    """A party whose type this build does not know about is more usefully shown
    as a company than dropped out of every tab. The same widening the Sourcing
    Hub does, and for the same reason."""
    assert b2b_enquiries.normalise_party_type("agent") == "agent"
    assert b2b_enquiries.normalise_party_type("business") == "client"
    assert b2b_enquiries.normalise_party_type(None) == "client"


def test_skills_arrive_as_a_line_or_a_list():
    """The bot asks for skills as one line of free text; the admin form sends a
    list. Both are stored as a list."""
    from_line = b2b_enquiries.build_document(
        {"contact_name": "A", "skills": "welding, rigging , "}
    )
    from_list = b2b_enquiries.build_document(
        {"contact_name": "A", "skills": ["welding", " rigging "]}
    )
    assert from_line["skills"] == ["welding", "rigging"]
    assert from_list["skills"] == ["welding", "rigging"]


def test_every_field_is_written_on_every_insert():
    """A document whose shape depends on what the sender happened to answer is
    one every reader has to defend against."""
    sparse = b2b_enquiries.build_document({"contact_name": "A"})
    full = b2b_enquiries.build_document(bot_payload())
    assert set(sparse) == set(full)
