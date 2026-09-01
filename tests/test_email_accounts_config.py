"""Where the list of mailboxes comes from, and what happens when it is absent.

This is the configuration that decides whether mail sent to a second inbox is
ever fetched, and it fails in the worst possible way: not by raising, but by
quietly polling one mailbox and reporting success. A whole afternoon went into
finding that once. These tests are what stops it going quiet again.

Three sources, in order of precedence:

  1. ``secrets/email_accounts.json`` — the file, when a deployment has one.
  2. ``EMAIL_ACCOUNTS_JSON`` — the same array as an environment variable, for a
     host where dropping a file next to the compose project is the awkward part.
  3. the single ``IMAP_*``/``SMTP_*`` pair in ``.env`` — the fallback, and the
     one that must announce itself.
"""
from __future__ import annotations

import json

import pytest

import app.config as C

TWO_ACCOUNTS = [
    {
        "provider": "smtp_imap",
        "imap_server": "imap.gmail.com",
        "imap_username": "first@example.com",
        "imap_password": "x",
    },
    {
        "provider": "smtp_imap",
        "imap_server": "imap.hostinger.com",
        "imap_username": "second@example.com",
        "imap_password": "y",
    },
]


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """`settings` with no accounts file, and the log-dedupe reset each time."""
    monkeypatch.setitem(C.settings.__dict__, "email_accounts_file", str(tmp_path / "absent.json"))
    monkeypatch.setitem(C.settings.__dict__, "email_accounts_json", "")
    monkeypatch.setattr(C, "_account_source_reported", None)
    return C.settings


def usernames(accounts) -> list:
    return [a.get("imap_username") for a in accounts]


def test_the_file_is_read_when_it_exists(settings, tmp_path, monkeypatch):
    path = tmp_path / "email_accounts.json"
    path.write_text(json.dumps(TWO_ACCOUNTS), encoding="utf-8")
    monkeypatch.setitem(C.settings.__dict__, "email_accounts_file", str(path))
    assert usernames(settings.email_accounts) == ["first@example.com", "second@example.com"]


def test_the_env_var_is_read_when_there_is_no_file(settings, monkeypatch):
    """The deployed case: `secrets/` never arrives, the environment does."""
    monkeypatch.setitem(C.settings.__dict__, "email_accounts_json", json.dumps(TWO_ACCOUNTS))
    assert usernames(settings.email_accounts) == ["first@example.com", "second@example.com"]


@pytest.mark.parametrize("wrap", ["'{}'", '"{}"'])
def test_the_env_var_survives_being_quoted(settings, monkeypatch, wrap):
    """A hosting panel that keeps the quotes must not cost you an inbox.

    Some environment editors store the value exactly as typed, quotes and all,
    and `json.loads` on a quoted array raises. That failure looked identical to
    having configured nothing: one mailbox polled, no error, no candidate.
    """
    monkeypatch.setitem(
        C.settings.__dict__, "email_accounts_json", wrap.format(json.dumps(TWO_ACCOUNTS))
    )
    assert usernames(settings.email_accounts) == ["first@example.com", "second@example.com"]


def test_the_file_wins_over_the_env_var(settings, tmp_path, monkeypatch):
    path = tmp_path / "email_accounts.json"
    path.write_text(json.dumps([TWO_ACCOUNTS[0]]), encoding="utf-8")
    monkeypatch.setitem(C.settings.__dict__, "email_accounts_file", str(path))
    monkeypatch.setitem(C.settings.__dict__, "email_accounts_json", json.dumps(TWO_ACCOUNTS))
    assert usernames(settings.email_accounts) == ["first@example.com"]


def test_neither_source_falls_back_to_one_mailbox(settings):
    assert len(settings.email_accounts) == 1


def test_the_fallback_warns_and_names_the_mailbox_it_settled_for(settings, caplog):
    """The silence that cost the afternoon."""
    with caplog.at_level("WARNING"):
        settings.email_accounts
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "falling back to a single mailbox was silent"
    assert "falling back to the single mailbox" in warnings[0]
    assert "EMAIL_ACCOUNTS_JSON" in warnings[0], "the warning must say how to fix it"


@pytest.mark.parametrize(
    "broken, expect_in_warning",
    [("[]", "holds no accounts"), ("{not json", "could not be parsed")],
)
def test_a_broken_env_var_is_distinguished_from_an_absent_one(
    settings, monkeypatch, caplog, broken, expect_in_warning
):
    """Different mistakes, different fixes — so they must not share a message."""
    monkeypatch.setitem(C.settings.__dict__, "email_accounts_json", broken)
    with caplog.at_level("WARNING"):
        assert len(settings.email_accounts) == 1
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert warnings and expect_in_warning in warnings[0]
