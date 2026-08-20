"""Multipass extraction: one bundle, three endpoints, three independent jobs.

A candidate sends one PDF. Inside it are a CV on pages 52-53, an Aadhaar card on
page 54, a passport data page on page 55 and fifty-six certificates. Until now
everything but the CV was thrown away — the classifier found the résumé pages,
the rest were ignored, and the Aadhaar number a recruiter needs for a Gulf visa
file was retyped by hand off a downloaded scan.

This module routes each document to the endpoint that can actually read it:

    pages 52-53  ──▶  POST /v1/jobs  mode=resume    ──▶  candidates
    page  54     ──▶  POST /v1/jobs  mode=aadhaar   ──▶  aadhaar_records
    page  55     ──▶  POST /v1/jobs  mode=passport  ──▶  passport_records
    everything else                                 ──▶  ignored, never uploaded

Each pass is its own row in the ingestion state machine, keyed on
``(provider, account, message, attachment, mode)``, so a passport that fails
retries on its own without touching the Aadhaar that succeeded — and a
redelivery of the same email re-attaches to the same jobs rather than paying for
all three again.

The résumé pass is deliberately *not* here. It stays inside `IngestionPipeline`,
where dedup, the confidence gate and the auto-reply hang off it; this module
owns the two identity passes, which nothing downstream blocks on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.config import settings
from app.db import identity_records
from app.db.ingestion_state import (
    IngestionRow,
    IngestionStateStore,
    MODE_AADHAAR,
    MODE_PASSPORT,
    PROVIDER_EMAIL,
)
from app.extraction import page_classifier as pc
from app.extraction import pdf_pages
from app.extraction.jobs import AsyncOCRJobClient, OCRJobError
from app.logging_config import get_logger

log = get_logger(__name__)

#: The passes this module owns. Résumé extraction is the pipeline's own.
IDENTITY_MODES = (MODE_AADHAAR, MODE_PASSPORT)

_STORE_BY_MODE = {
    MODE_AADHAAR: identity_records.store_aadhaar_record,
    MODE_PASSPORT: identity_records.store_passport_record,
}


def mailbox_account_id() -> str:
    """Which mailbox this deployment is reading.

    Part of the ingestion row's natural key, so two mailboxes polled by the same
    deployment cannot collide on a message id — Gmail's ids are unique per
    account, not globally.
    """
    provider = (settings.email_provider or "smtp_imap").lower().strip()
    if provider == "gmail":
        return settings.gmail_credentials_file or "gmail"
    return settings.imap_username or settings.smtp_username or "default"


@dataclass
class PassResult:
    """What became of one mode's pass over one attachment."""

    mode: str
    pages: List[int]
    status: str                       # succeeded | pending | failed | abandoned | skipped
    row_id: str = ""
    job_id: Optional[str] = None
    record_id: Optional[str] = None
    detail: str = ""


@dataclass
class MultipassResult:
    """Every identity pass run over one attachment, plus what was ignored."""

    classification: Optional[pc.MultipassClassification] = None
    passes: List[PassResult] = field(default_factory=list)
    ignored_pages: List[int] = field(default_factory=list)

    @property
    def succeeded(self) -> List[PassResult]:
        return [p for p in self.passes if p.status == "succeeded"]

    @property
    def pending(self) -> List[PassResult]:
        return [p for p in self.passes if p.status == "pending"]

    def summary(self) -> str:
        if not self.passes:
            return "no identity documents found"
        return "; ".join(
            f"{p.mode} p{','.join(str(n) for n in p.pages)}={p.status}" for p in self.passes
        )


