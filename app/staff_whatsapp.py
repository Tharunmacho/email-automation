"""Ask the WhatsApp bot to tell a staff member they have been given somebody.

The CRM knows *that* an allocation happened and *who* it went to. It does not
know how to reach a WhatsApp number, and deliberately does not learn: the Meta
credentials, the number the agency sends from, the send budget and the rate
limiter all live in the bot, and putting a copy of them here would mean two
services to rotate and two places for the day's send count to be wrong.

So the request carries two ids and nothing else. The bot reads the candidate and
the staff member back out of this API and composes the message itself, which is
what keeps the wording out of the CRM — a change to what the message says is a
change in one repository, not a coordinated release across two.

Best-effort by construction, and the ordering in `notify_candidate_assigned`
is what makes that safe: the durable in-app notification is already written by
the time this runs, so a WhatsApp message that cannot be sent costs the staff
member a pop-up they already have in the bell. Nothing here raises.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: The bot's route. Fixed rather than configurable: `wa_bot_url` names the
#: deployment, and a path that can differ per environment is a path that gets
#: mistyped in one of them.
RELAY_PATH = "/api/staff-assignment"


def relay_enabled() -> bool:
    """Whether this deployment has a bot to talk to.

    Both halves are required. A URL with no key reaches a bot that will refuse
    it, which is a 401 on every allocation and no way to tell that from a
    misconfigured key — so an incomplete configuration is treated as no
    configuration and logged once, at the call site, rather than per request.
    """
    return bool(settings.wa_bot_url and settings.wa_bot_api_key)


def relay_assignment(candidate_id: str, staff_id: str) -> bool:
    """Tell the bot to message the staff member. Returns whether it accepted.

    Uses `urllib` rather than a client library on purpose: the payload is two
    strings and the response is discarded, so the dependency would buy nothing
    and this service does not otherwise make outbound HTTP calls.
    """
    if not relay_enabled():
        return False
    if not candidate_id or not staff_id:
        return False

    url = f"{settings.wa_bot_url.rstrip('/')}{RELAY_PATH}"
    body = json.dumps({"candidate_id": candidate_id, "staff_id": staff_id}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": settings.wa_bot_api_key,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=settings.wa_bot_timeout_seconds) as response:
            if 200 <= response.status < 300:
                log.info(
                    "Asked the bot to notify staff %s about candidate %s",
                    staff_id, candidate_id,
                )
                return True
            log.warning(
                "The bot refused the staff notification for candidate %s: HTTP %s",
                candidate_id, response.status,
            )
            return False
    except urllib.error.HTTPError as exc:
        # Read the body: the bot answers with a reason, and "HTTP 400" on its
        # own sends whoever reads this log to the wrong service.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        log.warning(
            "The bot refused the staff notification for candidate %s: HTTP %s %s",
            candidate_id, exc.code, detail,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        # A timeout, DNS, a bot that is being redeployed. The allocation stands.
        log.warning(
            "Could not reach the bot to notify staff about candidate %s: %s",
            candidate_id, exc,
        )
        return False
