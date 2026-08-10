"""Tests for SMTPIMAPClient and email client factory."""
import email.message
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.email_client import get_email_client, GmailClient, SMTPIMAPClient
from app.email_client.smtp_imap_client import _decode_header_str, _parse_from


def test_decode_header_str():
    assert _decode_header_str("") == ""
    assert _decode_header_str("Plain Text") == "Plain Text"
    # Encoded word test
    assert "John Doe" in _decode_header_str("=?utf-8?q?John_Doe?=")


def test_parse_from():
    addr, name = _parse_from("John Doe <john@example.com>")
    assert addr == "john@example.com"
    assert name == "John Doe"

    addr2, name2 = _parse_from("jane@example.com")
    assert addr2 == "jane@example.com"
    assert name2 is None


def test_factory_selects_correct_client(monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "smtp_imap")
    client = get_email_client()
    assert isinstance(client, SMTPIMAPClient)

    monkeypatch.setattr(settings, "email_provider", "gmail")
    # Patch GmailClient __init__ to avoid trying to open credentials file
    with patch("app.email_client.factory.GmailClient") as mock_gmail:
        mock_instance = MagicMock()
        mock_gmail.return_value = mock_instance
        c2 = get_email_client()
        assert c2 == mock_instance


def test_smtp_imap_parse_mime_message():
    msg = email.message.EmailMessage()
    msg["From"] = "Applicant <applicant@example.com>"
    msg["To"] = "hr@company.com"
    msg["Subject"] = "Application for Engineer"
    msg["Date"] = "Mon, 10 Aug 2026 12:00:00 +0000"
    msg.set_content("Hello, here is my resume attached.")
    msg.add_attachment(b"%PDF-1.4 test pdf content", maintype="application", subtype="pdf", filename="resume.pdf")

    raw_bytes = msg.as_bytes()

    client = SMTPIMAPClient()
    client._fetched_bytes_cache["msg-101"] = raw_bytes

    email_msg = client.get_message("msg-101")
    assert email_msg.message_id == "msg-101"
    assert email_msg.from_addr == "applicant@example.com"
    assert email_msg.from_name == "Applicant"
    assert email_msg.subject == "Application for Engineer"
    assert "Hello, here is my resume" in email_msg.body_text
    assert len(email_msg.attachments) == 1
    assert email_msg.attachments[0].filename == "resume.pdf"
    assert email_msg.attachments[0].data == b"%PDF-1.4 test pdf content"


@patch("smtplib.SMTP_SSL")
@patch("smtplib.SMTP")
def test_smtp_send_reply(mock_smtp, mock_smtp_ssl, monkeypatch):
    monkeypatch.setattr(settings, "smtp_server", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "hr@example.com")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "smtp_use_tls", True)
    monkeypatch.setattr(settings, "smtp_use_ssl", False)

    mock_server_instance = MagicMock()
    mock_smtp.return_value = mock_server_instance

    client = SMTPIMAPClient()
    result = client.send_reply(
        message_id="msg-1",
        thread_id="thread-1",
        to_addr="candidate@example.com",
        subject="Application Received",
        body_text="Thank you for applying!",
    )

    assert result.get("status") == "sent"
    mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)
    mock_server_instance.starttls.assert_called_once()
    mock_server_instance.login.assert_called_once_with("hr@example.com", "secret")
    mock_server_instance.send_message.assert_called_once()
