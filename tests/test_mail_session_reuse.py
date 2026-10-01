"""One login per account per process, not one per email.

Zoho counts every IMAP LOGIN and SMTP login as a session, caps them per
account, and blocks IMAP access when they pile up. These pin the three things
that keep the count flat: a quiet connection is kept alive rather than dropped
and re-opened, an SMTP session is reused across sends, and a stopping process
logs out instead of leaving sessions on the server's books.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.email_client import factory
from app.email_client.smtp_imap_client import SMTPIMAPClient, _ImapPool


class FakeImap:
    def __init__(self):
        self.noops = 0
        self.logged_out = False

    def noop(self):
        self.noops += 1
        return "OK", [b"NOOP completed"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]

    def select(self, folder):
        return "OK", [b"1"]


def _client(connect):
    client = SMTPIMAPClient({
        "imap_server": "imap.example.com", "imap_port": 993,
        "imap_username": "hr@example.com", "imap_password": "x",
        "smtp_server": "smtp.example.com", "smtp_port": 465,
        "smtp_username": "hr@example.com", "smtp_password": "x",
        "smtp_use_ssl": True, "smtp_use_tls": False,
    })
    client._connect_imap = connect
    return client


def test_a_second_email_reuses_the_first_login():
    opened = []

    def connect():
        conn = FakeImap()
        opened.append(conn)
        return conn

    client = _client(connect)
    for _ in range(5):
        with client._imap() as mail:
            mail.select("INBOX")

    assert len(opened) == 1


def test_keepalive_noops_an_idle_connection_instead_of_dropping_it(monkeypatch):
    conn = FakeImap()
    pool = _ImapPool(2)
    pool.give_back(pool.borrow(lambda: conn))
    before = conn.noops

    pool.keepalive(idle_after=0.0)

    assert conn.noops == before + 1
    assert pool.size == 1
    assert not conn.logged_out


def test_keepalive_logs_out_a_connection_the_server_has_dropped():
    dead = FakeImap()
    dead.noop = MagicMock(side_effect=OSError("connection reset"))
    pool = _ImapPool(2)
    pool.give_back(pool.borrow(lambda: dead))

    pool.keepalive(idle_after=0.0)

    assert dead.logged_out
    assert pool.size == 0


def test_closing_the_client_logs_every_pooled_session_out():
    conns = []

    def connect():
        conn = FakeImap()
        conns.append(conn)
        return conn

    client = _client(connect)
    with client._imap():
        pass
    client.close()

    assert conns and all(c.logged_out for c in conns)
    # A connection handed back after close is logged out too, not re-pooled.
    pool = client._pool
    late = FakeImap()
    pool.give_back(pool.borrow(lambda: late))
    assert late.logged_out


@patch("smtplib.SMTP_SSL")
def test_a_run_of_replies_shares_one_smtp_login(mock_smtp_ssl):
    server = MagicMock()
    server.noop.return_value = (250, b"OK")
    mock_smtp_ssl.return_value = server
    client = _client(FakeImap)

    for n in range(4):
        client.send_reply(f"m{n}", f"t{n}", "a@example.com", "Hi", "Thanks")

    assert mock_smtp_ssl.call_count == 1
    assert server.login.call_count == 1
    assert server.send_message.call_count == 4


@patch("smtplib.SMTP_SSL")
def test_a_dead_smtp_session_is_replaced_not_reused(mock_smtp_ssl):
    first, second = MagicMock(), MagicMock()
    first.noop.side_effect = OSError("gone")
    second.noop.return_value = (250, b"OK")
    mock_smtp_ssl.side_effect = [first, second]
    client = _client(FakeImap)

    client.send_reply("m1", "t1", "a@example.com", "Hi", "Thanks")
    client.send_reply("m2", "t2", "a@example.com", "Hi", "Thanks")

    assert mock_smtp_ssl.call_count == 2
    assert second.send_message.call_count == 1


@patch("smtplib.SMTP_SSL")
def test_an_idle_smtp_session_is_quit_by_upkeep(mock_smtp_ssl, monkeypatch):
    server = MagicMock()
    server.noop.return_value = (250, b"OK")
    mock_smtp_ssl.return_value = server
    client = _client(FakeImap)
    client.send_reply("m1", "t1", "a@example.com", "Hi", "Thanks")

    monkeypatch.setattr(settings, "smtp_idle_close_seconds", 0)
    client.maintain_sessions()

    server.quit.assert_called_once()
    assert client._smtp_conn is None


def test_closing_the_factory_logs_out_every_cached_client(monkeypatch):
    factory.reset_email_clients()
    closed = []

    class Stub:
        def close(self):
            closed.append(self)

    monkeypatch.setattr(settings, "email_provider", "smtp_imap")
    monkeypatch.setattr(factory, "SMTPIMAPClient", lambda config=None: Stub())
    a = factory.get_email_client()
    assert factory.get_email_client() is a

    factory.close_email_clients()

    assert closed == [a]
    assert factory.get_email_client() is not a
    factory.reset_email_clients()
