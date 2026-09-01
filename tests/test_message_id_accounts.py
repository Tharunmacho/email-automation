"""A message id has to say which mailbox it came from.

An IMAP UID means something only inside one account: every mailbox numbers its
own slots from 1, so `611` on cv@adiragroups.com and `611` on adira.saudi@
gmail.com are unrelated mail. The ledger, the deletion tombstones, the
candidate's `source_email.message_id` and the Redis per-message claim all key on
that one string as if it were global — so a CV on one account could be dropped
as "already settled", or suppressed as belonging to a deleted candidate, because
of a message on the other. Nothing logged it; from the poller's side the message
simply was not in the answer.
"""
from __future__ import annotations

import contextlib

import pytest

from app.core.message_ids import account_of, is_qualified, local_id_of, qualify
from app.email_client.smtp_imap_client import SMTPIMAPClient


ACCOUNT_A = {"imap_username": "cv@adiragroups.com", "imap_server": "imap.hostinger.com"}


# --------------------------------------------------------------------------- #
#  The identifier itself
# --------------------------------------------------------------------------- #
def test_the_account_travels_with_the_uid():
    assert qualify("cv@adiragroups.com", "611") == "cv@adiragroups.com:611"


def test_the_same_number_on_two_accounts_is_two_different_ids():
    """The whole bug in one assertion."""
    assert qualify("cv@adiragroups.com", "611") != qualify("adira.saudi@gmail.com", "611")


def test_both_halves_come_back_out():
    mid = qualify("cv@adiragroups.com", "611")

    assert account_of(mid) == "cv@adiragroups.com"
    assert local_id_of(mid) == "611"


def test_a_windows_credentials_path_survives_being_an_account():
    r"""Split from the right, because `C:\secrets\creds.json` has a colon in it
    and the account is the part that may contain one."""
    mid = qualify(r"C:\secrets\gmail_credentials.json", "1a04ff")

    assert account_of(mid) == r"C:\secrets\gmail_credentials.json"
    assert local_id_of(mid) == "1a04ff"


def test_a_legacy_id_still_names_a_real_uid():
    """Rows and queued tasks written before ids carried an account must not be
    misread — `611` is still UID 611, it just no longer says whose."""
    assert not is_qualified("611")
    assert local_id_of("611") == "611"
    assert account_of("611") == ""


def test_a_client_that_cannot_say_who_it_is_produces_the_old_shape():
    """Better the previous behaviour than a confidently wrong prefix."""
    assert qualify("", "611") == "611"


# --------------------------------------------------------------------------- #
#  What the client hands out, and what it sends back to the server
# --------------------------------------------------------------------------- #
def test_search_results_carry_the_account(monkeypatch):
    client = SMTPIMAPClient(config=ACCOUNT_A)
    monkeypatch.setattr(type(client), "_imap", _fake_imap([b"609 610 611"]))

    found = client.search_message_ids()

    assert found == [
        "cv@adiragroups.com:611",
        "cv@adiragroups.com:610",
        "cv@adiragroups.com:609",
    ]


def test_the_server_is_still_told_a_bare_uid():
    """Qualification is for us. IMAP has never heard of it."""
    client = SMTPIMAPClient(config=ACCOUNT_A)

    assert client._uid("cv@adiragroups.com:611") == "611"


def test_acting_on_another_accounts_message_is_refused():
    """`611` exists on this server too, and it is somebody else's mail. Reading
    or flagging it is the exact damage the qualification exists to prevent, so
    it is raised rather than quietly done to the wrong message."""
    client = SMTPIMAPClient(config=ACCOUNT_A)

    with pytest.raises(ValueError, match="belongs to"):
        client._uid("adira.saudi@gmail.com:611")


def test_a_legacy_bare_id_is_still_accepted_by_the_client():
    """A Celery task queued before this change still names a UID on its own
    server; refusing it would strand the message, not protect it."""
    client = SMTPIMAPClient(config=ACCOUNT_A)

    assert client._uid("611") == "611"


