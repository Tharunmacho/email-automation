"""Job orders on the website's Current Jobs page, read live through GET /public/jobs.

The website renders whatever this endpoint returns, so these tests assert on the
items themselves: which orders appear, and what of them is made public.
"""
from __future__ import annotations

import io

import mongomock
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.config import settings
from app.website_jobs import listing_for, public_jobs

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
POSTER = "/public/job-posters/" + "a" * 32 + ".jpg"


def order(**overrides):
    base = {
        "id": "ORD-101-2026",
        "title": "Production Supervisor",
        "client": "Secret Client Co",
        "destinationCountry": "Saudi Arabia",
        "headcount": 2,
        "salary": "SAR 3000-4000",
        "skills": ["Piping", "Fabrication"],
        "description": "INTERNAL: client pays 20% fee, do not share",
        "status": "OPEN",
        "minExperience": "5-8 Years",
        "minAge": 25,
        "maxAge": 45,
        "industry": "Oil & Gas",
        "submittedCandidateIds": ["cand-1"],
        # Agent terms: for agents only, never on the website.
        "terms": {"b2bRate": "4.9 with Ticket", "advancePayment": "EUR 946", "candidatePays": "VFS"},
        "website": {
            "show": True,
            "city": "Jeddah",
            "category": "Piping",
            "jobType": "Full time",
            "description": "Supervise piping fabrication and erection.",
            "posterUrl": POSTER,
        },
    }
    base.update(overrides)
    return base


BASE = "https://api.example.com"


# --------------------------------------------------------------------------- #
#  The card
# --------------------------------------------------------------------------- #
def test_card_carries_the_public_fields_in_the_website_schema():
    card = listing_for({**order(), "_id": ObjectId("6723d6000000000000000000")}, BASE)
    assert card == {
        "id": "ORD-101-2026",
        "title": "Production Supervisor",
        "city": "Jeddah",
        "country": "Saudi Arabia",
        "industry": "Piping",
        "type": "Full time",
        "experience": "5-8 Years",
        "date": "Oct 31, 2024",
        "desc": "Supervise piping fabrication and erection.",
        "salary": "SAR 3000-4000",
        "image": f"{BASE}{POSTER}",
        "visible": True,
        "allLanguages": True,
        "source": "crm",
    }


def test_card_never_carries_the_client_remarks_ages_or_candidates():
    published = " ".join(str(v) for v in listing_for(order(), BASE).values())
    assert "Secret Client" not in published
    assert "INTERNAL" not in published
    assert "45" not in published
    assert "cand-1" not in published
    assert "4.9" not in published
    assert "946" not in published
    assert "VFS" not in published


@pytest.mark.parametrize("status, show, listed", [
    ("OPEN", True, True),
    ("IN PROGRESS", True, True),
    ("FILLED", True, True),
    ("CLOSED", True, False),
    ("OPEN", False, False),
    # An order nobody chose to publish — saved before the website block
    # existed, or converted from a B2B enquiry — stays off the site.
    ("OPEN", None, False),
])
def test_only_open_orders_someone_published_are_listed(status, show, listed):
    website = {"show": show} if show is not None else {}
    assert bool(public_jobs([order(status=status, website=website)])) is listed


def test_order_without_a_website_block_is_not_listed():
    assert public_jobs([order(website=None)]) == []


def test_order_without_website_details_still_makes_a_complete_card():
    card = listing_for(order(website=None, salary="Not disclosed", minExperience="Any", industry="General"))
    assert card["image"] == settings.website_jobs_default_image
    assert card["type"] == "Full time"
    assert card["salary"] == ""
    assert card["experience"] == ""
    assert card["industry"] == ""
    assert card["city"] == ""
    assert card["date"] == ""
    assert card["desc"] == "2 vacancies · Skills: Piping, Fabrication"
    # Every text field is a string: the website calls .toLowerCase() on them.
    assert all(isinstance(card[k], str) for k in ("title", "city", "country", "industry"))


def test_absolute_poster_urls_are_used_as_given():
    card = listing_for(order(website={"posterUrl": "https://cdn.example.com/p.jpg"}), BASE)
    assert card["image"] == "https://cdn.example.com/p.jpg"


