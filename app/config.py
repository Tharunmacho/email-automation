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

    # ---- Gmail ----
    gmail_credentials_file: str = "secrets/gmail_credentials.json"
    gmail_token_file: str = "secrets/gmail_token.json"
    gmail_query: str = "has:attachment -label:Resumes/Processed newer_than:7d"
    gmail_mark_read: bool = True
    gmail_processed_label: str = "Resumes/Processed"
    gmail_max_results: int = 25

    # ---- Anthropic Claude ----
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    anthropic_max_tokens: int = 4096



    # ---- Auto Reply ----
    auto_reply_enabled: bool = True
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