# --------------------------------------------------------------------------- #
#  Filing a delete, which asks every account on purpose
# --------------------------------------------------------------------------- #
def test_a_foreign_id_yields_no_uid_hint_instead_of_raising():
    """The same résumé is normally delivered to both mailboxes and ingested
    from one, so a delete asks each in turn and "not mine" is ordinary."""
    client = SMTPIMAPClient(config=ACCOUNT_A)

    assert client._uid_hint("adira.saudi@gmail.com:611") == ""


def test_the_owning_account_still_gets_its_hint():
    client = SMTPIMAPClient(config=ACCOUNT_A)

    assert client._uid_hint("cv@adiragroups.com:611") == "611"


# --------------------------------------------------------------------------- #
#  The ingestion row's account, which is what the row's key is *for*
# --------------------------------------------------------------------------- #
def test_the_ingestion_row_takes_its_account_from_the_message():
    """`mailbox_account_id()` answers from global settings, so with two
    mailboxes it returned the same account for both — and stamped Gmail UIDs
    onto rows labelled `cv@adiragroups.com`."""
    from app.ingestion.job_recorder import IngestionStateRecorder

    recorder = IngestionStateRecorder("adira.saudi@gmail.com:2613", "2613_5")

    assert recorder.account_id == "adira.saudi@gmail.com"


def test_a_legacy_message_falls_back_to_the_configured_mailbox(monkeypatch):
    from app.ingestion.job_recorder import IngestionStateRecorder

    monkeypatch.setattr(
        "app.ingestion.multipass.mailbox_account_id", lambda: "cv@adiragroups.com"
    )
    recorder = IngestionStateRecorder("2613", "2613_5")

    assert recorder.account_id == "cv@adiragroups.com"


# --------------------------------------------------------------------------- #
#  The bug, at the level it actually bit
# --------------------------------------------------------------------------- #
def test_one_accounts_verdict_does_not_settle_the_other_accounts_mail(monkeypatch):
    """The reported failure, reproduced.

    Two mailboxes are polled together and both hand the runner a UID 611. The
    ledger has judged one of them — an old non-résumé, or an email whose
    candidate was deleted. Pooled into one list of bare numbers, that verdict
    silently swallowed the *other* account's message too: a CV that had just
    arrived was dropped as "already settled", never fetched, never logged.
    """
    from unittest.mock import MagicMock

    from app.ingestion.pipeline import ProcessResult
    from app.ingestion.runner import IngestionRunner

    settled = "cv@adiragroups.com:611"
    fresh = "adira.saudi@gmail.com:611"

    class _Ledger:
        def seen_message_ids(self, ids):
            return {m for m in ids if m == settled}

    class _Pipeline:
        def __init__(self):
            self.ledger = _Ledger()
            self.handled = []

        def process_email(self, email, gmail=None):
            self.handled.append(email)
            return ProcessResult(email, "skipped", "stubbed")

    def _client(account, uid):
        c = MagicMock()
        c.imap_username = account
        c.search_message_ids.return_value = [uid]
        c.get_message.side_effect = lambda mid: mid
        return c

    monkeypatch.setattr("app.ingestion.runner.settings.gmail_max_results", 25)
    monkeypatch.setattr("app.ingestion.runner.claim_message", _always_claimed)

    pipeline = _Pipeline()
    runner = IngestionRunner(
        clients=[
            _client("cv@adiragroups.com", settled),
            _client("adira.saudi@gmail.com", fresh),
        ],
        pipeline=pipeline,
    )
    summary = runner.run_once()

    assert pipeline.handled == [fresh], (
        "the fresh CV was swallowed by the other account's verdict"
    )
    assert summary.fetched == 1


# --------------------------------------------------------------------------- #
def _fake_imap(search_results):
    """An `_imap()` context manager whose UID SEARCH answers with fixed data."""
    class _Mail:
        def select(self, _folder):
            return ("OK", [b"1"])

        def uid(self, command, *args):
            if command == "search":
                return ("OK", search_results)
            raise AssertionError(f"unexpected IMAP command {command!r}")

    class _Ctx:
        def __enter__(self):
            return _Mail()

        def __exit__(self, *_exc):
            return False

    return lambda _self: _Ctx()


@contextlib.contextmanager
def _always_claimed(_message_id):
    yield True