class MultipassExtractor:
    """Runs the Aadhaar and passport passes over one attachment.

    Every collaborator is injectable because the interesting behaviour here —
    a job that is still running when the budget expires, a 429 on submission, a
    row that has already succeeded — has to be testable without a network or a
    database.
    """

    def __init__(
        self,
        state: Optional[IngestionStateStore] = None,
        client: Optional[AsyncOCRJobClient] = None,
        provider: str = PROVIDER_EMAIL,
        account_id: Optional[str] = None,
    ):
        self._state = state
        self._client = client
        self.provider = provider
        self._account_id = account_id

    @property
    def state(self) -> IngestionStateStore:
        if self._state is None:
            self._state = IngestionStateStore()
        return self._state

    @property
    def client(self) -> AsyncOCRJobClient:
        if self._client is None:
            self._client = AsyncOCRJobClient()
        return self._client

    @property
    def account_id(self) -> str:
        if self._account_id is None:
            self._account_id = mailbox_account_id()
        return self._account_id

    # ------------------------------------------------------------------ #
    def run(
        self,
        page_texts: Sequence[str],
        data: bytes,
        *,
        message_id: str,
        attachment_id: str,
        filename: str,
        sha256: str = "",
        storage_key: str = "",
        candidate_id: Optional[str] = None,
        classification: Optional[pc.MultipassClassification] = None,
    ) -> MultipassResult:
        """Find the identity documents in this bundle and extract them.

        Never raises. An identity document is supporting evidence: failing to
        read the passport must not cost the candidate the résumé that was
        successfully ingested alongside it.
        """
        try:
            classification = classification or pc.classify_multipass(page_texts)
        except Exception as exc:  # noqa: BLE001
            log.warning("Multipass classification failed for %s: %s", filename, exc)
            return MultipassResult()

        result = MultipassResult(
            classification=classification,
            ignored_pages=list(classification.ignored_pages),
        )
        found = {
            MODE_AADHAAR: classification.aadhaar_pages,
            MODE_PASSPORT: classification.passport_pages,
        }
        if not any(found.values()):
            log.debug("No Aadhaar or passport pages in %s", filename)
            return result

        for mode in IDENTITY_MODES:
            pages = found.get(mode) or []
            if not pages:
                continue
            try:
                result.passes.append(
                    self.run_one(
                        mode,
                        pages,
                        data,
                        message_id=message_id,
                        attachment_id=attachment_id,
                        filename=filename,
                        sha256=sha256,
                        storage_key=storage_key,
                        candidate_id=candidate_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one pass never kills the other
                log.exception("Multipass %s extraction failed for %s", mode, filename)
                result.passes.append(
                    PassResult(mode=mode, pages=pages, status="failed", detail=str(exc))
                )

        log.info("Multipass over %s: %s", filename, result.summary())
        return result

    # ------------------------------------------------------------------ #
    def run_one(
        self,
        mode: str,
        pages: List[int],
        data: bytes,
        *,
        message_id: str,
        attachment_id: str,
        filename: str,
        sha256: str = "",
        storage_key: str = "",
        candidate_id: Optional[str] = None,
    ) -> PassResult:
        """One mode, one attachment: register, submit, wait, store."""
        row = self.state.open_row(
            self.provider,
            self.account_id,
            message_id,
            attachment_id,
            mode,
            sha256=sha256,
            storage_key=storage_key,
            filename=filename,
            pages=pages,
            candidate_id=candidate_id,
        )

        # Already done on an earlier delivery of this mail. Re-running it would
        # re-bill the extraction to overwrite an identical record.
        if row.status == "succeeded":
            return PassResult(
                mode=mode, pages=pages, status="succeeded", row_id=row.id,
                job_id=row.ocr_job_id, record_id=row.result_id,
                detail="already extracted",
            )
        if row.status == "abandoned":
            return PassResult(
                mode=mode, pages=pages, status="abandoned", row_id=row.id,
                detail=row.last_error or "awaiting operator review",
            )
        if candidate_id and not row.candidate_id:
            self.state.set_candidate(row.id, candidate_id)
            row.candidate_id = candidate_id

        if not self.state.claim_for_submit(row.id, settings.ocr_job_max_attempts):
            # Another worker owns it, or it has spent its attempts. Either way
            # this call is not the one that drives it.
            current = self.state.get(row.id) or row
            return PassResult(
                mode=mode, pages=pages, status="pending", row_id=row.id,
                job_id=current.ocr_job_id,
                detail=f"not claimed (status={current.status}, attempts={current.attempts})",
            )

        payload, payload_name = self._payload_for(data, pages, filename, mode)
        try:
            handle = self.client.submit(payload, payload_name, mode, row.idempotency_key)
        except OCRJobError as exc:
            status = self.state.mark_failed(row.id, str(exc), settings.ocr_job_max_attempts)
            return PassResult(
                mode=mode, pages=pages,
                status="abandoned" if status == "abandoned" else "failed",
                row_id=row.id, detail=str(exc),
            )

        self.state.mark_submitted(row.id, handle.job_id)
        outcome = self.client.wait(
            handle.job_id, mode, settings.identity_job_wait_seconds
        )
        return self.complete(row, outcome, pages=pages)

    # ------------------------------------------------------------------ #
    def complete(
        self,
        row: IngestionRow,
        outcome,
        pages: Optional[List[int]] = None,
    ) -> PassResult:
        """Turn a finished (or unfinished) job into a stored record and a state.

        Shared with the reconciler, which reaches the same three endings by a
        different route: it polls a job the pipeline had to walk away from.
        """
        pages = pages if pages is not None else list(row.pages)

        if outcome.succeeded:
            record_id = self._store(row, outcome, pages)
            self.state.mark_succeeded(row.id, result_id=record_id, candidate_id=row.candidate_id)
            return PassResult(
                mode=row.ocr_mode, pages=pages, status="succeeded", row_id=row.id,
                job_id=outcome.job_id, record_id=record_id,
            )

        if outcome.status == "failed":
            # The *service* failed the job. Its own `retryable` flag is the best
            # information available about whether a fourth attempt would differ.
            detail = outcome.error or "OCR job failed"
            if not outcome.error_retryable:
                self.state.mark_failed(row.id, detail, max_attempts=1)
                return PassResult(
                    mode=row.ocr_mode, pages=pages, status="abandoned", row_id=row.id,
                    job_id=outcome.job_id, detail=detail,
                )
            status = self.state.mark_failed(row.id, detail, settings.ocr_job_max_attempts)
            return PassResult(
                mode=row.ocr_mode, pages=pages,
                status="abandoned" if status == "abandoned" else "failed",
                row_id=row.id, job_id=outcome.job_id, detail=detail,
            )

        # Still queued or running. The job id is on the row, so this is a
        # handover to the reconciler rather than a loss.
        self.state.touch(row.id)
        return PassResult(
            mode=row.ocr_mode, pages=pages, status="pending", row_id=row.id,
            job_id=outcome.job_id,
            detail=f"job still {outcome.status}; left for the reconciler",
        )

    # ------------------------------------------------------------------ #
    def _store(self, row: IngestionRow, outcome, pages: List[int]) -> str:
        store = _STORE_BY_MODE.get(row.ocr_mode)
        if store is None:  # pragma: no cover - guarded by IDENTITY_MODES
            raise ValueError(f"No record store for OCR mode {row.ocr_mode!r}")
        return store(
            row.id,
            outcome.result or {},
            candidate_id=row.candidate_id,
            provider=row.provider,
            account_id=row.account_id,
            message_id=row.message_id,
            attachment_id=row.attachment_id,
            filename=row.filename,
            sha256=row.sha256,
            pages=pages,
            ocr_job_id=outcome.job_id,
        )

    @staticmethod
    def _payload_for(
        data: bytes, pages: List[int], filename: str, mode: str
    ) -> tuple[bytes, str]:
        """The bytes to upload: the named pages alone where that is possible.

        Uploading a 60-page bundle to the Aadhaar endpoint to read page 54 wastes
        the upload, wastes the OCR, and gives the extractor fifty-nine pages of
        certificates to be confused by. `subset_pdf` returns None for images and
        for anything it cannot carve — the original bytes are then correct, and
        for a single-page image they are also already minimal.
        """
        subset = pdf_pages.subset_pdf(data, pages)
        if subset is None:
            return data, filename or f"{mode}.pdf"
        stem = (filename or "attachment").rsplit(".", 1)[0]
        return subset, f"{stem}_{mode}_p{'-'.join(str(n) for n in pages)}.pdf"
