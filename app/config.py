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

    # ---- Email Accounts (Multi-Inbox Configuration) ----
    # Reads from secrets/email_accounts.json if it exists.
    # Otherwise, falls back to the legacy single `.env` variables for backward compatibility.
    email_accounts_file: str = "secrets/email_accounts.json"
    
    @property
    def email_accounts(self) -> List[dict]:
        import json
        from pathlib import Path
        path = Path(self.email_accounts_file)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list) and len(data) > 0:
                    return data
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Failed to parse %s: %s", self.email_accounts_file, e)

        # Fallback to single account from .env if json doesn't exist
        return [{
            "provider": self.email_provider,
            "imap_server": self.imap_server,
            "imap_port": self.imap_port,
            "imap_username": self.imap_username,
            "imap_password": self.imap_password,
            "imap_use_ssl": self.imap_use_ssl,
            "imap_folder": self.imap_folder,
            "smtp_server": self.smtp_server,
            "smtp_port": self.smtp_port,
            "smtp_username": self.smtp_username,
            "smtp_password": self.smtp_password,
            "smtp_use_ssl": self.smtp_use_ssl,
            "smtp_use_tls": self.smtp_use_tls,
        }]

    # ---- Legacy Email Provider Choice ----
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
    # Open every document attachment and judge it on its contents, whatever it
    # is called. `01.pdf` and `Scan_2026.pdf` are what candidates actually send,
    # and a filename cannot be trusted in either direction. Costs a download and
    # a text-layer read per attachment; the page classifier then rejects
    # non-resumes before any OCR or LLM spend. Turn off to fall back to
    # `detector_min_score` alone.
    inspect_all_documents: bool = True
    # Images cannot be read without OCR, so they are screened on file size
    # rather than name — below this, it is a signature logo or an icon, not a
    # legible scanned page.
    #
    # 2 KB, not 40 KB. A phone camera shot of a CV that WhatsApp or Gmail has
    # re-compressed lands around 15-30 KB, and the old 40 KB floor threw those
    # away silently — the single largest source of "no resume-type attachment"
    # on mail that plainly carried one. Icons and signature logos are still
    # well under 2 KB, so the thing the floor exists to catch is still caught.
    min_image_attachment_bytes: int = 2_000
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
    admin_email: str = "adira@gmail.com"
    admin_password: str = "adira@2026"

    # ---- Demo Accounts & SLA ----
    # Seeded on startup and published by /auth/demo-accounts, which is
    # unauthenticated — only the admin account is advertised, and turning this
    # off silences the endpoint entirely.
    demo_accounts_enabled: bool = True
    demo_admin_email: str = "adira@gmail.com"
    demo_admin_password: str = "adira@2026"
    # How long a profile may sit allocated-but-unresolved before the sweep calls
    # it a breach, measured from `assigned_at` (falling back to `ingested_at`)
    # until it is opened or judged.
    #
    # Two days. Long enough that a profile landing on Friday afternoon is not
    # escalated over the weekend for nobody's benefit, and short enough that a
    # candidate nobody has opened is chased while they are still deciding
    # whether to answer somebody else. A tighter window turns the alert channel
    # into noise, and a muted channel reports nothing at all.
    sla_threshold_hours: int = 48
    auto_assign_enabled: bool = True

    # ---- WhatsApp bot integration ----
    # The recruitment bot's credential for POST /candidates and
    # GET /policy/cv-required, presented as `X-Service-Key`.
    #
    # Deliberately not `auth_secret` above. That one signs a session token which
    # identifies a person and expires in hours; this identifies another system
    # and lives until it is rotated. Sharing one would mean a leaked recruiter
    # session could inject candidates, and rotating the bot's credential would
    # sign every recruiter out.
    #
    # The empty default is a closed door, not an open one: `verify_service_key`
    # returns False for an unset expectation, so a deployment that forgot to set
    # this rejects every request rather than serving an unauthenticated write
    # endpoint to the internet.
    whatsapp_service_key: str = ""

    # ---- Telling the bot about an allocation ----
    # Where the recruitment bot listens, and the credential it expects on its
    # `/api/*` routes.
    #
    # A staff member is told on WhatsApp that they have been given somebody by
    # asking the bot to send it, rather than by sending it from here. The bot
    # owns the Meta credentials, the number the agency sends from, the send
    # budget and the rate limiter; a second service holding a copy of all four
    # is a second thing to rotate, a second thing to leak, and a second place
    # for the daily send count to be wrong.
    #
    # Empty disables the relay completely — the in-app notification is still
    # written and pushed, and nothing goes out over WhatsApp. That is the right
    # default for a deployment that has not been given a bot to talk to, and it
    # is why nothing here raises when it is unset.
    wa_bot_url: str = ""
    wa_bot_api_key: str = ""
    wa_bot_timeout_seconds: float = 5.0

    # Path to the CV policy table (JSON). Empty uses the built-in rules in
    # `app/policy/cv_policy.py`, which are a starting point rather than the
    # agency's real ones — pointing this at a file is how the table changes
    # without a release.
    cv_policy_path: str = ""

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
    # Quality pass, run only on the pages that hold the résumé.
    ocr_dpi: int = 300
    # How many pages go into one OCR call, and the granularity at which the
    # scan stops. The cloud OCR takes a file, so this is the only lever on how
    # long a single call can take: a 9-page 1.6 MB scan timed out at 180s as one
    # request, while the same pages in chunks answer in seconds.
    #
    # It is also the *early-stopping* granularity. At 10, a bundle whose résumé
    # sits on pages 25-26 is read as pages 1-10, 11-20, 21-30 and then stops —
    # pages 31-50 are never rendered, never uploaded, never billed. Raising this
    # buys fewer round trips at the cost of overshooting further past the CV.
    ocr_chunk_pages: int = 10
    # Hard ceiling on pages OCR'd from one scanned document, so a 200-page
    # mis-send cannot run forever. Set above the largest real bundle: the
    # resume can legitimately sit on page 25 of 50, and stopping early would
    # lose it. Truncation is always logged, never silent.
    ocr_max_pages: int = 60
    # Give up early on a scan that is plainly not an application at all: after
    # this many pages with no resume *and* no supporting document (certificate,
    # experience letter, ID) among them, there is no CV coming. Certificates do
    # NOT trip this — a CV on page 15 behind fourteen of them is the case the
    # whole page classifier exists for.
    ocr_give_up_pages: int = 4
    veris_ocr_base_url: str = "https://veris.recursai.in"
    veris_ocr_api_key: str = ""
    # A 9-page 1.6 MB scanned bundle timed out at 180s, which left the resume
    # unreadable and the mail stuck retrying. Raise this for mailboxes that get
    # large scans; it is the ceiling on one OCR call, not a per-page budget.
    veris_timeout_seconds: float = 100.0

    # ---- Async OCR jobs (POST /v1/jobs) ----
    # Queue the extraction instead of holding an HTTP connection open for it.
    # The synchronous endpoint loses the work outright when it times out; a job
    # survives the disconnect and is collected later by the reconciler. Turn
    # off to fall back to the synchronous `/v1/resume/extract` call.
    ocr_async_jobs_enabled: bool = True
    # How long the pipeline will wait for the résumé job before giving up on
    # doing it inline. The candidate record depends on this result, so the
    # budget is generous — a 60-page scan queued behind three others.
    ocr_job_wait_seconds: float = 240.0
    # How long it waits for the Aadhaar / passport jobs, which nothing
    # downstream blocks on. Short on purpose: the ingestion row already holds
    # the job id, so anything unfinished is collected by the reconciler rather
    # than holding a Gmail message open.
    identity_job_wait_seconds: float = 45.0
    # Poll backoff. Base doubles per attempt, capped, and jittered across the
    # whole interval so a batch submitted together does not come back in
    # lockstep. A `Retry-After` from the service overrides both.
    ocr_job_backoff_base_seconds: float = 1.5
    ocr_job_backoff_cap_seconds: float = 30.0
    # Before the backoff starts, poll fast and flat. Pure exponential backoff
    # meant a job that finished at 8s was not seen until ~22s — the waiting cost
    # more than the extraction. Almost every resume finishes inside this window,
    # so this interval, not the backoff curve, is what sets per-resume latency.
    # Past the window the job is a long one and polling gets out of the way.
    ocr_job_fast_poll_seconds: float = 25.0
    ocr_job_fast_poll_interval_seconds: float = 0.6
    # How many times one submission rides out a full queue (429/503) before the
    # row is failed and left to the reconciler.
    ocr_job_submit_retries: int = 4
    # Total submissions per ingestion row across all reconciler passes. On the
    # last one the row is abandoned into the operator review queue rather than
    # being retried forever.
    ocr_job_max_attempts: int = 5

    # ---- Throughput ----
    # How many extractions may be submitted-but-unfinished at Veris at once,
    # process-wide. This is the real throttle on how much work is queued at the
    # service, and it is deliberately NOT the worker-thread count: a thread
    # waiting on a job it already submitted should not be occupying a slot that
    # a résumé with nothing submitted could use. Raising it does not make Veris
    # faster; it stops us being the reason its queue is short. Watch
    # `queue_wait_ms` in GET /ingest/ocr-state — near zero means the cap is not
    # the bottleneck and raising it will not help.
    veris_max_inflight_jobs: int = 24
    # Worker threads in one batch run (`IngestionRunner`). The work is I/O-bound
    # — Gmail, Veris, the LLM — so this can sit well above the core count. It
    # bounds Gmail and Mongo concurrency; `veris_max_inflight_jobs` bounds Veris.
    # Bound to a small number (3) by default because IMAP providers (like Hostinger)
    # strictly limit simultaneous connections per IP address.
    ingestion_max_workers: int = 3

    # ---- Multipass extraction ----
    # Route Aadhaar and passport pages out of the same bundle to their own OCR
    # endpoints, instead of dropping everything that is not the résumé.
    multipass_extraction_enabled: bool = True
    # If True, sends the entire original PDF to the OCR extraction endpoints
    # instead of cropping out just the pages where the document was detected.
    # WARNING: Sending a 50MB 60-page PDF to Veris API may cause the connection to drop!
    send_full_bundle_to_ocr: bool = False
    # If True, ALWAYS prefers local Tesseract for the scanning phase to prevent cloud timeouts on huge files
    prefer_local_ocr_for_scanning: bool = True
    mongo_aadhaar_collection: str = "aadhaar_records"
    mongo_passport_collection: str = "passport_records"
    mongo_document_collection: str = "document_records"

    # ---- Passport nationality filter ----
    # The Veris passport endpoint is trained on the Indian booklet. Fed a
    # foreign passport it does not decline — it returns a confidently wrong
    # record, and a wrong passport number reaches a Gulf visa file. So the
    # issuing country is settled locally, from the text layer, and only an
    # Indian passport is uploaded. Set False to send every passport again.
    passport_india_only: bool = True
    # What to do with a passport whose country cannot be established at all —
    # a scan too poor to yield an MRZ or an emblem line. The two failure modes
    # are not symmetric: allow them and an occasional foreign passport gets
    # through, forbid them and a genuine Indian passport behind a bad scan is
    # silently lost. Defaulting to True keeps the Indian ones, which are the
    # overwhelming majority of what this mailbox receives. Set False for a
    # strict "confirmed Indian or nothing" policy.
    passport_allow_undetermined_nationality: bool = False

    # ---- Scheduled ingestion ----
    # Celery beat searches every configured mailbox at this interval and
    # fans each message out to a worker task. Set to 0 to leave ingestion
    # manual while retaining the worker for CRM-triggered syncs.
    gmail_poll_interval_seconds: int = 30

    # ---- Reconciler ----
    # A row untouched for this long is assumed stuck. Measured from the last
    # update rather than from arrival, so a healthy long-running job — which
    # the poller keeps touching — is never re-submitted underneath itself.
    reconciler_stuck_after_seconds: int = 600
    reconciler_batch_size: int = 50
    # How often celery beat runs the sweep. Well under
    # `reconciler_stuck_after_seconds`, so a row that goes quiet is picked up on
    # the first tick after it qualifies rather than a whole interval later.
    reconciler_interval_seconds: int = 120

    # How often celery beat runs the SLA sweep.
    #
    # It used to run on nothing at all: the task existed and the beat schedule
    # did not list it, so a breach was only ever found when an admin pressed
    # Scan. An alert channel that reports overdue work only to somebody already
    # looking for overdue work is not one, hence the schedule.
    #
    # Hourly, against a window measured in days. Finer would cost a sweep of the
    # collection for no earlier warning — a profile that breaches at 14:05 is
    # not more overdue at 14:10 than it is at 15:00, and the alert says how many
    # hours it has been waiting either way.
    sla_scan_interval_seconds: int = 3600
    # How long the reconciler waits on any single job it re-drives. Short: its
    # job is to move rows along, not to sit on one.
    reconciler_job_wait_seconds: float = 30.0

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
    # The fan-out poller only searches Gmail and queues one task per message,
    # so it holds the poll lock for a second or two rather than for a whole
    # batch. Sized for a slow Gmail search, not for any processing.
    poll_dispatch_lock_ttl_seconds: int = 120
    # How long one email stays claimed by whoever is processing it. Under
    # fan-out the poll lock no longer spans the work, so this is what keeps two
    # workers off the same message — and what keeps a manual sync from
    # re-processing messages a beat tick already dispatched. Must exceed the
    # worst case for a *single* message (OCR + LLM over a large scan): expiring
    # early lets a second worker start on top of a live one.
    message_lock_ttl_seconds: int = 900

    # ---- Derived helpers ----
    # File extensions the pre-filter treats as a possible resume attachment.
    # This is a *type* hint, not a gate: an attachment whose extension is absent
    # or unknown is still admitted when its MIME type or magic bytes say it is a
    # document or an image (see `detector._resume_type_attachments`). `.bin` is
    # here because Gmail and several webmail clients label a perfectly good PDF
    # that way when the sender's client omitted a Content-Type.
    resume_extensions: List[str] = Field(
        default_factory=lambda: [
            ".pdf", ".doc", ".docx", ".rtf", ".txt", ".odt", ".pages",
            ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp",
            ".heic", ".heif", ".bin",
        ]
    )
    # Sender fragments / patterns whose mail we never treat as candidate resumes.
    ignore_sender_fragments: List[str] = Field(
        default_factory=lambda: [
            "no-reply", "noreply", "donotreply", "do-not-reply",
            "mailer-daemon", "postmaster", "notifications@", "newsletter",
            "billing@", "invoice@", "receipts@", "support@", "alerts@",
            "naukri.com", "@naukri", "jobboard", "linkedin.com",
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


def get_settings() -> Settings:
    return Settings()

settings = get_settings()
