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


def test_only_unread_mail_is_offered_to_the_poll():
    """Unread is the queue. A message somebody has opened is one a human has
    already dealt with, and re-ingesting it is not wanted.

    `ALL` was tried instead — it does not lose a résumé that gets read before
    the poller reaches it — and it re-offered the whole inbox, so the batch went
    on old mail that had already been handled by hand. The desk's rule is
    unread-only; this pins it so it cannot drift back by accident.
    """
    stub = _SearchingIMAP()
    _client_with(stub).search_message_ids()

    searches = [c for c in stub.commands if c[0] == "search"]
    assert searches, "no search was issued"
    criteria = [str(part).upper() for c in searches for part in c[1:]]
    assert any("UNSEEN" in part for part in criteria), criteria
    assert not any(part == "ALL" for part in criteria), (
        "read mail must not be re-offered to the poll"
    )


def test_the_newest_mail_is_returned_first():
    """Today's applicant must not queue behind the window's stale end.

    Oldest-first is fair to a backlog and wrong for this: a poll would spend
    its whole batch on old mail while a CV that arrived a minute ago waited for
    the next one. Nothing is lost to the ordering — what does not fit stays in
    the folder and is listed again, so the older end drains behind the new mail
    rather than in front of it.
    """
    stub = _SearchingIMAP(b"1 2 3 10 11")

    assert _client_with(stub).search_message_ids() == ["11", "10", "3", "2", "1"]


def test_nothing_is_capped_away_unless_a_cap_is_asked_for():
    """The ids are just numbers. How many to *work* is the caller's decision,
    made after it has dropped the ones it has already judged — capping here
    would hide messages behind non-résumé mail the poll no longer looks at."""
    stub = _SearchingIMAP(b" ".join(str(n).encode() for n in range(1, 200)))
    client = _client_with(stub)

    assert len(client.search_message_ids()) == 199
    assert client.search_message_ids(max_results=5) == ["199", "198", "197", "196", "195"]


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
    assert "UNSEEN" in args, "the window narrows the queue; it does not widen it"


def test_zero_days_means_the_whole_folder(monkeypatch):
    """The escape hatch. A one-off wide window is how an inbox gets backfilled
    deliberately, rather than by accident on every poll."""
    monkeypatch.setattr(settings, "mail_lookback_days", 0)
    stub = _SearchingIMAP()
    _client_with(stub).search_message_ids()

    # `[1:]` drops the command, `[1:]` again the charset argument IMAP takes
    # before the criteria.
    args = [str(a) for a in next(c for c in stub.commands if c[0] == "search")[2:]]
    assert args == ["UNSEEN"], "no window means unread mail of any age"
