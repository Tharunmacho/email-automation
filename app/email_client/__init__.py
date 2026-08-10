"""Email client package for resume ingestion and reply sending."""
from app.email_client.factory import get_email_client
from app.email_client.smtp_imap_client import SMTPIMAPClient
from app.gmail.client import GmailClient

__all__ = ["get_email_client", "SMTPIMAPClient", "GmailClient"]
