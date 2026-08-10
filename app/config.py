"""Central, environment-driven configuration.

Everything the pipeline needs is declared here and loaded from environment
variables / a local ``.env`` file. Import ``settings`` anywhere; it is a cached
singleton so the ``.env`` is parsed only once per process.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Application ----
    app_env: str = "development"
    log_level: str = "INFO"

    # ---- Email Provider Choice ----
    # "smtp_imap" | "gmail"
    email_provider: str = "smtp_imap"

    # ---- SMTP (Outgoing Email) ----
    smtp_server: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    # ---- IMAP (Incoming Email) ----
    imap_server: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True
    imap_folder: str = "INBOX"
    imap_processed_folder: str = "Resumes/Processed"
    imap_deleted_folder: str = "Resumes/Deleted"

    # ---- Gmail API (Legacy / Alternative) ----
    gmail_credentials_file: str = "secrets/gmail_credentials.json"
    gmail_token_file: str = "secrets/gmail_token.json"
    # No age window, and both label buckets are excluded: handled mail never
    # comes back, so the search does not need a date cutoff to stay cheap.
    gmail_query: str = "has:attachment -label:Resumes/Processed -label:Resumes/Deleted"
    gmail_mark_read: bool = True
    gmail_processed_label: str = "Resumes/Processed"
    # Retired mail: the candidate was deleted from the app, so this exact email
    # must never be re-ingested. A *new* email carrying the same resume is
    # unlabelled and ingests normally.
    gmail_deleted_label: str = "Resumes/Deleted"
    gmail_max_results: int = 25

    # ---- Anthropic Claude ----
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    anthropic_max_tokens: int = 4096



    # ---- Resume gates ----
    # Stage 1, before any download: how strong the "this is a resume email"
    # signal must be. The detector scores 0.5 for merely having a document
    # attached, so anything at or below that lets the whole mailbox through.
    detector_min_score: float = 0.7
    # Stage 2, after parsing: below this the document is not stored as a
    # candidate at all. Guards against a failed OCR falling back to the
    # heuristic parser and turning a hall ticket into a profile.
    min_ingest_confidence: float = 0.5

    # ---- Auto Reply ----
    # Off by default: replying is the one thing this app does that reaches
    # strangers, and a false positive mails someone who never applied.
    auto_reply_enabled: bool = False
    auto_reply_signature: str = "Best regards,\nRecruitment Team"

    # ---- Auth ----
    # Signs session tokens. MUST be set in production: leaving the default
    # means anyone who reads this file can mint a valid admin token.
    auth_secret: str = "dev-only-change-me"
    auth_token_ttl_hours: int = 12
    # Seed account, created once on startup if it does not already exist.
    admin_email: str = "admin@gmail.com"
    admin_password: str = "admin@123"

    # ---- MongoDB ----
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "resume_ats"
    mongo_candidates_collection: str = "candidates"

    # ---- Storage ----
    storage_backend: str = "gridfs"      # gridfs | local
    storage_local_dir: str = "data/resumes"
    storage_gridfs_bucket: str = "resumes"

    # ---- OCR ----
    tesseract_cmd: str = ""
    ocr_languages: str = "eng"
    ocr_min_text_chars: int = 120
    veris_ocr_base_url: str = "https://veris.recursai.in"
    veris_ocr_api_key: str = ""

    # ---- Celery ----
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ---- Redis (Celery broker + distributed locks) ----
    redis_url: str = "redis://localhost:6379/0"
    # Kept short on purpose: every probe of "is a worker up?" pays this when
    # Redis is down, and it runs inside a request. On Windows a `localhost`
    # connect tries IPv6 then IPv4, so the real wait is roughly double this.
    # A broker that cannot accept a TCP connection within a second is down as
    # far as an interactive request is concerned.
    redis_socket_timeout: float = 1.0
    # A poll cycle holding the lock longer than this is assumed dead. Must
    # exceed a realistic worst-case batch (OCR + LLM over a full inbox page),
    # because expiring early lets a second cycle start on top of a live one.
    poll_lock_ttl_seconds: int = 1800

    # ---- Derived helpers ----
    # File extensions the pre-filter treats as a possible resume attachment.
    resume_extensions: List[str] = Field(
        default_factory=lambda: [
            ".pdf", ".doc", ".docx", ".rtf", ".txt",
            ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp",
        ]
    )
    # Sender fragments / patterns whose mail we never treat as candidate resumes.
    ignore_sender_fragments: List[str] = Field(
        default_factory=lambda: [
            "no-reply", "noreply", "donotreply", "do-not-reply",
            "mailer-daemon", "postmaster", "notifications@", "newsletter",
            "billing@", "invoice@", "receipts@", "support@", "alerts@",
            "infosys", "springboard", "coursera", "udemy", "skillshare",
        ]
    )

    @property
    def credentials_path(self) -> Path:
        return Path(self.gmail_credentials_file)

    @property
    def token_path(self) -> Path:
        return Path(self.gmail_token_file)

    @property
    def storage_dir(self) -> Path:
        return Path(self.storage_local_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
