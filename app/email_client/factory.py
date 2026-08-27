"""Factory module for selecting the configured email client (SMTP/IMAP or Gmail API)."""
from __future__ import annotations

from typing import Any, List

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient
from app.gmail.client import GmailClient
from app.logging_config import get_logger

log = get_logger(__name__)


def get_email_client(config: dict | None = None) -> Any:
    """Return an email client instance configured by settings or the provided config."""
    if config:
        provider = config.get("provider", "smtp_imap").lower().strip()
    else:
        provider = (settings.email_provider or "smtp_imap").lower().strip()
        
    if provider == "gmail":
        log.debug("Instantiating GmailClient (Google API OAuth)")
        return GmailClient()
    
    log.debug("Instantiating SMTPIMAPClient (Email & Password)")
    return SMTPIMAPClient(config=config)


def get_all_email_clients() -> List[Any]:
    """Return a list of all configured email clients based on email_accounts."""
    accounts = settings.email_accounts
    if not accounts:
        return [get_email_client()]
    
    clients = []
    for account in accounts:
        clients.append(get_email_client(config=account))
    return clients
