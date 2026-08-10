"""Extract machine-readable text from any supported resume file.

Dispatch by detected file type. For PDFs we first try the embedded text layer;
if it is empty/short (a scanned document) we fall back to OCR. Images always go
through OCR. The result records *how* text was obtained so downstream code and
audits know whether OCR was involved.
"""
from __future__ import annotations

import re

from app.config import settings
from app.core.exceptions import TextExtractionError, UnsupportedFileTypeError
from app.core.models import ExtractedDocument, PageText
from app.extraction import file_type as ft
from app.extraction import page_classifier as pc
from app.extraction import pdf_pages
from app.extraction.ocr import (
    ocr_image_bytes,
    ocr_pdf_page_texts,
    ocr_via_veris,
    ocr_via_veris_pages,
)
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
        return _classified(_split_pages(text), method="plain")

    raise UnsupportedFileTypeError(f"Cannot extract text from category={kind.category}")


# --------------------------------------------------------------------------- #
def _page_layout_text(page) -> str:
    """One PDF page's text, with a two-column layout un-interleaved."""
    # Group blocks into visual columns (left sidebar vs right main body) to
    # prevent interleaving.
    blocks = page.get_text("blocks")
    mid_x = page.rect.width * 0.4

    valid_blocks = [b for b in blocks if len(b) > 4 and b[4].strip()]
    left_blocks = [b for b in valid_blocks if b[0] < mid_x]
    right_blocks = [b for b in valid_blocks if b[0] >= mid_x]

    left_blocks.sort(key=lambda b: b[1])
    right_blocks.sort(key=lambda b: b[1])

    if left_blocks and right_blocks and len(left_blocks) >= 2 and len(right_blocks) >= 2:
        sorted_blocks = left_blocks + right_blocks
    else:
        sorted_blocks = sorted(valid_blocks, key=lambda b: (b[1], b[0]))

    return "\n\n".join(b[4].strip() for b in sorted_blocks)


_SPACED_CHARS = re.compile(r"\b[A-Za-z]\s[A-Za-z]\s[A-Za-z]\s[A-Za-z]\b")


def _needs_ocr(text: str) -> bool:
    """A page whose text layer is missing, or is per-character garbage."""
    return len(text.strip()) < settings.ocr_min_text_chars or bool(_SPACED_CHARS.search(text))


def _extract_pdf(data: bytes, filename: str = "") -> ExtractedDocument:
    import fitz  # PyMuPDF

    page_texts: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        page_count = doc.page_count
        for page in doc:
            page_texts.append(_page_layout_text(page))

    ocr_pages: set[int] = set()
    method = "pdf_text"

    layer_chars = sum(len(t.strip()) for t in page_texts)
    joined = "\n\n".join(page_texts)
    is_spaced = bool(_SPACED_CHARS.search(joined))
    thin = layer_chars < settings.ocr_min_text_chars

    # OCR only when the text layer cannot be trusted. This used to fire on
    # "a Veris key is configured", which sent every PDF — text layer and all —
    # to the cloud in one whole-file call. That is what timed out on a 9-page
    # scan, and it was pure waste on the PDFs that could already be read.
    if thin or is_spaced:
        log.info(
            "PDF OCR triggered (%s, pages=%d, thin=%s, spaced=%s)",
            filename, page_count, thin, is_spaced,
        )
        page_texts, ocr_pages = _ocr_pdf_selectively(
            data, filename, page_texts, page_count,
        )
        if ocr_pages:
            method = "pdf_ocr"

    return _classified(
        page_texts,
        method=method,
        page_count=page_count,
        ocr_pages=ocr_pages,
    )


