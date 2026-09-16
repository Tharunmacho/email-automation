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
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from app.config import settings
from app.core.crm_ids import candidate_code
from app.logging_config import get_logger

log = get_logger(__name__)

#: The bot's routes. Fixed rather than configurable: `wa_bot_url` names the
#: deployment, and a path that can differ per environment is a path that gets
#: mistyped in one of them.
RELAY_PATH = "/api/staff-assignment"
SLA_RELAY_PATH = "/api/sla-breach"
CANDIDATE_CHAT_PATH = "/api/candidates"


class WhatsAppChatError(RuntimeError):
    """The bot was configured but its protected transcript API could not answer."""


def relay_enabled() -> bool:
    """Whether this deployment has a bot to talk to.

    Both halves are required. A URL with no key reaches a bot that will refuse
    it, which is a 401 on every allocation and no way to tell that from a
    misconfigured key — so an incomplete configuration is treated as no
    configuration, and the request is never made.
    """
    return bool(settings.wa_bot_url and settings.wa_bot_api_key)


def fetch_candidate_chat(wa_id: str) -> dict | None:
    """Read one candidate's WhatsApp transcript from the bot.

    ``None`` means the bot has no matching conversation. Transport and
    authentication failures raise so the CRM can distinguish an empty chat from
    a service it could not reach.
    """
    if not relay_enabled():
        raise WhatsAppChatError("WhatsApp chat service is not configured")

    normalized = "".join(character for character in str(wa_id) if character.isdigit())
    if not normalized:
        return None
    url = (
        f"{settings.wa_bot_url.rstrip('/')}{CANDIDATE_CHAT_PATH}/"
        f"{urllib.parse.quote(normalized, safe='')}"
    )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"X-Api-Key": settings.wa_bot_api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.wa_bot_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise WhatsAppChatError(f"WhatsApp bot replied with HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise WhatsAppChatError("Could not reach the WhatsApp chat service") from exc

    transcript = payload.get("transcript") if isinstance(payload, dict) else None
    if not isinstance(transcript, list):
        raise WhatsAppChatError("WhatsApp bot returned an invalid transcript")
    return {"wa_id": normalized, "sessions": transcript}


def _post(path: str, payload: Dict[str, Any], what: str, *, require_sent: bool = False) -> bool:
    """One request to the bot. Optionally verify its send result, never raises.

    Uses `urllib` rather than a client library: the payloads are a handful of
    fields and this service makes no other outbound HTTP calls.
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
                if require_sent:
                    try:
                        outcome = json.load(response)
                    except (ValueError, TypeError, AttributeError) as exc:
                        log.warning("The bot gave no valid send result for %s: %s", what, exc)
                        return False
                    if (not isinstance(outcome, dict)
                            or outcome.get("sent") is not True
                            or outcome.get("shadowed") is True):
                        reason = (outcome.get("reason", "not_sent")
                                  if isinstance(outcome, dict) else "invalid_result")
                        log.warning("The bot did not send %s: %s", what, reason)
                        return False
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


def relay_sla_breach(
    alerts: List[Dict[str, Any]],
    threshold_hours: float,
    *,
    recipient_stage: str = "manager",
    recipient_ids: List[str] | None = None,
) -> bool:
    """Tell the bot to message the admins that work has gone unattended.

    Facts, not ids, and this one is a push where the allocation relay is a pull.
    A sweep's result is not a record with an id the bot could fetch: by the time
    it asked, another sweep may have resolved half of it, and re-reading would
    report a different set than the one that actually breached.

    The bot's approved template names one candidate and one owner, so it rejects
    a digest. Send one callback for each newly breached profile instead.
    """
    if not alerts or not recipient_ids:
        return False
    if not relay_enabled():
        log.warning("SLA WhatsApp relay disabled: WA_BOT_URL or WA_BOT_API_KEY is missing")
        return False

    recipients = list(dict.fromkeys(recipient_ids))
    sent = True
    for alert in alerts:
        internal_id = alert.get("candidate_id")
        payload: Dict[str, Any] = {
            "count": 1,
            "threshold_hours": threshold_hours,
            "recipient_stage": recipient_stage,
            "super_admin_name": settings.sla_super_admin_name,
            # The bot resolves and retains only these active CRM contacts. It must
            # never infer SLA recipients from the complete staff directory.
            "recipient_ids": recipients,
            "candidate_code": alert.get("candidate_code") or (
                candidate_code(internal_id) if internal_id else None
            ),
            "candidate_name": alert.get("full_name") or alert.get("candidate_name"),
            "staff_name": alert.get("assigned_staff_name"),
            "hours_overdue": alert.get("hours_overdue"),
            # "unviewed" — never opened. "unevaluated" — opened, never judged.
            "reason": alert.get("reason"),
        }
        if not _post(
            SLA_RELAY_PATH,
            payload,
            f"an SLA alert for candidate {internal_id}",
            require_sent=True,
        ):
            sent = False
    return sent
