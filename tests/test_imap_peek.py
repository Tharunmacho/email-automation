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
