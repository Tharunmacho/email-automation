"""Gmail OAuth2 flow.

First run opens a browser for consent and caches the token; subsequent runs load
and silently refresh it. We request the minimum scopes needed: read messages and
modify labels / mark-as-read.
"""
from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import settings
from app.core.exceptions import PipelineError
from app.logging_config import get_logger

log = get_logger(__name__)

# gmail.modify covers reading messages/attachments AND marking read / labelling.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def get_credentials() -> Credentials:
    import os
    
    # Populate credentials/token from environment variables if running in cloud/Docker
    gmail_creds_env = os.environ.get("GMAIL_CREDENTIALS_JSON")
    gmail_token_env = os.environ.get("GMAIL_TOKEN_JSON")
    
    if gmail_creds_env:
        settings.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        settings.credentials_path.write_text(gmail_creds_env, encoding="utf-8")
        log.info("Populated Gmail credentials file from GMAIL_CREDENTIALS_JSON env var")
        
    if gmail_token_env:
        settings.token_path.parent.mkdir(parents=True, exist_ok=True)
        settings.token_path.write_text(gmail_token_env, encoding="utf-8")
        log.info("Populated Gmail token file from GMAIL_TOKEN_JSON env var")

    token_path = settings.token_path
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        log.info("Refreshing expired Gmail token")
        try:
            creds.refresh(Request())
        except Exception as exc:
            log.warning("Failed to refresh expired Gmail token: %s; re-authorisation required", exc)
            creds = None

    if not creds or not creds.valid:
        if not settings.credentials_path.exists() and not gmail_creds_env:
            raise PipelineError(
                f"Gmail OAuth client file not found at {settings.credentials_path}. "
                "Set GMAIL_CREDENTIALS_JSON env var or place secrets/gmail_credentials.json on server."
            )
        try:
            log.info("Starting Gmail OAuth consent flow (a browser window will open)")
            flow = InstalledAppFlow.from_client_secrets_file(str(settings.credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        except Exception as exc:
            raise PipelineError(
                "Gmail OAuth token missing or invalid, and interactive browser flow failed (common on VPS/Docker). "
                "FIX: Run 'python -m app.cli auth' locally once, then pass the contents of secrets/gmail_token.json "
                "into the GMAIL_TOKEN_JSON environment variable on your VPS."
            ) from exc

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    log.info("Gmail token cached at %s", token_path)
    return creds