def _ocr_chunk(data: bytes, pages: list[int], filename: str) -> dict[int, str]:
    """OCR exactly these pages, cutting them out of the PDF first.

    The cloud OCR takes a *file*, so the only way to bound how much it has to
    chew on is to hand it a smaller file. A 9-page 1.6 MB scan timed out at 180
    seconds as one call; the same pages sent two at a time answer in seconds.
    """
    if not pages:
        return {}

    if settings.veris_ocr_api_key:
        payload = pdf_pages.subset_pdf(data, pages) or data
        try:
            texts = [t or "" for t in ocr_via_veris_pages(payload, filename or "resume.pdf")]
            if any(t.strip() for t in texts):
                if len(texts) == len(pages):
                    return dict(zip(pages, texts))
                # Veris merged or dropped a page, so per-page numbering inside
                # this chunk cannot be trusted. Keeping the text on the chunk's
                # first page loses the boundary but never the content.
                log.warning(
                    "Veris returned %d page(s) for a %d-page chunk %s; keeping the "
                    "text but not its page boundaries",
                    len(texts), len(pages), pages,
                )
                return {pages[0]: "\n\n".join(t for t in texts if t.strip())}
        except Exception as err:  # noqa: BLE001
            log.warning("Veris OCR failed on pages %s (%s); trying local OCR", pages, err)

    try:
        return ocr_pdf_page_texts(data, dpi=settings.ocr_dpi, pages=set(pages))
    except Exception as err:  # noqa: BLE001
        log.warning("Local OCR failed on pages %s: %s", pages, err)
        return {}


def _ocr_scan_progressively(
    data: bytes, filename: str, page_count: int,
) -> tuple[dict[int, str], int]:
    """Read a scanned bundle a couple of pages at a time, stopping at the résumé.

    A pure scan has no text layer, so there is nothing to classify from until
    something has been OCR'd — the page selection cannot be done up front. So do
    it incrementally: OCR a small chunk, classify what has been read so far, and
    stop once the résumé has been found and one page past it has been checked
    for a continuation.

    A CV on pages 1-2 of a 30-page bundle therefore costs four pages of OCR, and
    a CV on page 15 costs sixteen — never thirty, and never one giant call.
    """
    found: dict[int, str] = {}
    read = 0
    all_pages = list(range(1, page_count + 1))
    budget = max(1, settings.ocr_max_pages)

    for chunk in pdf_pages.chunks(all_pages, settings.ocr_chunk_pages):
        if read >= budget:
            log.warning(
                "Stopped OCR at the %d-page budget; pages %s of '%s' were never read "
                "(raise OCR_MAX_PAGES if resumes are being missed)",
                budget, [p for p in all_pages if p not in found and p > read], filename,
            )
            break

        found.update(_ocr_chunk(data, chunk, filename))
        read = chunk[-1]

        texts = [found.get(n, "") for n in all_pages]
        result = pc.classify_document(texts)
        if result.is_resume and result.resume_pages:
            # Keep going only while the résumé might continue onto a page we
            # have not looked at yet.
            if max(result.resume_pages) < read:
                log.info(
                    "Resume found on page(s) %s of '%s' after reading %d of %d page(s)",
                    result.resume_pages, filename, read, page_count,
                )
                break
            continue

        # Nothing resume-like yet. Whether to keep paying depends on what the
        # pages *are*: certificates, experience letters and ID scans are the
        # supporting documents of an application, and a CV can legitimately sit
        # behind fifteen of them. Invoices and hall tickets are not part of any
        # application, so once several pages in a row read as that, stop —
        # there is no CV coming.
        seen = [p.kind for p in result.pages if p.page_number <= read]
        # UNKNOWN counts as a reason to keep reading: the page had content and
        # committed to nothing, which is not evidence that no CV is coming.
        supporting = {pc.CERTIFICATE, pc.EXPERIENCE_LETTER, pc.ID_DOCUMENT, pc.UNKNOWN}
        if read >= settings.ocr_give_up_pages and not any(k in supporting for k in seen):
            log.info(
                "Stopping OCR of '%s' after %d of %d page(s): nothing read so far is "
                "a resume or an application document (kinds: %s)",
                filename, read, page_count, sorted(set(seen)),
            )
            break
    return found, read


