r"""Reading a message must not consume it.

`search_message_ids` only ever asks IMAP for UNSEEN, and `get_message` used a
plain `(RFC822)` fetch — which sets \Seen as a side effect. So merely *looking*
at an email removed it from every future poll: an email the detector ignored, or
one that failed mid-parse, could never be reconsidered. That is how a real
candidate's resume was fetched once, skipped, and then vanished from the queue.

Marking a message read is a decision the runner makes after a successful
ingest — never something reading it does by accident.
"""
from __future__ import annotations

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient

RAW = (
    b"From: Applicant <applicant@example.com>\r\n"
    b"Subject: Resume\r\n"
    b"Message-ID: <abc@example.com>\r\n"
    b"Content-Type: text/plain\r\n\r\n"
    b"Please find my CV attached.\r\n"
)


class _RecordingIMAP:
    """Minimal IMAP stub that records the commands it is given."""

    def __init__(self):
        self.commands: list[tuple] = []

    def select(self, _folder):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "fetch":
            return ("OK", [(b"1 (BODY[] {10})", RAW)])
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


def _client_with(stub) -> SMTPIMAPClient:
    client = SMTPIMAPClient()
    client._connect_imap = lambda: stub
    return client


def test_fetching_a_message_uses_peek_and_never_sets_seen():
    stub = _RecordingIMAP()
    email = _client_with(stub).get_message("400")

    assert email.subject == "Resume"

    fetches = [c for c in stub.commands if c[0] == "fetch"]
    assert fetches, "no fetch was issued"
    for _cmd, _uid, spec in fetches:
        assert "PEEK" in spec.upper(), f"fetch spec {spec!r} would mark the mail read"
        assert "RFC822" not in spec.upper()

    stores = [c for c in stub.commands if c[0] == "store"]
    assert stores == [], f"reading a message must not change flags, got {stores}"


def test_an_ignored_message_is_still_unseen_on_the_next_poll():
    """The whole point: a skipped email stays in the UNSEEN queue."""
    stub = _RecordingIMAP()
    client = _client_with(stub)

    client.get_message("400")
    client._fetched_bytes_cache.clear()      # a later poll, fresh process
    client.get_message("400")

    assert [c for c in stub.commands if c[0] == "store"] == []


# --------------------------------------------------------------------------- #
#  A message somebody has read is still work
# --------------------------------------------------------------------------- #
class _SearchingIMAP(_RecordingIMAP):
    """Answers a search with a fixed UID list, oldest first."""

    def __init__(self, uids=b"3 1 2 10"):
        super().__init__()
        self.uids = uids

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "search":
            return ("OK", [self.uids])
        return super().uid(command, *args)


def test_the_search_asks_for_every_message_in_the_folder():
    """Asking for UNSEEN is what silently lost résumés.

    The folder is the queue — an ingested message is moved to
    `Resumes/Processed` and a retired one to `Resumes/Deleted` — so whatever is
    still in the inbox is work. While the search asked for UNSEEN, anyone
    opening a CV in Gmail before the poller reached it removed that résumé from
    every future poll, with nothing logged anywhere.
    """
    stub = _SearchingIMAP()
    _client_with(stub).search_message_ids()

    searches = [c for c in stub.commands if c[0] == "search"]
    assert searches, "no search was issued"
    criteria = [str(part).upper() for c in searches for part in c[1:]]
    # The invariant is the *absence* of UNSEEN, not the presence of any one
    # keyword: a date window narrows which messages are listed, and read state
    # must never be what decides.
    assert not any("UNSEEN" in part for part in criteria), (
        "a message that has been read is still an unprocessed résumé"
    )


def test_the_backlog_is_returned_oldest_first():
    """A busy inbox must drain in arrival order.

    Newest-first starves the oldest applicants exactly when their SLA clock has
    run longest — and if mail arrives faster than a batch is worked, it starves
    them for ever.
    """
    stub = _SearchingIMAP(b"1 2 3 10 11")

    assert _client_with(stub).search_message_ids() == ["1", "2", "3", "10", "11"]


def test_nothing_is_capped_away_unless_a_cap_is_asked_for():
    """The ids are just numbers. How many to *work* is the caller's decision,
    made after it has dropped the ones it has already judged — capping here
    would hide messages behind non-résumé mail the poll no longer looks at."""
    stub = _SearchingIMAP(b" ".join(str(n).encode() for n in range(1, 200)))
    client = _client_with(stub)

    assert len(client.search_message_ids()) == 199
    assert client.search_message_ids(max_results=5) == ["1", "2", "3", "4", "5"]


# --------------------------------------------------------------------------- #
#  Recent mail, read or unread — but not the whole archive
# --------------------------------------------------------------------------- #
def test_the_search_is_bounded_by_the_lookback_window(monkeypatch):
    """`ALL` without a window meant the entire inbox history.

    These mailboxes held 1,304 messages between them, almost none of it
    recorded, and the poll set about working through all of it oldest-first —
    an OCR and a Veris parse for years-old mail while today's applicants queued
    behind it. `SINCE` is evaluated by the server, so the old mail is never
    listed and never travels. The same window measured 34 messages.
    """
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "mail_lookback_days", 30)
    stub = _SearchingIMAP()
    _client_with(stub).search_message_ids()

    search = next(c for c in stub.commands if c[0] == "search")
    args = [str(a) for a in search[1:]]
    assert "SINCE" in args, args

    expected = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%d-%b-%Y")
    assert expected in args, f"expected {expected} in {args}"
    assert "UNSEEN" not in args, "inside the window, read mail is still work"


def test_zero_days_means_the_whole_folder(monkeypatch):
    """The escape hatch. A one-off wide window is how an inbox gets backfilled
    deliberately, rather than by accident on every poll."""
    monkeypatch.setattr(settings, "mail_lookback_days", 0)
    stub = _SearchingIMAP()
    _client_with(stub).search_message_ids()

    args = [str(a) for a in next(c for c in stub.commands if c[0] == "search")[1:]]
    assert "ALL" in args and "SINCE" not in args
