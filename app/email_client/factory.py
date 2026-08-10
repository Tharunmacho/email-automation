"""Factory module for selecting the configured email client (SMTP/IMAP or Gmail API)."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.email_client.smtp_imap_client import SMTPIMAPClient
from app.gmail.client import GmailClient
from app.logging_config import get_logger

log = get_logger(__name__)


def get_email_client() -> Any:
    """Return an email client instance configured by settings.email_provider."""
    provider = (settings.email_provider or "smtp_imap").lower().strip()
    if provider == "gmail":
        log.debug("Instantiating GmailClient (Google API OAuth)")
        return GmailClient()
    
    log.debug("Instantiating SMTPIMAPClient (Email & Password)")
    return SMTPIMAPClient()