# --------------------------------------------------------------------------- #
#  Through the API
# --------------------------------------------------------------------------- #
@pytest.fixture
def api(monkeypatch, tmp_path):
    from app.api import routes
    from app.db import mongo
    from app.storage.local import LocalStorageBackend

    db = mongomock.MongoClient(tz_aware=True)["crm"]
    monkeypatch.setattr(mongo, "get_db", lambda: db)
    storage = LocalStorageBackend(tmp_path)
    monkeypatch.setattr(routes, "get_storage_backend", lambda *a, **k: storage)
    monkeypatch.setattr(settings, "public_api_url", "")

    def sign_in():
        routes.app.dependency_overrides[routes.current_user] = lambda: {
            "id": "admin-1", "email": "admin@x.com", "name": "Admin", "role": "admin",
            "pages": ["job-orders"], "actions": [],
        }

    def sign_out():
        routes.app.dependency_overrides.pop(routes.current_user, None)

    client = TestClient(routes.app)
    client.db = db  # type: ignore[attr-defined]
    client.sign_in = sign_in  # type: ignore[attr-defined]
    client.sign_out = sign_out  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        sign_out()


def test_public_jobs_follow_the_job_orders_as_they_change(api):
    api.sign_in()
    api.post("/job-orders", json=order(id="ORD-1", title="Welder"))
    api.post("/job-orders", json=order(id="ORD-2", title="Production Supervisor"))
    api.post("/job-orders", json=order(id="ORD-3", title="Waiter", website={"show": False}))
    api.sign_out()  # the website has no session

    first = api.get("/public/jobs", headers={"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"})
    assert first.status_code == 200
    assert "max-age=60" in first.headers["cache-control"]
    # Newest first; an order kept off the website is not there.
    assert [job["id"] for job in first.json()["items"]] == ["ORD-2", "ORD-1"]
    assert first.json()["items"][0]["image"] == f"https://api.example.com{POSTER}"

    api.sign_in()
    api.put("/job-orders/ORD-2", json=order(id="ORD-2", title="Production Supervisor", status="CLOSED"))
    api.delete("/job-orders/ORD-1")
    api.sign_out()
    assert api.get("/public/jobs").json() == {"items": [], "count": 0}

    api.sign_in()
    api.put("/job-orders/ORD-2", json=order(id="ORD-2", title="Production Supervisor", status="OPEN"))
    api.sign_out()
    assert [job["title"] for job in api.get("/public/jobs").json()["items"]] == ["Production Supervisor"]


def test_public_api_url_wins_over_the_request(api, monkeypatch):
    monkeypatch.setattr(settings, "public_api_url", "https://crm-api.adiragroups.com/")
    api.db["job_orders"].insert_one(order())
    image = api.get("/public/jobs").json()["items"][0]["image"]
    assert image == f"https://crm-api.adiragroups.com{POSTER}"


def test_public_jobs_never_return_internal_fields(api):
    api.db["job_orders"].insert_one(order())
    item = api.get("/public/jobs").json()["items"][0]
    assert not {"client", "description", "minAge", "maxAge", "submittedCandidateIds", "terms", "_id"} & set(item)


def test_poster_upload_round_trips_through_the_public_route(api):
    api.sign_in()
    response = api.post("/job-orders/posters", files={"file": ("poster.png", io.BytesIO(PNG), "image/png")})
    assert response.status_code == 201
    url = response.json()["url"]
    assert url.startswith("/public/job-posters/") and url.endswith(".png")

    api.sign_out()
    poster = api.get(url)
    assert poster.status_code == 200
    assert poster.content == PNG
    assert poster.headers["content-type"] == "image/png"
    assert "immutable" in poster.headers["cache-control"]


def test_poster_upload_needs_a_session_and_an_image(api):
    files = {"file": ("poster.png", io.BytesIO(PNG), "image/png")}
    assert api.post("/job-orders/posters", files=files).status_code in (401, 403, 404)
    api.sign_in()
    response = api.post("/job-orders/posters", files={"file": ("cv.pdf", io.BytesIO(b"%PDF-1.4 x"), "image/png")})
    assert response.status_code == 422


@pytest.mark.parametrize("name", ["..%2Fresumes%2Fcv.pdf", "abc.jpg", "a" * 32 + ".pdf", "A" * 32 + ".jpg"])
def test_public_poster_route_serves_only_generated_names(api, name):
    assert api.get(f"/public/job-posters/{name}").status_code == 404
