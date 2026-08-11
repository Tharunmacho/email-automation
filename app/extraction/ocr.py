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
    apart from the certificates scanned into the same PDF. Falls back to local
    Tesseract if Veris fails.
    """
    import tempfile
    from pathlib import Path

    suffix = Path(filename).suffix or ".pdf"
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
                pages = getattr(res, "pages", [])
                if isinstance(pages, list):
                    page_texts = [
                        page.get("text", "") if isinstance(page, dict) else getattr(page, "text", "")
                        for page in pages
                    ]
                else:
                    page_texts = []
                log.info(
                    "Veris OCR successfully processed file; extracted %d chars over %d page(s)",
                    sum(len(p) for p in page_texts), len(page_texts),
                )
                return page_texts
        except Exception as e:
            log.warning("Veris OCR API failed (%s). Falling back to local OCR if available.", e)
            try:
                if suffix.lower() == ".pdf":
                    texts = ocr_pdf_page_texts(file_data)
                    return [texts[n] for n in sorted(texts)]
                return [ocr_image_bytes(file_data)]
            except Exception as fallback_err:
                log.warning("Local OCR fallback skipped/failed: %s", fallback_err)
                return []
