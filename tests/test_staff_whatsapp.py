"""The relay that asks the bot to message a staff member on WhatsApp.

What these pin down is mostly what the relay *refuses* to do. It runs at the end
of an allocation that has already been written down and already been pushed to
the console, so every way it can fail has one correct answer — log it, return
False, and leave the allocation exactly as it was.
"""
from unittest.mock import patch

from app.config import settings
from app.staff_whatsapp import (
    RELAY_PATH,
    SLA_RELAY_PATH,
    relay_assignment,
    relay_enabled,
    relay_sla_breach,
)


class _Response:
    """Enough of `urlopen`'s return value to stand in for it."""

    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _configured():
    """A deployment that has been given a bot to talk to."""
    return patch.multiple(
        settings,
        wa_bot_url="https://bot.example.com/",
        wa_bot_api_key="secret",
        wa_bot_timeout_seconds=5.0,
    )


class _Feed:
    """The two calls `NotificationRepository` makes when recording and counting.

    Local rather than imported from `test_notifications`: importing that module
    from inside a test body executes it under the conftest guard, which trips
    `RealDatabaseUsedInTests` whenever this file is run on its own.
    """

    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def count_documents(self, query):
        return len([
            d for d in self.docs
            if all(d.get(key) == value for key, value in query.items())
        ])


class _NoAdmins:
    def list_admins(self, include_inactive=False):
        return []


def test_an_unconfigured_deployment_sends_nothing_at_all():
    """The default, and the one every existing deployment is in today.

    Not an error and not a warning per allocation: a CRM with no bot behind it
    is a supported way to run this, and the in-app notification is unaffected.
    """
    with patch.multiple(settings, wa_bot_url="", wa_bot_api_key=""):
        assert relay_enabled() is False
        with patch("urllib.request.urlopen") as urlopen:
            assert relay_assignment("cand-1", "staff-1") is False
        urlopen.assert_not_called()


def test_a_url_without_a_key_counts_as_unconfigured():
    """Half a configuration reaches a bot that answers 401 every time.

    Refusing here makes that a deployment mistake somebody notices, rather than
    a 401 per allocation that looks identical to a rotated key.
    """
    with patch.multiple(settings, wa_bot_url="https://bot.example.com", wa_bot_api_key=""):
        assert relay_enabled() is False
        with patch("urllib.request.urlopen") as urlopen:
            assert relay_assignment("cand-1", "staff-1") is False
        urlopen.assert_not_called()


def test_it_sends_two_ids_and_nothing_else():
    """The whole point of the design: no candidate data crosses this hop."""
    import json

    with _configured(), patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        assert relay_assignment("cand-1", "staff-7") is True

    request = urlopen.call_args.args[0]
    assert request.full_url == f"https://bot.example.com{RELAY_PATH}"
    assert request.method == "POST"
    # Header names are canonicalised by urllib, hence the capitalisation.
    assert request.get_header("X-api-key") == "secret"
    assert json.loads(request.data) == {"candidate_id": "cand-1", "staff_id": "staff-7"}


def test_a_missing_id_is_not_sent():
    """A candidate with no owner is not an allocation to announce."""
    with _configured(), patch("urllib.request.urlopen") as urlopen:
        assert relay_assignment("cand-1", "") is False
        assert relay_assignment("", "staff-1") is False
    urlopen.assert_not_called()


def test_the_bot_being_unreachable_is_not_an_exception():
    """A redeploy, a timeout, DNS. The allocation has already happened."""
    with _configured(), patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        assert relay_assignment("cand-1", "staff-1") is False


def test_the_bot_refusing_is_not_an_exception_either():
    import urllib.error

    error = urllib.error.HTTPError(
        url="https://bot.example.com", code=401, msg="Unauthorized", hdrs=None, fp=None
    )
    with _configured(), patch("urllib.request.urlopen", side_effect=error):
        assert relay_assignment("cand-1", "staff-1") is False


