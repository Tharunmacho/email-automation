"""The Settings mailbox list contains automation accounts and no secrets."""

import json

from app.api import routes
from app.api.routes import _public_email_accounts


def test_public_email_accounts_only_returns_automation_mailbox_facts():
    accounts = [
        {
            "provider": "smtp_imap",
            "imap_username": "cv@adiragroups.com",
            "imap_password": "first-secret",
            "smtp_username": "cv@adiragroups.com",
            "smtp_password": "second-secret",
            "imap_server": "mail.example.com",
        },
        {
            "provider": "smtp_imap",
            "imap_username": "jobs@adiragroups.com",
            "imap_password": "third-secret",
        },
    ]

    result = _public_email_accounts(accounts)

    assert result == [
        {
            "address": "cv@adiragroups.com",
            "provider": "smtp_imap",
            "configured": True,
        },
        {
            "address": "jobs@adiragroups.com",
            "provider": "smtp_imap",
            "configured": True,
        },
    ]
    assert "password" not in repr(result).lower()
    assert "secret" not in repr(result).lower()


def test_public_email_accounts_deduplicates_and_skips_blank_addresses():
    result = _public_email_accounts(
        [
            {"imap_username": "CV@adiragroups.com", "imap_password": "x"},
            {"smtp_username": "cv@ADIRAGROUPS.com", "smtp_password": "x"},
            {"imap_username": "", "imap_password": "x"},
        ]
    )

    assert [account["address"] for account in result] == ["CV@adiragroups.com"]


def test_ingest_rules_exposes_all_configured_mailboxes_without_credentials(monkeypatch, tmp_path):
    accounts = [
        {"provider": "smtp_imap", "imap_username": "cv@adiragroups.com", "imap_password": "secret-one"},
        {"provider": "smtp_imap", "imap_username": "jobs@adiragroups.com", "imap_password": "secret-two"},
    ]
    monkeypatch.setitem(routes.settings.__dict__, "email_accounts_file", str(tmp_path / "absent.json"))
    monkeypatch.setitem(routes.settings.__dict__, "email_accounts_json", json.dumps(accounts))

    response = routes.ingest_rules({"id": "admin-1", "role": "admin"})

    assert [entry["address"] for entry in response["mailbox"]["accounts"]] == [
        "cv@adiragroups.com",
        "jobs@adiragroups.com",
    ]
    assert response["mailbox"]["account"] == "cv@adiragroups.com"
    assert "secret-one" not in repr(response)
    assert "secret-two" not in repr(response)
