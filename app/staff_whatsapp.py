"""Asking the WhatsApp bot to send something the CRM cannot send itself.

Two things travel this way: an allocation, which tells a staff member a
candidate is now theirs, and an SLA breach, which tells the admins that one of
them has not touched it since.

The CRM knows *that* both happened. It does not know how to reach a phone, and
deliberately does not learn: the Meta credentials, the number the agency sends
from, the send budget and the rate limiter all live in the bot, and a copy of
them here would be a second thing to rotate and a second place for the day's
send count to be wrong.

So these send facts, and the bot composes the wording. That is what keeps the
message text in one repository — changing what a message says is not a
coordinated release across two services.

Best-effort by construction, and the ordering at both call sites is what makes
that safe: the durable in-app notification is already written by the time either
of these runs. A message that cannot be sent costs a pop-up somebody already has
in the bell. Nothing here raises.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: The bot's routes. Fixed rather than configurable: `wa_bot_url` names the
#: deployment, and a path that can differ per environment is a path that gets
#: mistyped in one of them.
RELAY_PATH = "/api/staff-assignment"
SLA_RELAY_PATH = "/api/sla-breach"


def relay_enabled() -> bool:
    """Whether this deployment has a bot to talk to.

    Both halves are required. A URL with no key reaches a bot that will refuse
    it, which is a 401 on every allocation and no way to tell that from a
    misconfigured key — so an incomplete configuration is treated as no
    configuration, and the request is never made.
    """
    return bool(settings.wa_bot_url and settings.wa_bot_api_key)


def _post(path: str, payload: Dict[str, Any], what: str) -> bool:
    """One request to the bot. Returns whether it accepted, never raises.

    Uses `urllib` rather than a client library on purpose: the payloads are a
    handful of fields, the responses are discarded, and this service makes no
    other outbound HTTP calls to justify the dependency.
    """
    if not relay_enabled():
        return False

    url = f"{settings.wa_bot_url.rstrip('/')}{path}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": settings.wa_bot_api_key,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=settings.wa_bot_timeout_seconds) as response:
            if 200 <= response.status < 300:
                log.info("Asked the bot to send %s", what)
                return True
            log.warning("The bot refused %s: HTTP %s", what, response.status)
            return False
    except urllib.error.HTTPError as exc:
        # Read the body: the bot answers with a reason, and "HTTP 400" on its
        # own sends whoever reads this log to the wrong service.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        log.warning("The bot refused %s: HTTP %s %s", what, exc.code, detail)
        return False
    except Exception as exc:  # noqa: BLE001
        # A timeout, DNS, a bot mid-redeploy. What it was announcing still
        # happened and is still recorded.
        log.warning("Could not reach the bot to send %s: %s", what, exc)
        return False


def relay_assignment(candidate_id: str, staff_id: str) -> bool:
    """Tell the bot to message the staff member a candidate now belongs to.

    Two ids and nothing else. The bot reads the candidate and the staff member
    back out of this API, so no candidate data crosses the hop.
    """
    if not candidate_id or not staff_id:
        return False
    return _post(
        RELAY_PATH,
        {"candidate_id": candidate_id, "staff_id": staff_id},
        f"the assignment of candidate {candidate_id} to staff {staff_id}",
    )


def relay_sla_breach(alerts: List[Dict[str, Any]], threshold_hours: float) -> bool:
    """Tell the bot to message the admins that work has gone unattended.

    Facts, not ids, and this one is a push where the allocation relay is a pull.
    A sweep's result is not a record with an id the bot could fetch: by the time
    it asked, another sweep may have resolved half of it, and re-reading would
    report a different set than the one that actually breached.

    One call per sweep rather than one per profile. The first sweep after this
    ships will find every historic breach at once, and a message each would be
    both a bill and a channel nobody reads afterwards.
    """
    if not alerts:
        return False

    first = alerts[0]
    staff_names = {a.get("assigned_staff_name") for a in alerts if a.get("assigned_staff_name")}

    payload: Dict[str, Any] = {
        "count": len(alerts),
        "threshold_hours": threshold_hours,
        "staff_count": len(staff_names),
    }

    # The single-breach case is the one worth naming. A digest that named the
    # first of six would read as though it were the only one.
    if len(alerts) == 1:
        payload.update({
            "candidate_id": first.get("candidate_id"),
            "candidate_name": first.get("full_name") or first.get("candidate_name"),
            "staff_name": first.get("assigned_staff_name"),
            "hours_overdue": first.get("hours_overdue"),
            # "unviewed" — never opened. "unevaluated" — opened, never judged.
            "reason": first.get("reason"),
        })

    return _post(SLA_RELAY_PATH, payload, f"an SLA alert covering {len(alerts)} profile(s)")