def _ocr_pdf_selectively(
    data: bytes, filename: str, page_texts: list[str], page_count: int,
) -> tuple[list[str], set[int]]:
    """OCR the pages that need it — only the ones holding the résumé.

    Two cases:

      * The PDF has a usable text layer — classify from it for free, then OCR
        only those résumé pages whose text came out thin or per-character
        spaced. Often that is no pages at all.
      * The PDF is a pure scan — read it progressively, smallest call first,
        and stop as soon as the résumé has been located.
    """
    has_text_layer = sum(len(t.strip()) for t in page_texts) >= settings.ocr_min_text_chars

    if has_text_layer:
        wanted = pc.classify_document(page_texts).resume_pages
        targets = [n for n in wanted if _needs_ocr(page_texts[n - 1])]
        if not targets:
            return page_texts, set()
        log.info("Selective OCR on resume page(s) %s of %d", targets, page_count)
        fresh: dict[int, str] = {}
        for chunk in pdf_pages.chunks(targets, settings.ocr_chunk_pages):
            fresh.update(_ocr_chunk(data, chunk, filename))
    else:
        fresh, _read = _ocr_scan_progressively(data, filename, page_count)

    merged = list(page_texts)
    ocr_pages: set[int] = set()
    for number, text in fresh.items():
        if not text.strip():
            continue
        while len(merged) < number:
            merged.append("")
        merged[number - 1] = text
        ocr_pages.add(number)
    return merged, ocr_pages


def _classified(
    page_texts: list[str],
    method: str,
    page_count: int | None = None,
    ocr_pages: set[int] | None = None,
) -> ExtractedDocument:
    """Wrap extracted page text with the classifier's verdict.

    ``text`` stays the full extraction — every page, nothing dropped — and the
    classification records which slice of it is the candidate's profile.
    """
    ocr_pages = ocr_pages or set()

    # "I read it and it is not a resume" and "I could not read it" are opposite
    # findings, and collapsing them is dangerous: the first is a permanent skip,
    # the second is a retryable failure. A scanned 9-page CV with Tesseract
    # missing produced *zero* characters, which the classifier — correctly, on
    # the evidence it had — scored as a non-resume. That would have rejected
    # every scanned application in the mailbox as a misnamed file, and marked
    # each one handled. An empty extraction is an extraction error.
    if not any((t or "").strip() for t in page_texts):
        raise TextExtractionError(
            f"No text could be extracted from this {method} document "
            f"({len(page_texts)} page(s)). It is most likely a scan with no text "
            "layer while OCR is unavailable: install Tesseract (or set "
            "TESSERACT_CMD) or configure VERIS_OCR_API_KEY."
        )

    result = pc.classify_document(page_texts)
    pages = [
        PageText(
            page_number=p.page_number,
            text=page_texts[p.page_number - 1],
            kind=p.kind,
            score=p.score,
            ocr_used=p.page_number in ocr_pages,
        )
        for p in result.pages
    ]
    text = "\n\n".join(t.strip() for t in page_texts if t.strip())
    log.info(
        "Page classification: is_resume=%s conf=%.2f — %s",
        result.is_resume, result.confidence, result.reason,
    )
    return ExtractedDocument(
        text=text,
        method=method,
        page_count=page_count if page_count is not None else len(page_texts),
        ocr_used=bool(ocr_pages),
        char_count=len(text),
        pages=pages,
        resume_pages=result.resume_pages,
        is_resume=result.is_resume,
        classification_confidence=result.confidence,
        classification_reason=result.reason,
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
            return _classified(_split_pages(text), method="docx")
    except Exception as exc:  # noqa: BLE001 — fall through to docx2txt
        log.warning("python-docx failed (%s); trying docx2txt", exc)

    # Fallback: docx2txt.
    try:
        import docx2txt

        with io.BytesIO(data) as buf:
            text = (docx2txt.process(buf) or "").strip()
        if text:
            return _classified(_split_pages(text), method="docx")
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
    return _classified([text], method="image_ocr", page_count=1, ocr_pages={1})


def _extract_rtf(data: bytes) -> ExtractedDocument:
    # Minimal RTF → text: strip control words. Good enough to feed the AI.
    raw = data.decode("latin-1", errors="replace")
    raw = re.sub(r"\\'[0-9a-fA-F]{2}", " ", raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", raw)
    text = re.sub(r"[{}]", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise TextExtractionError("RTF produced no text.")
    return _classified([text], method="rtf", page_count=1)


def _split_pages(text: str) -> list[str]:
    """Page-break the formats that have no page model of their own.

    Word and plain-text documents carry no page count, but a converted or
    concatenated bundle usually still has form feeds where the pages were.
    """
    pages = [part for part in (text or "").split("\f")]
    return pages if len(pages) > 1 else [text or ""]