def test_an_allocation_asks_the_bot_to_message_the_new_owner():
    """The hook itself: the ids that were recorded are the ids that are relayed."""
    from app.db.notifications import NotificationRepository
    from app.notifications import notify_candidate_assigned

    repo = NotificationRepository(collection=_Feed())

    with patch("app.api.websocket.publish_event", return_value=True), patch(
        "app.notifications.relay_assignment", return_value=True
    ) as relay:
        notify_candidate_assigned(
            "staff-1",
            {"id": "cand-9", "full_name": "Rajesh Kumar"},
            repo=repo,
            users=_NoAdmins(),
        )

    relay.assert_called_once_with("cand-9", "staff-1")


def test_a_failed_relay_does_not_change_what_was_recorded():
    """`notified` counts people told in the console, and WhatsApp is not that.

    A staff member who got the bell entry but no message was still notified, and
    a relay that raised must not take the count — or the allocation — with it.
    """
    from app.db.notifications import NotificationRepository
    from app.notifications import notify_candidate_assigned

    repo = NotificationRepository(collection=_Feed())

    with patch("app.api.websocket.publish_event", return_value=True), patch(
        "app.notifications.relay_assignment", return_value=False
    ):
        notified = notify_candidate_assigned(
            "staff-1",
            {"id": "cand-9", "full_name": "Rajesh Kumar"},
            repo=repo,
            users=_NoAdmins(),
        )

    assert notified == 1
    assert repo.unread_count("staff-1") == 1


# --------------------------------------------------------------------------- #
#  The SLA half: telling the admins nobody has touched it
# --------------------------------------------------------------------------- #
def _breach(**overrides):
    alert = {
        "candidate_id": "cand-1",
        "full_name": "John Doe",
        "candidate_name": "John Doe",
        "assigned_staff_id": "staff-1",
        "assigned_staff_name": "Priya Sharma",
        "hours_overdue": 51.4,
        "reason": "unviewed",
    }
    alert.update(overrides)
    return alert


def test_a_sweep_that_found_nothing_sends_nothing():
    """`scan` only calls this with fresh breaches, but an empty list is still
    the shape a caller can produce, and it is not news."""
    with _configured(), patch("urllib.request.urlopen") as urlopen:
        assert relay_sla_breach([], 48) is False
    urlopen.assert_not_called()


def test_one_overdue_profile_travels_named():
    import json

    with _configured(), patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        assert relay_sla_breach([_breach()], 48) is True

    request = urlopen.call_args.args[0]
    assert request.full_url == f"https://bot.example.com{SLA_RELAY_PATH}"
    payload = json.loads(request.data)
    assert payload["count"] == 1
    assert payload["threshold_hours"] == 48
    assert payload["candidate_name"] == "John Doe"
    assert payload["staff_name"] == "Priya Sharma"
    assert payload["reason"] == "unviewed"


def test_a_backlog_travels_as_a_count_and_names_nobody():
    """Naming the first of six would read as though it were the only one."""
    import json

    alerts = [
        _breach(candidate_id="c1", assigned_staff_name="Priya Sharma"),
        _breach(candidate_id="c2", assigned_staff_name="Arun Nair"),
        _breach(candidate_id="c3", assigned_staff_name="Priya Sharma"),
    ]
    with _configured(), patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        assert relay_sla_breach(alerts, 48) is True

    payload = json.loads(urlopen.call_args.args[0].data)
    assert payload["count"] == 3
    # Two distinct people hold those three profiles, not three.
    assert payload["staff_count"] == 2
    assert "candidate_name" not in payload
    assert "staff_name" not in payload


def test_the_bot_being_down_does_not_break_the_sweep():
    with _configured(), patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        assert relay_sla_breach([_breach()], 48) is False


def test_a_breach_notification_asks_the_bot_to_tell_the_admins():
    """The hook itself, on the path `scan` actually takes."""
    from app.db.notifications import NotificationRepository
    from app.notifications import notify_sla_breaches

    alerts = [_breach()]
    with patch("app.api.websocket.publish_event", return_value=True), patch(
        "app.notifications.relay_sla_breach", return_value=True
    ) as relay:
        notify_sla_breaches(
            alerts,
            48,
            repo=NotificationRepository(collection=_Feed()),
            users=_NoAdmins(),
        )

    relay.assert_called_once_with(alerts, 48)
