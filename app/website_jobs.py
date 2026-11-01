"""Job orders as the public website's Current Jobs page shows them.

adiragroups.com reads `GET /public/jobs` and draws one card per item: picture,
category tag, job type, title, two lines of description, "city, country",
salary, and an Apply button. The items come straight from the `job_orders`
collection on every request, so the page is always what the CRM holds: an order
appears while it is open and is gone as soon as it is closed or deleted.

Publishing is opt-in. An order is listed only when its `website.show` is true,
which the Job Orders form sets for a new order unless the recruiter unticks it.
Orders saved before the website block existed, and orders converted from B2B
enquiries, have no such flag and stay off the site until someone ticks it — so
nothing goes public that no one chose to put there.

Each item has the same shape as the website's own `jobListings` documents, so
the page can show both side by side with one fetch added.

What goes public is deliberately narrow. The client's name, the remarks (sent
to agents, not the public), age limits and the candidates mapped to the order
never reach the website;
the card is built from the role, the destination, the salary, the experience
asked for, the order's poster (`posterUrl`, required when an order is created,
because agents get it on WhatsApp too), and the order's `website` block, which a
recruiter fills in for the purpose (city, category, job type, public
description, and a switch to keep an order off the site).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from app.config import settings

#: The website's job type options, in the order its admin offers them.
JOB_TYPES = ("Full time", "Contract", "Part time", "Remote")

#: Values the CRM puts in an order when the recruiter left a field empty. They
#: read as placeholders on a public card, so they publish as blank.
_PLACEHOLDERS = {"", "any", "general", "not disclosed", "n/a", "na", "-"}

#: Only orders matching this are ever read for the public page.
PUBLISHED_QUERY = {"status": {"$ne": "CLOSED"}, "website.show": True}


def website_block(order: dict) -> dict:
    block = order.get("website")
    return block if isinstance(block, dict) else {}


def poster_url(order: dict) -> str:
    """The order's poster as stored, or "" when it has none.

    Kept on the order itself (`posterUrl`) since the poster also goes to agents
    on WhatsApp. Orders saved before that kept it in `website.posterUrl`, which
    is still read.
    """
    return _text(order.get("posterUrl")) or _text(website_block(order).get("posterUrl"))


def is_published(order: dict) -> bool:
    """Whether the order's card belongs on the website right now."""
    if website_block(order).get("show") is not True:
        return False
    return str(order.get("status") or "OPEN").upper() != "CLOSED"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _public(value: Any) -> str:
    text = _text(value)
    return "" if text.lower() in _PLACEHOLDERS else text


def resolve_url(url: str, base_url: str) -> str:
    """An absolute address for a picture, as the website must have one.

    Posters uploaded here are stored as API paths (`/public/job-posters/…`) so
    that a change of API domain does not mean editing every order. Anything
    already absolute is used as given.
    """
    url = _text(url)
    if not url or re.match(r"^https?://", url, re.IGNORECASE):
        return url
    if url.startswith("/") and base_url:
        return base_url.rstrip("/") + url
    return url


def _fallback_description(order: dict) -> str:
    """A line for the card when the recruiter wrote no public description.

    Built only from fields that are already public. The order's own
    `description` is never used: it is the Remarks agents get with the order.
    """
    parts: list[str] = []
    headcount = order.get("headcount")
    if isinstance(headcount, int) and not isinstance(headcount, bool) and headcount > 0:
        parts.append(f"{headcount} {'vacancy' if headcount == 1 else 'vacancies'}")
    experience = _public(order.get("minExperience"))
    if experience:
        parts.append(f"Experience: {experience}")
    skills = [s for s in (_public(skill) for skill in order.get("skills") or []) if s]
    if skills:
        parts.append("Skills: " + ", ".join(skills[:6]))
    return " · ".join(parts)


def _posted(order: dict) -> str:
    """"Oct 31, 2026" — when the order was first saved, as the card shows dates."""
    oid = order.get("_id")
    stamp = getattr(oid, "generation_time", None)
    if not isinstance(stamp, datetime):
        return ""
    return stamp.astimezone(timezone.utc).strftime("%b %d, %Y")


def listing_for(order: dict, base_url: str = "") -> dict:
    """The website card for `order`, in the website's `jobListings` schema.

    Every text field is a string, never null: the website's search box calls
    `.toLowerCase()` on title, industry, city and country, and one missing
    value would break filtering for every job on the page.
    """
    web = website_block(order)
    job_type = _text(web.get("jobType"))
    return {
        "id": _text(order.get("id")),
        "title": _text(order.get("title")) or _text(order.get("designation")),
        "city": _text(web.get("city")),
        "country": _text(order.get("destinationCountry")),
        # The coloured tag on the card. The CRM's industry list is broad
        # ("Oil & Gas"); a recruiter can name the trade instead ("Piping").
        "industry": _text(web.get("category")) or _public(order.get("industry")),
        "type": job_type if job_type in JOB_TYPES else JOB_TYPES[0],
        "experience": _public(order.get("minExperience")),
        "date": _posted(order),
        "desc": _text(web.get("description")) or _fallback_description(order),
        "salary": _public(order.get("salary")),
        "image": resolve_url(poster_url(order), base_url) or settings.website_jobs_default_image,
        # The website's page keeps only items with `visible` set and, for
        # another language, `allLanguages`; both are true for everything here.
        "visible": True,
        "allLanguages": True,
        "source": "crm",
    }


def public_jobs(orders: Iterable[dict], base_url: str = "") -> list[dict]:
    """The cards for every order that belongs on the website, as given."""
    return [listing_for(order, base_url) for order in orders if is_published(order)]
