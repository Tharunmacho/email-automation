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


def ocr_pdf_pages(pdf_data: bytes, dpi: int = 150) -> str:
    """Render each PDF page to an image and OCR it. Used when a PDF has no text layer."""
    pytesseract = _ensure_tesseract()
    import fitz  # PyMuPDF
    from PIL import Image

    texts: list[str] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(stream=pdf_data, filetype="pdf") as doc:
        for page_index, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            page_text = pytesseract.image_to_string(img, lang=settings.ocr_languages)
            texts.append(page_text)
            log.debug("OCR page %d → %d chars", page_index + 1, len(page_text))
    return "\n".join(texts)


def ocr_via_veris(file_data: bytes, filename: str) -> str:
    """Run OCR via the Veris OCR cloud API, falling back to local Tesseract if it fails."""
    import tempfile
    from pathlib import Path

    suffix = Path(filename).suffix or ".pdf"
    with tempfile.TemporaryDirectory() as tmp:
        temp_file = Path(tmp) / f"temp_ocr{suffix}"
        temp_file.write_bytes(file_data)

        log.info("Running Veris OCR on file %s (%d bytes)", filename, len(file_data))
        try:
            from recursai.veris_ocr import VerisOCR
            with VerisOCR(api_key=settings.veris_ocr_api_key, base_url=settings.veris_ocr_base_url) as client:
                res = client.resume.extract(str(temp_file))
                pages = getattr(res, "pages", [])
                if isinstance(pages, list):
                    extracted_text = "\n".join(
                        page.get("text", "") if isinstance(page, dict) else getattr(page, "text", "")
                        for page in pages
                    )
                else:
                    extracted_text = ""
                log.info("Veris OCR successfully processed file; extracted %d chars", len(extracted_text))
                return extracted_text
        except Exception as e:
            log.warning("Veris OCR API failed (%s). Falling back to local Tesseract OCR.", e)
            if suffix.lower() == ".pdf":
                return ocr_pdf_pages(file_data)
            else:
                return ocr_image_bytes(file_data)
