"""Central, environment-driven configuration.

Everything the pipeline needs is declared here and loaded from environment
variables / a local ``.env`` file. Import ``settings`` anywhere; it is a cached
singleton so the ``.env`` is parsed only once per process.
"""
from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


#: The value `.env.example` ships and a container can never reach.
_LOCAL_REDIS = "redis://localhost:6379/0"


_ACCOUNT_SOURCE_LOCK = threading.Lock()
_account_source_reported: "str | None" = None


def _report_account_source(level: int, message: str) -> None:
    """Say where the mailbox list came from — once per distinct answer.

    `email_accounts` is a property and it is read on every poll, so logging
    unconditionally would put this line in the log every few seconds. Reporting
    only when the answer *changes* gives one line at startup and one more the
    moment somebody fixes the file or breaks it, which is when it is wanted.
    """
    global _account_source_reported
    with _ACCOUNT_SOURCE_LOCK:
        if message == _account_source_reported:
            return
        _account_source_reported = message
    logging.getLogger(__name__).log(level, message)


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
    # Reads from secrets/email_accounts.json if it exists, or EMAIL_ACCOUNTS_JSON env var.
    # Otherwise, falls back to the legacy single `.env` variables for backward compatibility.
    email_accounts_file: str = "secrets/email_accounts.json"
    email_accounts_json: str = ""

    @property
    def email_accounts(self) -> List[dict]:
        """Every mailbox to poll.

        The fallback to the single `.env` account used to be silent, and silence
        here is expensive: `secrets/` is in both `.gitignore` and
        `.dockerignore` and is bind-mounted over in the deployed compose file,
        so the accounts file reaches a server only if somebody puts it there by
        hand. When it is missing, the app does not fail — it quietly polls one
        mailbox, and mail sent to the other simply never arrives. The only trace
        was a page count in an unrelated log line.

        So every route through here now says which it took, and taking the
        fallback is a warning naming the file it wanted and the lone account it
        settled for.
        """
        import json
        from pathlib import Path

        path = Path(self.email_accounts_file)
        reason = f"{path} does not exist"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 — a bad file must not stop the poll
                reason = f"{path} could not be parsed ({exc})"
            else:
                if isinstance(data, list) and data:
                    _report_account_source(
                        logging.INFO,
                        f"Polling {len(data)} mailbox(es) configured in {path}",
                    )
                    return data
                # Parsed, but says nothing. Previously indistinguishable from a
                # missing file, and it is a different mistake with a different fix.
                reason = f"{path} holds no accounts"

        raw_env_json = (self.email_accounts_json or "").strip()
        if raw_env_json:
            if (raw_env_json.startswith("'") and raw_env_json.endswith("'")) or (raw_env_json.startswith('"') and raw_env_json.endswith('"')):
                raw_env_json = raw_env_json[1:-1].strip()
            try:
                data = json.loads(raw_env_json)
            except Exception as exc:  # noqa: BLE001
                reason = f"EMAIL_ACCOUNTS_JSON env var could not be parsed ({exc})"
            else:
                if isinstance(data, list) and data:
                    _report_account_source(
                        logging.INFO,
                        f"Polling {len(data)} mailbox(es) configured in EMAIL_ACCOUNTS_JSON env var",
                    )
                    return data
                reason = "EMAIL_ACCOUNTS_JSON env var holds no accounts"

        _report_account_source(
            logging.WARNING,
            f"{reason}; falling back to the single mailbox in .env "
            f"({self.imap_username or self.smtp_username or 'no account configured'}). "
            f"Any mail sent to another address will not be ingested — write "
            f"{path} or set EMAIL_ACCOUNTS_JSON in .env to poll more than one.",
        )
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

    # ---- Candidate nationality filter ----
    # Only Indian candidates are placed by this desk, so a CV belonging to
    # somebody else is refused before it reaches the Veris résumé endpoint or
    # the candidate database. See `app/extraction/resume_nationality.py`.
    resume_india_only: bool = True
    # A CV that says nothing about nationality is accepted.
    #
    # This is the setting that decides whether the filter is useful or ruinous,
    # and it ships True on evidence: most Indian CVs never write "Nationality:
    # Indian" anywhere. Demanding proof of Indian nationality would therefore
    # reject the majority of the candidates the filter exists to find, and
    # reject them invisibly, since nobody reviews what was never filed. Only
    # positive evidence of *another* country refuses a CV.
    resume_nationality_allow_undetermined: bool = True
    # What a country must score before it is named at all, and how far clear of
    # the runner-up it must be.
    #
    # 3.0 is above any pair of weak signals: an address and a phone number in
    # the same foreign country come to 2.0 and cannot refuse anybody on their
    # own. That is deliberate — an Indian driver working in Sharjah has a UAE
    # address and a +971 mobile, and he is exactly who must not be turned away.
    # A stated nationality (4.0) or a passport (6.0) clears it alone.
    resume_nationality_min_score: float = 3.0
    # And the margin, so a CV carrying evidence of two countries — a Dubai
    # employer and a home town in Kerala — is undetermined rather than a coin
    # flip. Undetermined is accepted.
    resume_nationality_margin: float = 1.5

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
    #: How many messages one poll will *work*. Not a limit on what it can see —
    #: everything still queued is reported and picked up by the next poll.
    gmail_max_results: int = 25

    #: How far back a poll looks, in days. 0 means "the whole folder".
    #:
    #: The search asks for every message in the inbox rather than only the
    #: unread ones, because a résumé somebody opened in Gmail is still a résumé
    #: nobody has processed. The cost of that is history: these mailboxes had
    #: 1,193 and 117 messages sitting in them, almost none of it recorded, and
    #: the poll set about working through all of it oldest-first — paying an OCR
    #: and a Veris parse for years-old mail while today's applicants queued
    #: behind it, and saturating a remote Mongo until GridFS writes timed out.
    #:
    #: A window keeps both halves of the rule: inside it nothing is missed
    #: whether or not it has been read; outside it, the past stays the past.
    #: `SINCE` is evaluated by the IMAP server, so the messages never travel.
    #:
    #: Widen it to take in older mail — a one-off `MAIL_LOOKBACK_DAYS=3650`
    #: sync is how you would backfill an inbox deliberately, rather than by
    #: accident on every poll.
    mail_lookback_days: int = 30

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
    # Sending happens off the ingestion path — see `app/ingestion/pipeline.py`.
    # A full SMTP conversation is a second or three of network on a good day,
    # and every one of those seconds used to be spent with the mail loop held
    # open behind it, on the one step whose failure the pipeline deliberately
    # swallows.
    #
    # Backgrounding a send is only acceptable alongside something that notices
    # when it did not happen, and these two are that: the sender retries a
    # transient failure in place, and the sweep picks up anything still owed —
    # a redeploy mid-send, an SMTP outage, a broker that was down.
    auto_reply_send_attempts: int = 3
    #: Seconds before the first retry; doubles thereafter.
    auto_reply_retry_backoff_seconds: float = 2.0
    #: How many times the *sweep* will come back to a candidate before leaving
    #: them alone. Counts across cycles, so a mistyped address costs five sends
    #: and then stops rather than one per poll for ever. Clear
    #: `auto_reply_attempts` on the record to give one another go.
    auto_reply_max_attempts: int = 5
    #: How many owed replies one sweep will send. A bound on a catch-up run
    #: after a long outage, not a budget: whatever it does not reach is picked
    #: up by the next sweep, oldest first.
    auto_reply_sweep_limit: int = 50
    #: How long a shutdown will wait for replies already queued. They are
    #: recoverable by the sweep, so this is about finishing cleanly rather than
    #: about not losing them — but a redeploy should not have to rely on that.
    auto_reply_drain_seconds: float = 20.0
    #: How recently a candidate may have been touched and still be left alone
    #: by the sweep.
    #:
    #: Draining the local pool closes the double-send window inside one process,
    #: but not across two. With more than one worker, the process that ingested
    #: a candidate holds the queued reply while a beat sweep may land on a
    #: *different* worker, drain an empty pool of its own, and find the same
    #: `auto_reply_sent=False`. Both then send, and the candidate gets two
    #: copies.
    #:
    #: A send takes seconds; this is minutes. So a record touched inside the
    #: grace period is assumed to be in flight somewhere and left for the next
    #: sweep, while one that genuinely failed has long since gone quiet and is
    #: picked up normally. Costs a delay on a failure, never a delivery.
    auto_reply_grace_seconds: int = 120
    #: How often beat sweeps for replies still owed, and how long one sweep may
    #: hold its lock. The interval is generous because the sweep is a safety
    #: net, not the delivery path — the common case is sent within seconds by
    #: the background sender, and this only picks up what that could not.
    auto_reply_sweep_interval_seconds: int = 300
    auto_reply_lock_ttl_seconds: int = 600

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
    # GridFS writes go over the same link as every other Mongo call, but they
    # are the only ones measured in megabytes, so they are the only ones that
    # need sizing. Against the production host these settle at roughly half a
    # megabyte a second; an 11 MB scanned bundle is therefore a ~20-second
    # write on an idle link and considerably longer once several run at once.
    #
    # A 255 KB chunk (GridFS's default) spends that link on per-document
    # overhead. Measured on the same 11 MB file: 255 KB → 24.6s, 1 MB → 18.6s,
    # 4 MB → 19.7s. 1 MB it is; past that there is nothing left to win.
    storage_gridfs_chunk_bytes: int = 1024 * 1024
    # How long one upload may take, as `base + size / throughput`. A flat
    # timeout cannot be right for both a 150 KB CV and an 11 MB bundle, and
    # `socketTimeoutMS` (30s) was being asked to serve as both — which is what
    # made the large ones fail with `NetworkTimeout` mid-`insert_many`. The
    # assumed floor is deliberately well under what the link actually does, so
    # the deadline is generous rather than marginal, and only a genuinely stuck
    # write reaches it.
    storage_write_base_timeout_seconds: float = 30.0
    storage_write_min_throughput_bytes: int = 128 * 1024   # 128 KB/s
    # Uploads in flight at once, across every worker in this process. Not 1:
    # measured, four concurrent 3 MB writes moved 0.90 MB/s against 0.47 MB/s
    # done one after another, so serialising would halve the throughput. Not 8
    # either: the same measurement doubled each *individual* write's latency,
    # and it is the slowest single write that decides whether anything times
    # out. Half the worker count keeps the tail bounded without giving up the
    # aggregate.
    storage_max_concurrent_writes: int = 4
    # A dropped connection mid-upload is weather, not a verdict on the file.
    storage_write_attempts: int = 3

    # ---- OCR ----
    tesseract_cmd: str = ""
    ocr_languages: str = "eng"
    ocr_min_text_chars: int = 120
    # The DPI a page is first rendered at. Pages that read poorly are re-read at
    # `ocr_escalate_dpi` — see `app/extraction/local_ocr.py`.
    # Triage: read every page cheaply first, then spend the real budget on the
    # few pages that turned out to matter.
    #
    # Every page this pipeline actually cares about is re-read in the cloud —
    # the résumé through the résumé endpoint, the Aadhaar and passport through
    # theirs. So the local read's job is not to produce good text, it is to
    # decide *which pages to send*. Measured on the two bundles from production,
    # one cheap 150-DPI pass over all 28 pages costs 23s against 78s for the
    # full-quality pass, and picks out the same résumé and Aadhaar.
    #
    # It does not, on its own, pick out the same passports — which is why this
    # is a triage and not a replacement. At 200 DPI the passport on page 27 of
    # the Saravanan bundle disappears entirely, and at 120 DPI a page that is
    # not a passport is added. Classification is not stable across resolution,
    # so triage only ever *nominates*: every page it finds interesting, and
    # every page it could not read confidently, is then re-read at `ocr_dpi`
    # and the routing decision is taken from that text.
    #
    # DEFAULT OFF, because on the documents this desk actually receives it made
    # things worse and the measurement is not close. On the two production
    # bundles it nominated 28 pages out of 28 — nothing was excluded, so the
    # cheap pass bought nothing and cost a whole extra read of every page:
    # 37s became 49s, 24s became 34s. The reason it cannot exclude anything is
    # that 17 of those 28 pages classify as `unknown` (real content, commits to
    # nothing), and an `unknown` page is precisely one nobody may skip.
    #
    # The premise was wrong too. A cheap pass looked ~6x cheaper on synthetic
    # text, where cost tracks pixel count; on real noisy scans it is only ~1.6x,
    # because Tesseract spends its time on layout analysis and speckle rather
    # than on pixels. Triage would have to eliminate more than 60% of the full
    # reads to break even, and it eliminates none.
    #
    # Left in, off, because the shape is sound for a bundle that really is
    # mostly clearly-typed pages. Turn it on only with a measurement in hand.
    # The second look: a page showing any hint of an identity document is
    # re-read carefully before the bundle is allowed to conclude it has none.
    #
    # The scores are coarse by design — a strong marker is 2.0, a weak one 0.5,
    # and a page needs 3.0 to be routed. So a page carrying a single weak trace
    # of a passport scores 0.5 and is dropped without anything ever looking at
    # it properly. That is the wrong way round: a hint is precisely the signal
    # that this page deserves *more* effort, not the signal to stop.
    #
    # So anything at or above `ocr_deep_read_score` that has not already cleared
    # the seed is rendered again at `ocr_deep_read_dpi` and put through the full
    # ladder. Bounded by `ocr_deep_read_max_pages`, because "look harder at
    # everything" is how a bundle costs an hour.
    ocr_deep_read_enabled: bool = True
    #: The lowest *scored* evidence that earns a second look. A page scoring
    #: nothing at all still gets one when `has_identity_hint` finds a bare
    #: trace on it, so this is a floor on the scored path only.
    ocr_deep_read_score: float = 0.5
    #: More pixels per character, which is what settles a marker OCR half-read.
    ocr_deep_read_dpi: int = 450
    #: A ceiling on the second look, not a budget for it.
    ocr_deep_read_max_pages: int = 12
    ocr_triage_enabled: bool = False
    ocr_triage_dpi: int = 150
    # Below this many characters a triage read is not trusted, and the page is
    # confirmed at full resolution whatever it appeared to say. Deliberately
    # generous: a page wrongly nominated costs one read, a page wrongly dropped
    # costs a passport.
    ocr_triage_min_chars: int = 200
    ocr_dpi: int = 300
    ocr_escalate_dpi: int = 450
    # Tesseract page-segmentation modes. 6 (one uniform block) is right for most
    # pages; 4 (variable-width columns) and 3 (fully automatic) rescue the
    # two-column résumés and the mixed certificate scans it gets wrong.
    # Local (adaptive) thresholding before Tesseract sees the page.
    #
    # `autocontrast` stretches one histogram over the whole sheet, which is the
    # wrong model for the documents that actually arrive: a phone photograph of
    # a CV under a desk lamp is bright at the top and grey at the bottom, and a
    # single global cut-off either loses the dark half or fills the light half
    # with speckle. Sauvola computes a threshold per neighbourhood instead, so
    # each part of the page is judged against its own background.
    #
    # Applied only where it helps — see `_prepare`. A clean 300-DPI scan is
    # already separable and pays nothing for this.
    #
    # DEFAULT OFF on the same evidence. Measured over both production bundles it
    # changed neither the extracted text (17711 and 14979 chars, to the
    # character) nor the routing, and on one of them it cost 27% more time
    # (19.9s -> 25.4s). These are flatbed scans with even lighting, which is the
    # case Tesseract's own global threshold already handles well.
    #
    # It is kept for the case it was written for and which this desk does also
    # receive: phone photographs of a document, lit from one side, where a
    # single global cut-off loses half the page. Turn it on for those, having
    # measured; do not turn it on for flatbed scans.
    ocr_adaptive_threshold: bool = False
    #: Neighbourhood, in pixels, for the local threshold. Roughly a character
    #: height at 300 DPI; smaller starts eating the insides of bold strokes.
    ocr_adaptive_window: int = 31
    ocr_psm: int = 6
    ocr_alternate_psms: List[int] = [4, 3]
    # Below this reading-quality score a page is retried — a different
    # segmentation, then a higher DPI, then a second engine. Roughly "fewer than
    # a dozen real words on the page", which for a document page means the read
    # failed rather than that the page was empty.
    ocr_page_quality_floor: float = 12.0
    # Scanned booklets — passports above all — come off the scanner on their
    # side, and a sideways page does not read badly. It reads as confident
    # nonsense that clears every quality gate, which is exactly how a real
    # passport data page went unseen. Pages are turned and re-read when
    # Tesseract's own confidence in the upright read falls below the floor.
    ocr_detect_rotation: bool = True
    # A page that reads as language upright is left alone: the résumé in the
    # bundle that prompted this yields 17 recognisable words the right way up
    # and its certificates 9, so an ordinary page costs one small probe and no
    # extra OCR. The passport pages yielded none upright — and 8 and 12 once
    # turned, which is what makes the decision safe.
    ocr_rotation_word_floor: int = 6
    # Send the visa and immigration pages of a passport booklet along with the
    # pages that identify it.
    #
    # OFF, on evidence. Those pages are found correctly — they lie between two
    # confirmed passport pages and read as nothing — but the passport endpoint
    # extracts passport *fields*, and the fields live on the data page and the
    # back page. A visa sticker adds none of them. What it does add is payload:
    # sending eighteen pages instead of three pushed one real job past the
    # `identity_job_wait_seconds` budget, so it came back "pending" and the
    # passport was not stored at all. Better data on three pages beats no data
    # on eighteen.
    #
    # Turn it on if the extractor is ever taught to read visas, and raise
    # `identity_job_wait_seconds` with it.
    passport_include_booklet_interior: bool = False
    passport_booklet_max_words: int = 4
    # Pages are independent, and `pytesseract` shells out, so this scales close
    # to linearly with cores. It is per document, not per batch.
    #
    # 0 means "size it from the host" — see `local_ocr.local_worker_count`. The
    # old flat 4 left most of a modern machine idle: a 30-page bundle took 28
    # seconds on an 8-core host. Set a positive number to pin it.
    ocr_local_workers: int = 0
    # Try RapidOCR on pages Tesseract reads badly, when it is installed. A host
    # without it is a supported configuration; this only decides whether we look.
    ocr_secondary_engine_enabled: bool = True
    # A page that will not read is not worth an unbounded wait.
    #
    # Nothing on the local path used to have a clock on it. A four-page scanned
    # CV sat in Tesseract while the inline poll — which runs inside the API
    # process whenever no Celery worker is up — stayed PENDING, and the
    # dashboard polled that task ID until someone restarted the container.
    # `ocr_page_timeout_seconds` bounds one Tesseract invocation;
    # `ocr_document_budget_seconds` bounds the whole document. Both degrade a
    # page to "unread", which every caller already handles: the text-layer read
    # stands, the page is named in the log, and the résumé still lands.
    #
    # 45s is roughly fifty times what a real read costs. Measured on a dense,
    # low-contrast page: psm 6/4/3 all answer in under a second at 300, 450 and
    # even 600 dpi. What takes minutes is not a slow page, it is Tesseract
    # failing to segment a page of scanner noise at all — so this cuts the
    # pathological case and never a legitimate one, with room for a host several
    # times slower than the one it was calibrated on.
    #
    # Set either to 0 to disable it.
    # A ceiling on Tesseract processes running at once, across the *whole*
    # process. 0 sizes it from the CPUs this container may actually use.
    #
    # Without it two independent pools multiply. `ingestion_max_workers`
    # messages are processed at once, each opening its own pool of
    # `local_worker_count()` page readers, so eight bundles on a four-worker
    # pool is thirty-two Tesseract processes — on a box with two cores. None of
    # them go faster; they simply all miss `ocr_page_timeout_seconds` together,
    # and a missed timeout returns an empty page. That is the mechanism behind
    # a bundle whose pages read at 147 and 312 characters on one poll and zero
    # on the next: the work never fit, so which pages finished was a race.
    #
    # Admission control fixes what worker counts cannot, because it is the only
    # bound that sees both pools. Pages queue instead of thrashing, each read
    # gets a real core, and a page that would have timed out simply waits.
    ocr_max_concurrent_pages: int = 0
    # A page that read as nothing is retried smaller before it is given up on.
    #
    # Downscaling is the opposite of the DPI escalation above and answers the
    # opposite failure: escalation buys pixels for a page too faint to segment,
    # while this buys *time* for a page too large to finish. A quarter of the
    # pixels is roughly a quarter of the work, and a page that reads at all is
    # worth more than a perfect read that never returns.
    ocr_rescue_enabled: bool = True
    # The wall clock ONE page may spend in the local reader, across every pass.
    #
    # `ocr_page_timeout_seconds` bounds a single Tesseract invocation, and that
    # turned out not to bound anything that matters: the ladder runs psm 6, then
    # psm 4, then psm 3, then two shrinking retries, so a page that times out at
    # every rung costs five times the per-pass limit. At 45s each that is 225
    # seconds — for one page, which then returns empty anyway. Four such pages
    # in the four concurrent slots is fifteen minutes of a 30-page bundle spent
    # producing nothing.
    #
    # The per-pass limit cannot express "stop working on this page"; only a
    # total can. When it runs out the page is unread, which is a state the
    # pipeline now handles properly — Veris is asked for it, and it comes back.
    # Trying locally for longer than this buys nothing the cloud reader will not
    # deliver faster.
    ocr_page_total_seconds: float = 90.0
    ocr_page_timeout_seconds: float = 45.0
    ocr_document_budget_seconds: float = 600.0
    # Hard ceiling on pages OCR'd from one scanned document, so a 200-page
    # mis-send cannot run forever. Set above the largest real bundle: the
    # resume can legitimately sit on page 25 of 50, and stopping early would
    # lose it. Truncation is always logged, never silent.
    # A safety ceiling on one document, not a budget to be spent carefully:
    # local OCR is CPU, not billing, so this exists only so a mis-sent
    # thousand-page scan cannot occupy a worker indefinitely. Truncation is
    # always logged with the page numbers that went unread — never silent.
    ocr_max_pages: int = 300
    # After the classifier has established which pages hold the résumé, send
    # exactly those pages to the Veris résumé endpoint for a higher-quality
    # read. This is the *only* route to that endpoint: nothing is uploaded
    # before its content has been identified locally, which is what stopped
    # invoices and job-board digests being billed as résumé extractions.
    veris_refine_resume_pages: bool = True
    # When Veris is configured, its answer is the *only* answer allowed to
    # become a candidate profile.
    #
    # The local heuristic parser exists to read a résumé on a host with no OCR
    # service at all, and it is far weaker: it produced a candidate called
    # "Work history" whose designation was "SARAVANAN.A Role" and whose
    # Projects section held a paragraph about languages. Storing that when the
    # service was merely briefly unreachable is worse than storing nothing —
    # the record looks complete, so nobody goes back to it, and the good
    # extraction is never taken.
    #
    # With this on, a failed Veris call fails the attachment instead. The email
    # is left unlabelled, so the next poll simply tries again. Turn it off only
    # for a deployment that has no Veris key and must fall back to local
    # parsing.
    require_veris_resume: bool = True
    # Above this, a payload is re-rendered as JPEG before it is uploaded. The
    # page trim removes pages but not resolution, so one sheet of a phone-camera
    # bundle can still be tens of megabytes — and that upload, not the
    # extraction, is what pushes an identity job past its wait budget. Four
    # megabytes comfortably holds a 300dpi A4 scan.
    ocr_payload_max_bytes: int = 4_000_000
    ocr_payload_dpi: int = 300
    # Pages local Tesseract could not read at all are offered to Veris rather
    # than dropped.
    #
    # Local OCR is the *gatekeeper* for identity documents: a page has to read
    # well enough here to be classified as a passport before it is allowed to
    # reach the passport endpoint. So a page that comes back empty is not merely
    # unread — it is invisible. It scores nothing, lands in `ignored_pages`, and
    # the bundle reports "no passport" rather than "a page could not be read".
    # That is how a passport on page 20 of a 28-page bundle went missing while
    # the resume and the Aadhaar on the same scan came through fine.
    #
    # Empty is not a property of the page, either. The same bundle read twice
    # fifteen minutes apart gave 147 and 312 characters on pages 16 and 17 the
    # first time and zero both the second: under CPU pressure a Tesseract pass
    # hits `ocr_page_timeout_seconds`, returns nothing, and the page silently
    # becomes blank. Which pages that hits is a race, not a fact about the file.
    #
    # So the unread pages — and only those — go to the cloud reader, after the
    # local pass, as a subset PDF. It costs one call on documents that need it
    # and nothing at all on documents that read cleanly.
    veris_recover_unread_pages: bool = True
    # Empty is not the only way a page is lost, and it is not even the common
    # one — it is just the one that was easy to see.
    #
    # When a full-size pass times out, `local_ocr` retries the page at half
    # width, and half a scan of a passport comes back as a few dozen characters
    # of speckle. That page is no longer empty, so nothing above notices it, and
    # every gate downstream is sized for text rather than for noise: under
    # `_MIN_PAGE_CHARS` the classifier does not score the page at all, and a
    # short garbled read carries none of the words `has_identity_hint` needs to
    # earn a second look. Observed exactly so — a passport read as 81 characters
    # at 1600px, filed as a nothing-page, and reported as "no passport found".
    #
    # A degraded read is therefore worse than an empty one: it fails in the same
    # way and it suppresses the recovery that an empty page would have got. So
    # the same call that fetches the unread pages fetches these too. It is the
    # same subset PDF and the same single request — sending four pages instead
    # of two costs a larger upload, not another extraction — and the local text
    # is kept wherever the cloud read does not actually beat it.
    veris_recover_degraded_pages: bool = True
    # A bound on that call, not a budget. A bundle where local OCR read almost
    # nothing is usually a broken file rather than forty recoverable pages, and
    # uploading all of it is the wrong answer to that.
    veris_recover_max_pages: int = 40
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
    #
    # Raised to 120 once "to prevent inline timeout on passport extractions",
    # and put back, because that read the wrong thing as the failure. A job that
    # comes back `pending` has not been lost — it keeps its id on the ingestion
    # row and the reconciler collects it. Measured on one batch: the passport
    # that was waited out cost 28.1s inline, and the one left to the reconciler
    # was collected in 53ms with the same fields and the same valid check
    # digits. The wait bought nothing and was three quarters of the batch.
    #
    # What the raise was really compensating for is a deployment with no Celery
    # worker, where the only sweep is `inline_reconcile_budget_seconds` on the
    # poll thread. Run the worker and this budget stops mattering at all.
    identity_job_wait_seconds: float = 45.0
    # How long the inline poll keeps sweeping for identity jobs it had to leave
    # running. Only used when there is no Celery worker — with one, beat's
    # reconciler does this and this budget is never spent. Generous, because it
    # runs on a background thread nobody is waiting on, and the alternative is a
    # passport that was successfully extracted and never stored.
    inline_reconcile_budget_seconds: float = 300.0
    # Gap between sweeps, widening as it goes. The job is already running at
    # the service; asking more often does not make it finish sooner.
    inline_reconcile_interval_seconds: float = 5.0
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
    ingestion_max_workers: int = 8
    # ---- Automatic polling ----
    # Whether the mailboxes are drained on a timer.
    #
    # Off: extraction runs when somebody presses Sync, and at no other moment.
    # That is a deliberate choice rather than a missing feature — a timer that
    # reads mailboxes and runs OCR without anyone asking spends money on the
    # extraction service, and it also puts a second poll cycle alongside a
    # manual one, which is how two runs came to submit the same résumé at once.
    #
    # The screen still updates by itself: the live push is driven by what the
    # ingestion *does*, not by what triggered it, so a manual sync fills the
    # candidate list without a page reload exactly as a timed poll would.
    #
    # Turning it on is one flag, and two places honour it: Celery beat runs
    # `poll_gmail` when a worker is up, and the API runs the same cycle
    # in-process when one is not.
    mail_autopoll_enabled: bool = False
    # Only consulted when the poll above is enabled.
    mail_poll_interval_seconds: int = 60
    # Simultaneous IMAP connections held open *per account*. Connections are
    # pooled and reused rather than opened per operation, so this is the cap on
    # concurrency against one mailbox, not a count of how many are opened over a
    # batch. Providers close the newest connection over their per-account limit
    # — Hostinger's is small — and that failure surfaces as a random mid-batch
    # timeout, so this stays comfortably underneath it while still letting the
    # two mailboxes work in parallel with each other.
    imap_max_connections: int = 4

    # ---- Multipass extraction ----
    # Route Aadhaar and passport pages out of the same bundle to their own OCR
    # endpoints, instead of dropping everything that is not the résumé.
    multipass_extraction_enabled: bool = True
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
    # `gmail_poll_interval_seconds` used to live here and is deliberately gone.
    # Nothing read it: the beat entry is built by `_mail_poll_schedule`, which
    # reads `mail_poll_interval_seconds` and only contributes anything at all
    # when `mail_autopoll_enabled` is set. A knob that is still accepted from
    # `.env` and quietly changes nothing is worse than no knob — somebody tunes
    # the poll interval, watches it have no effect, and looks everywhere except
    # at the setting they edited. `GMAIL_POLL_INTERVAL_SECONDS` in a `.env` is
    # now ignored outright (`extra="ignore"`), which is what it already was in
    # substance.

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

    @model_validator(mode="after")
    def _use_canonical_mongo_database(self) -> "Settings":
        """The production CRM has one database name: ``resume_ats``.

        A short-lived deployment used ``adira`` and split new registrations
        away from the existing CRM data. Keep accepting that stale environment
        value during rollout, but route it to the canonical database so future
        deploys cannot recreate the split.
        """
        if self.mongo_db.strip().lower() == "adira":
            object.__setattr__(self, "mongo_db", "resume_ats")
        return self

    @model_validator(mode="after")
    def _redis_url_follows_the_broker(self) -> "Settings":
        """One Redis, configured once.

        `REDIS_URL` drives the distributed locks; `CELERY_BROKER_URL` drives the
        queue. They address the same server in every deployment that has one —
        but they are separate settings with the same localhost default, so a
        deployment that pointed the broker at its real Redis and left `REDIS_URL`
        alone got a worker that connected and a lock that did not:

            Redis lock fallback: Error 111 connecting to localhost:6379

        which degrades silently to a per-process lock that cannot see the other
        containers. That is the failure this exists to prevent, and it is worth
        preventing because nothing about it is visible until two servers both
        drain the same mailbox.

        So a `REDIS_URL` that was never set adopts the broker's host instead of a
        default that is correct only on a developer's laptop. Setting it
        explicitly still wins, for the deployment that really does keep the two
        apart.
        """
        broker = (self.celery_broker_url or "").strip()
        if not broker or broker == _LOCAL_REDIS or broker == self.redis_url:
            return self

        # Two ways `REDIS_URL` ends up wrong, and the second is the common one:
        # it is not missing from the deployment's environment, it is *present
        # and still carrying the value copied from `.env.example`*. Inside a
        # container `localhost` is that container, so this exact string next to
        # a broker on a real host is never what anyone meant.
        untouched = "redis_url" not in self.model_fields_set
        if untouched or self.redis_url == _LOCAL_REDIS:
            object.__setattr__(self, "redis_url", broker)
            if not untouched:
                logging.getLogger(__name__).warning(
                    "REDIS_URL was %s while the Celery broker points elsewhere; "
                    "using the broker's Redis for locks too. Set REDIS_URL "
                    "explicitly to something other than the default to keep them "
                    "apart.", _LOCAL_REDIS,
                )
        return self


def get_settings() -> Settings:
    return Settings()

settings = get_settings()
