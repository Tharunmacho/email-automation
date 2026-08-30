"""Factory module for selecting the configured email client (SMTP/IMAP or Gmail API)."""
from __future__ import annotations

import hashlib
import threading
from typing import Any, List

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient
from app.gmail.client import GmailClient
from app.logging_config import get_logger

log = get_logger(__name__)


# One client per account, kept for the life of the process.
#
# A client owns a pool of logged-in IMAP connections, and that pool exists
# precisely to avoid paying a TCP handshake, a TLS handshake and a LOGIN before
# every fetch. Building a fresh client per poll threw the pool away each time
# and handed the saving straight back: a manual sync spent around 26 seconds on
# connection setup before it had looked at a single message, and every poll paid
# it again from cold.
#
# Keyed on the account's own connection details, so editing a mailbox in
# `email_accounts.json` or `.env` produces a new client rather than quietly
# reusing one still logged in as the old user.
_clients: dict[tuple, Any] = {}
_clients_lock = threading.Lock()


def _client_key(provider: str, config: dict | None) -> tuple:
    source = config or {}

    def field(name: str, fallback: Any) -> Any:
        return source.get(name, fallback) if config else fallback

    return (
        provider,
        field("imap_server", settings.imap_server),
        field("imap_port", settings.imap_port),
        field("imap_username", settings.imap_username),
        field("imap_folder", settings.imap_folder),
        # Hashed, not stored: this key ends up in a module-level dict, and a
        # mailbox password has no business sitting in one.
        hashlib.sha256(str(field("imap_password", settings.imap_password)).encode()).hexdigest(),
    )


def get_email_client(config: dict | None = None) -> Any:
    """The client for one account, reused across polls.

    Reused rather than rebuilt so its connection pool survives; see `_clients`.
    Callers get a shared object, so anything stateful on a client has to be safe
    to keep — the fetched-message cache is bounded for exactly that reason.
    """
    if config:
        provider = config.get("provider", "smtp_imap").lower().strip()
    else:
        provider = (settings.email_provider or "smtp_imap").lower().strip()

    key = _client_key(provider, config)
    with _clients_lock:
        client = _clients.get(key)
        if client is not None:
            return client

        if provider == "gmail":
            log.debug("Instantiating GmailClient (Google API OAuth)")
            client = GmailClient()
        else:
            log.debug("Instantiating SMTPIMAPClient (Email & Password)")
            client = SMTPIMAPClient(config=config)

        _clients[key] = client
        return client


def reset_email_clients() -> None:
    """Drop every cached client. For tests, and for a credentials change."""
    with _clients_lock:
        _clients.clear()


def get_all_email_clients() -> List[Any]:
    """Return a list of all configured email clients based on email_accounts."""
    accounts = settings.email_accounts
    if not accounts:
        return [get_email_client()]

    clients = []
    for account in accounts:
        clients.append(get_email_client(config=account))
    return clients
