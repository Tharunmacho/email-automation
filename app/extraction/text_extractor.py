"""Extract machine-readable text from any supported resume file.

Dispatch by detected file type. For PDFs we first try the embedded text layer;
if it is empty/short (a scanned document) we fall back to OCR. Images always go
through OCR. The result records *how* text was obtained so downstream code and
audits know whether OCR was involved.
"""
from __future__ import annotations

from app.config import settings
from app.core.exceptions import TextExtractionError, UnsupportedFileTypeError
from app.core.models import ExtractedDocument
from app.extraction import file_type as ft
from app.extraction.ocr import ocr_image_bytes, ocr_pdf_pages, ocr_via_veris
from app.logging_config import get_logger

log = get_logger(__name__)


def extract_text(data: bytes, filename: str = "") -> ExtractedDocument:
    kind = ft.detect(data, filename)
    log.info("Extracting text from %s (category=%s)", filename or "<attachment>", kind.category)

    if kind.category == ft.CATEGORY_PDF:
        return _extract_pdf(data, filename)
    if kind.category == ft.CATEGORY_DOCX:
        return _extract_docx(data)
    if kind.category == ft.CATEGORY_DOC:
        return _extract_doc(data)
    if kind.category == ft.CATEGORY_IMAGE:
        return _extract_image(data, filename)
    if kind.category == ft.CATEGORY_RTF:
        return _extract_rtf(data)
    if kind.category == ft.CATEGORY_TEXT:
        text = data.decode("utf-8", errors="replace")
        return ExtractedDocument(text=text, method="plain", char_count=len(text))

    raise UnsupportedFileTypeError(f"Cannot extract text from category={kind.category}")


# --------------------------------------------------------------------------- #
def _extract_pdf(data: bytes, filename: str = "") -> ExtractedDocument:
    import fitz  # PyMuPDF

    text_parts: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        page_count = doc.page_count
        for page in doc:
            text_parts.append(page.get_text("text"))
    text = "\n".join(text_parts).strip()

    # Heuristic: too little text ⇒ scanned PDF ⇒ OCR fallback.
    if len(text) < settings.ocr_min_text_chars:
        log.info("PDF text layer is thin (%d chars); falling back to OCR", len(text))
        if settings.veris_ocr_api_key:
            ocr_text = ocr_via_veris(data, filename or "resume.pdf").strip()
        else:
            ocr_text = ocr_pdf_pages(data).strip()
        if len(ocr_text) < settings.ocr_min_text_chars and not text:
            raise TextExtractionError("PDF produced no usable text, even after OCR.")
        return ExtractedDocument(
            text=ocr_text or text,
            method="pdf_ocr",
            page_count=page_count,
            ocr_used=True,
            char_count=len(ocr_text or text),
        )

    return ExtractedDocument(
        text=text, method="pdf_text", page_count=page_count,
        ocr_used=False, char_count=len(text),
    )


def _extract_docx(data: bytes) -> ExtractedDocument:
    import io

    # Primary: python-docx (captures paragraphs + tables).
    try:
        import docx

        document = docx.Document(io.BytesIO(data))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append("\t".join(cell.text for cell in row.cells))
        text = "\n".join(parts).strip()
        if text:
            return ExtractedDocument(text=text, method="docx", char_count=len(text))
    except Exception as exc:  # noqa: BLE001 — fall through to docx2txt
        log.warning("python-docx failed (%s); trying docx2txt", exc)

    # Fallback: docx2txt.
    try:
        import docx2txt

        with io.BytesIO(data) as buf:
            text = (docx2txt.process(buf) or "").strip()
        if text:
            return ExtractedDocument(text=text, method="docx", char_count=len(text))
    except Exception as exc:  # noqa: BLE001
        raise TextExtractionError(f"Could not read .docx: {exc}") from exc

    raise TextExtractionError("DOCX contained no extractable text.")


def _extract_doc(data: bytes) -> ExtractedDocument:
    """Legacy binary .doc — needs an external converter (LibreOffice/antiword).

    We attempt a headless LibreOffice conversion to .docx if available, then reuse
    the docx path. If no converter is present we raise a clear, actionable error.
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise UnsupportedFileTypeError(
            "Legacy .doc files require LibreOffice for conversion. Install LibreOffice "
            "(soffice on PATH), or ask senders to submit .docx/.pdf."
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.doc"
        src.write_bytes(data)
        subprocess.run(
            [soffice, "--headless", "--convert-to", "docx", "--outdir", tmp, str(src)],
            check=True, capture_output=True, timeout=120,
        )
        converted = Path(tmp) / "resume.docx"
        if not converted.exists():
            raise TextExtractionError("LibreOffice did not produce a .docx from the .doc.")
        result = _extract_docx(converted.read_bytes())
        result.method = "doc"
        return result


def _extract_image(data: bytes, filename: str = "") -> ExtractedDocument:
    if settings.veris_ocr_api_key:
        text = ocr_via_veris(data, filename or "resume.png").strip()
    else:
        text = ocr_image_bytes(data).strip()
    if len(text) < settings.ocr_min_text_chars:
        log.warning("Image OCR produced only %d chars", len(text))
    if not text:
        raise TextExtractionError("Image OCR produced no text.")
    return ExtractedDocument(text=text, method="image_ocr", ocr_used=True, char_count=len(text))


def _extract_rtf(data: bytes) -> ExtractedDocument:
    # Minimal RTF → text: strip control words. Good enough to feed the AI.
    import re

    raw = data.decode("latin-1", errors="replace")
    raw = re.sub(r"\\'[0-9a-fA-F]{2}", " ", raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", raw)
    text = re.sub(r"[{}]", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise TextExtractionError("RTF produced no text.")
    return ExtractedDocument(text=text, method="rtf", char_count=len(text))
