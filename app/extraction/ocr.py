"""The Veris OCR endpoints, and the local reader they fall back to.

Local reading itself lives in `app.extraction.local_ocr`; this module is about
the *cloud* passes and when they are worth making. The order matters and it is
the opposite of what it used to be: a document is now read locally and
classified first, and only pages already established to be a resume are sent to
the resume endpoint. Nothing is uploaded to find out what it is.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.extraction import local_ocr
from app.logging_config import get_logger

log = get_logger(__name__)


# Reading a page is `local_ocr`'s job — preprocessing, several segmentation
# modes, DPI escalation, an optional second engine and a page cache all live
# there. These three names stay because the reconciler and a handful of
# operational scripts import them, and they now forward rather than keeping a
# second, weaker copy of the same logic.


def ocr_image_bytes(data: bytes) -> str:
    """Run OCR on a single raster image given as bytes."""
    return local_ocr.ocr_image_bytes(data)


def ocr_pdf_page_texts(
    pdf_data: bytes, dpi: int = 150, pages: "set[int] | None" = None
) -> "dict[int, str]":
    """OCR a PDF page by page, returning ``{1-based page number: text}``."""
    return local_ocr.ocr_pdf_page_texts(pdf_data, dpi=dpi, pages=pages)


def ocr_pdf_pages(pdf_data: bytes, dpi: int = 150) -> str:
    """Every page of a PDF as one string. Used when a PDF has no text layer."""
    return local_ocr.ocr_pdf_pages(pdf_data, dpi=dpi)


def ocr_via_veris(file_data: bytes, filename: str) -> str:
    """Run OCR via Veris and return the whole document as one string."""
    return "\n".join(ocr_via_veris_pages(file_data, filename))


@dataclass
class VerisRead:
    """One résumé pass, and everything it came back with.

    `pages` is the per-page text; `result` is the untouched job payload the
    service returned, which carries the *structured* résumé fields alongside
    that text. Both used to be fetched separately — the text here and the
    fields again from `resume_parser`, two jobs against the same endpoint over
    the same pages, differing only in idempotency key. The second was pure
    duplication: same upload, same extraction, billed twice and waited for
    twice. Keeping the payload lets one call answer both.

    `result` is None when the answer did not come from the job queue — a
    synchronous call or a local fallback — and the caller then does what it
    always did.
    """

    pages: "list[str]"
    result: "dict | None" = None


def ocr_via_veris_pages(file_data: bytes, filename: str) -> "list[str]":
    """Page text only. Kept for callers with no use for the structured fields."""
    return ocr_via_veris_read(file_data, filename).pages


def ocr_via_veris_read(file_data: bytes, filename: str) -> "VerisRead":
    """Run OCR via the Veris OCR cloud API, one string per page.

    Page boundaries have to survive: the classifier needs them to tell the CV
    apart from the certificates scanned into the same PDF.

    Three routes, in order of preference:

    1. **The job queue** (`POST /v1/jobs`, mode=resume). The extraction is
       queued and polled, so a 9-page scan that takes four minutes is no longer
       a lost request — and a retry of the same mail re-attaches to the running
       job by idempotency key instead of paying for it twice.
    2. **The synchronous endpoint**, when async jobs are switched off.
    3. **Local Tesseract**, when Veris is unreachable or unconfigured.
    """
    import tempfile
    from pathlib import Path

    suffix = Path(filename).suffix or ".pdf"

    def _local() -> "VerisRead":
        try:
            if suffix.lower() == ".pdf":
                texts = ocr_pdf_page_texts(file_data)
                return VerisRead([texts[n] for n in sorted(texts)])
            return VerisRead([ocr_image_bytes(file_data)])
        except Exception as fallback_err:  # noqa: BLE001
            log.warning("Local OCR skipped/failed: %s", fallback_err)
            return VerisRead([])

    if not settings.veris_ocr_api_key:
        return _local()

    if settings.ocr_async_jobs_enabled:
        read = _veris_read_via_job(file_data, filename)
        if read is not None:
            return read
        log.info("Falling back to the synchronous Veris endpoint for %s", filename)

    with tempfile.TemporaryDirectory() as tmp:
        temp_file = Path(tmp) / f"temp_ocr{suffix}"
        temp_file.write_bytes(file_data)

        log.info("Running Veris OCR on file %s (%d bytes)", filename, len(file_data))
        try:
            from recursai.veris_ocr import VerisOCR
            init_kwargs = {
                "api_key": settings.veris_ocr_api_key,
                "base_url": settings.veris_ocr_base_url,
            }
            try:
                client = VerisOCR(timeout=settings.veris_timeout_seconds, **init_kwargs)
            except TypeError:
                client = VerisOCR(**init_kwargs)

            with client:
                res = client.resume.extract(str(temp_file))
                page_texts = _page_texts_from(getattr(res, "pages", []))
                log.info(
                    "Veris OCR successfully processed file; extracted %d chars over %d page(s)",
                    sum(len(p) for p in page_texts), len(page_texts),
                )
                return VerisRead(page_texts)
        except Exception as e:
            log.warning("Veris OCR API failed (%s). Falling back to local OCR if available.", e)
            return _local()


def _page_texts_from(pages) -> "list[str]":
    """Pull ``text`` off each page of a Veris response, whatever shape it is in.

    The synchronous SDK returns page objects; a job result comes back as plain
    dicts off the wire. Both mean the same thing.
    """
    if not isinstance(pages, list):
        return []
    out = []
    for page in pages:
        if isinstance(page, dict):
            out.append(page.get("text", "") or "")
        else:
            out.append(getattr(page, "text", "") or "")
    return out


def _veris_read_via_job(file_data: bytes, filename: str) -> "VerisRead | None":
    """The résumé pass, through the job queue.

    Returns None — not an empty read — when the queue could not be used at all,
    because "no job" and "a job that found no text" have to route differently:
    the first falls back to the synchronous endpoint, the second is an answer.
    """
    from app.extraction import ocr_gateway
    from app.extraction.jobs import (
        MODE_RESUME,
        OCRJobError,
        content_key,
        current_job_context,
    )

    context = current_job_context()
    key = (
        context.key_for(MODE_RESUME, content_key(file_data))
        if context
        else f"content/{content_key(file_data)}/{MODE_RESUME}"
    )
    recorder = getattr(context, "recorder", None)

    def _record_submission(handle) -> None:
        # Fires the moment Veris accepts the work, before the wait begins. The
        # wait is the interruptible part, and the job id is the only thing that
        # makes the extraction recoverable when it is interrupted.
        if recorder is not None:
            recorder.on_submitted(MODE_RESUME, handle.job_id, key)

    try:
        # Through the gateway: a connection this thread already has open, and an
        # in-flight slot released the instant this job finishes so the next
        # resume in the batch is submitted immediately rather than when some
        # unrelated thread frees up.
        handle, outcome = ocr_gateway.run_job(
            file_data,
            filename or "resume.pdf",
            MODE_RESUME,
            key,
            budget_seconds=settings.ocr_job_wait_seconds,
            on_submitted=_record_submission,
        )
        if handle is None or outcome is None:
            return None

        if recorder is not None:
            recorder.on_finished(MODE_RESUME, handle.job_id, outcome.status, outcome.error)

        if outcome.succeeded:
            page_texts = _page_texts_from((outcome.result or {}).get("pages"))
            log.info(
                "Veris job %s extracted %d chars over %d page(s) from %s",
                handle.job_id, sum(len(p) for p in page_texts), len(page_texts), filename,
            )
            # The payload goes back whole. The structured fields are already in
            # it, and fetching them again is a second job for the same answer.
            return VerisRead(page_texts, outcome.result or None)

        if outcome.pending:
            # Still running, and the job id is recorded. Reading the file
            # locally now would spend Tesseract on work that is already paid
            # for — but returning nothing would reject a real resume, so the
            # local read is the lesser evil and the next poll of this
            # (still unlabelled) mail collects the job's own answer.
            log.warning(
                "Veris job %s for %s is still %s after %.0fs; using the local read this pass",
                handle.job_id, filename, outcome.status, settings.ocr_job_wait_seconds,
            )
            return None

        log.warning("Veris job %s failed for %s: %s", handle.job_id, filename, outcome.error)
        return None
    except OCRJobError as exc:
        log.warning("Could not run %s through the OCR job queue (%s)", filename, exc)
        if recorder is not None:
            recorder.on_finished(MODE_RESUME, "", "failed", str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 — never fatal; the sync path is next
        log.warning("Unexpected error on the OCR job queue for %s: %s", filename, exc)
        return None
