"""OCR helpers for scanned PDFs and image resumes.

Uses Tesseract via ``pytesseract``. The Tesseract *binary* must be installed on
the host (it is not a pip package). If it is missing we raise a clear error so
the operator knows exactly what to install, rather than failing cryptically.
"""
from __future__ import annotations

import io
import shutil

from app.config import settings
from app.core.exceptions import TextExtractionError
from app.logging_config import get_logger

log = get_logger(__name__)


def _ensure_tesseract():
    import pytesseract

    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
        return pytesseract
    if shutil.which("tesseract") is None:
        raise TextExtractionError(
            "Tesseract OCR is required for scanned/image resumes but was not found. "
            "Install it (https://github.com/tesseract-ocr/tesseract) and either add it "
            "to PATH or set TESSERACT_CMD in your .env."
        )
    return pytesseract


def ocr_image_bytes(data: bytes) -> str:
    """Run OCR on a single raster image given as bytes."""
    pytesseract = _ensure_tesseract()
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        return pytesseract.image_to_string(img, lang=settings.ocr_languages)


def ocr_pdf_page_texts(
    pdf_data: bytes, dpi: int = 150, pages: "set[int] | None" = None
) -> "dict[int, str]":
    """OCR a PDF page by page, returning ``{1-based page number: text}``.

    ``pages`` restricts the work to those page numbers. On a 30-page application
    bundle whose résumé is two pages, that is the difference between 30 OCR
    passes and 2 — which is the whole point of classifying pages first.
    """
    pytesseract = _ensure_tesseract()
    import fitz  # PyMuPDF
    from PIL import Image

    out: dict[int, str] = {}
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(stream=pdf_data, filetype="pdf") as doc:
        for page_index, page in enumerate(doc):
            page_number = page_index + 1
            if pages is not None and page_number not in pages:
                continue
            pix = page.get_pixmap(matrix=matrix)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            page_text = pytesseract.image_to_string(img, lang=settings.ocr_languages)
            out[page_number] = page_text
            log.debug("OCR page %d @%ddpi → %d chars", page_number, dpi, len(page_text))
    return out


def ocr_pdf_pages(pdf_data: bytes, dpi: int = 150) -> str:
    """Render each PDF page to an image and OCR it. Used when a PDF has no text layer."""
    texts = ocr_pdf_page_texts(pdf_data, dpi=dpi)
    return "\n".join(texts[n] for n in sorted(texts))


def ocr_via_veris(file_data: bytes, filename: str) -> str:
    """Run OCR via Veris and return the whole document as one string."""
    return "\n".join(ocr_via_veris_pages(file_data, filename))


def ocr_via_veris_pages(file_data: bytes, filename: str) -> "list[str]":
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

    def _local() -> "list[str]":
        try:
            if suffix.lower() == ".pdf":
                texts = ocr_pdf_page_texts(file_data)
                return [texts[n] for n in sorted(texts)]
            return [ocr_image_bytes(file_data)]
        except Exception as fallback_err:  # noqa: BLE001
            log.warning("Local OCR skipped/failed: %s", fallback_err)
            return []

    if not settings.veris_ocr_api_key:
        return _local()

    if settings.ocr_async_jobs_enabled:
        pages = _veris_pages_via_job(file_data, filename)
        if pages is not None:
            return pages
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
                return page_texts
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


def _veris_pages_via_job(file_data: bytes, filename: str) -> "list[str] | None":
    """The résumé pass, through the job queue.

    Returns None — not an empty list — when the queue could not be used at all,
    because "no job" and "a job that found no text" have to route differently:
    the first falls back to the synchronous endpoint, the second is an answer.
    """
    from app.extraction.jobs import (
        MODE_RESUME,
        AsyncOCRJobClient,
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

    try:
        with AsyncOCRJobClient() as client:
            handle = client.submit(file_data, filename or "resume.pdf", MODE_RESUME, key)
            if recorder is not None:
                recorder.on_submitted(MODE_RESUME, handle.job_id, key)

            outcome = client.wait(handle.job_id, MODE_RESUME, settings.ocr_job_wait_seconds)
            if recorder is not None:
                recorder.on_finished(MODE_RESUME, handle.job_id, outcome.status, outcome.error)

            if outcome.succeeded:
                page_texts = _page_texts_from((outcome.result or {}).get("pages"))
                log.info(
                    "Veris job %s extracted %d chars over %d page(s) from %s",
                    handle.job_id, sum(len(p) for p in page_texts), len(page_texts), filename,
                )
                return page_texts

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
