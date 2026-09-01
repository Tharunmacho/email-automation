"""Bridge between the extraction layer's OCR jobs and the ingestion ledger.

`app.extraction` deliberately knows nothing about MongoDB — it reads documents.
`app.db.ingestion_state` deliberately knows nothing about OCR — it records
state. This is the twenty lines that let the résumé pass, which happens deep
inside `text_extractor`, still land on an ingestion row that the reconciler can
find later.

It implements `app.extraction.jobs.JobRecorder`, and is handed to the extraction
through the ambient `JobContext` for exactly one attachment.
"""
from __future__ import annotations

from typing import List, Optional

from app.config import settings
from app.db.ingestion_state import IngestionStateStore, PROVIDER_EMAIL
from app.logging_config import get_logger

log = get_logger(__name__)


class IngestionStateRecorder:
    """Writes one attachment's OCR job transitions onto its ingestion rows.

    Every method swallows its own failures. A row that cannot be written is an
    observability problem; letting it abort the extraction would turn it into a
    lost candidate, which is much worse.
    """

    def __init__(
        self,
        message_id: str,
        attachment_id: str,
        *,
        filename: str = "",
        sha256: str = "",
        storage_key: str = "",
        pages: Optional[List[int]] = None,
        candidate_id: Optional[str] = None,
        account_id: str = "",
        provider: str = PROVIDER_EMAIL,
        state: Optional[IngestionStateStore] = None,
    ):
        self.message_id = message_id
        self.attachment_id = attachment_id
        self.filename = filename
        self.sha256 = sha256
        self.storage_key = storage_key
        self.pages = list(pages or [])
        self.candidate_id = candidate_id
        self.provider = provider
        self._account_id = account_id
        self._state = state
        #: mode → row id, so `on_finished` does not have to re-derive the key.
        self.rows: dict[str, str] = {}

    @property
    def account_id(self) -> str:
        """Which mailbox this attachment's message came from.

        Read off the message id first, because that is the only source that is
        right per *message*. `mailbox_account_id()` answers from global
        settings, so with two mailboxes polled by one process it returned the
        same account for both — and the ingestion row's natural key, whose
        whole job is to stop two mailboxes colliding on a message id, was
        being stamped with the wrong half of the pair. A Gmail UID landed on a
        row labelled `cv@adiragroups.com`.

        The fallback remains for ids written before they carried an account,
        and for the single-mailbox deployments where it is simply true.
        """
        if not self._account_id:
            from app.core.message_ids import account_of
            from app.ingestion.multipass import mailbox_account_id

            self._account_id = account_of(self.message_id) or mailbox_account_id()
        return self._account_id

    @property
    def state(self) -> IngestionStateStore:
        if self._state is None:
            self._state = IngestionStateStore()
        return self._state

    # ---- JobRecorder ------------------------------------------------------ #
    def on_submitted(self, mode: str, job_id: str, idempotency_key: str) -> None:
        try:
            row = self.state.open_row(
                self.provider,
                self.account_id,
                self.message_id,
                self.attachment_id,
                mode,
                sha256=self.sha256,
                storage_key=self.storage_key,
                filename=self.filename,
                pages=self.pages,
                candidate_id=self.candidate_id,
            )
            self.rows[mode] = row.id
            # Best-effort: the claim is how attempts are counted, but a row
            # already claimed by a concurrent worker still needs this job id
            # recorded — it is the same job, under the same idempotency key.
            self.state.claim_for_submit(row.id, settings.ocr_job_max_attempts)
            self.state.mark_submitted(row.id, job_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not record %s job %s on the ingestion ledger: %s", mode, job_id, exc)

    def on_finished(self, mode: str, job_id: str, status: str, error: str = "") -> None:
        row_id = self.rows.get(mode)
        if not row_id:
            return
        try:
            if status == "succeeded":
                self.state.mark_succeeded(row_id, candidate_id=self.candidate_id)
            elif status == "failed":
                self.state.mark_failed(row_id, error or "OCR job failed",
                                       settings.ocr_job_max_attempts)
            else:
                # Queued or running: the wait ran out, not the job. Touching it
                # keeps it off the stuck list for one more sweep interval.
                self.state.touch(row_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not close %s job %s on the ingestion ledger: %s", mode, job_id, exc)

    # ---- pipeline ---------------------------------------------------------- #
    def link_candidate(self, candidate_id: str) -> None:
        """Attach the candidate to every row this attachment produced.

        Called after the insert, because the résumé row is opened long before
        there is a candidate to point it at.
        """
        self.candidate_id = candidate_id
        for row_id in self.rows.values():
            try:
                self.state.set_candidate(row_id, candidate_id)
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not link row %s to candidate %s: %s", row_id, candidate_id, exc)
