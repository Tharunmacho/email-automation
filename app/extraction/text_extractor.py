"""Extract machine-readable text from any supported resume file.

Dispatch is by detected file type, never by filename. For PDFs the embedded text
layer is used wherever it is trustworthy and every other page is read locally;
images always go through OCR. The result records *how* the text was obtained so
downstream code and audits know whether OCR was involved.

The ordering here is the point. Every page is read before anything is judged,
and every page is judged before anything is uploaded:

    read the whole document locally
        -> classify each page from its own content
            -> resume pages   -> the Veris resume endpoint
               Aadhaar pages  -> the Aadhaar endpoint      (multipass)
               Indian passport pages -> the passport endpoint (multipass)
               everything else -> never uploaded at all

A file therefore cannot be billed as a resume extraction because of what it was
called, and an Aadhaar card on the last page of a forty-page bundle cannot be
missed because the CV was found on page three.

"""
from __future__ import annotations

import re

from app.config import settings
from app.core.exceptions import TextExtractionError, UnsupportedFileTypeError
from app.core.models import ExtractedDocument, PageText
from app.extraction import file_type as ft
from app.extraction import local_ocr
from app.extraction import page_classifier as pc
from app.extraction import pdf_pages
from app.extraction import resume_nationality as rn
from app.extraction.ocr import ocr_via_veris_pages, ocr_via_veris_read
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
    if kind.category == ft.CATEGORY_ODT:
        return _extract_odt(data)
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
    """Read every page of the PDF, then say what is on each of them.

    The text layer is free and is used wherever it is trustworthy; every other
    page is read locally. No page is skipped on the grounds that the résumé has
    already been found — the identity documents in an application bundle are
    usually *behind* the CV, and stopping at the CV is precisely why they were
    never extracted.
    """
    import fitz  # PyMuPDF

    page_texts: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        page_count = doc.page_count
        for page in doc:
            page_texts.append(_page_layout_text(page))

    method = "pdf_text"

    # Which pages the embedded text layer cannot answer for. A page with no text
    # at all is a scan; one whose text comes back per-character spaced is a
    # broken font map, and both need to be read as pictures.
    targets = [n for n in range(1, page_count + 1) if _needs_ocr(page_texts[n - 1])]

    # A safety net against a mis-sent thousand-page scan, not a budget: local
    # reading costs CPU, not money. Enforced here rather than only inside the
    # reader so the limit holds however the pages are read, and it is always
    # reported with the page numbers that went unread.
    ceiling = max(1, settings.ocr_max_pages)
    if len(targets) > ceiling:
        log.warning(
            "'%s' needs %d page(s) read but the safety ceiling is %d; pages %s were "
            "not read (raise OCR_MAX_PAGES)",
            filename or "attachment", len(targets), ceiling, targets[ceiling:],
        )
        targets = targets[:ceiling]

    if targets:
        log.info(
            "Reading %d of %d page(s) of '%s' locally (no usable text layer)",
            len(targets), page_count, filename or "attachment",
        )
        reads = local_ocr.ocr_pdf_page_reads(data, pages=set(targets), filename=filename)
        fresh = {number: read.text for number, read in reads.items()}
        page_texts, ocr_pages = _merge_pages(page_texts, fresh)

        # Whatever the local pass could not read, the cloud reader is asked for.
        #
        # This is the difference between "there is nothing on this page" and "we
        # failed to read this page", and until now the pipeline could not tell
        # them apart: both arrived at the classifier as an empty string, scored
        # nothing, and were filed as an ignored certificate. An identity
        # document only reaches the endpoint that can extract it if the local
        # read was already good enough to name it, so every page lost here was
        # lost silently and permanently.
        unread = [n for n in targets if not page_texts[n - 1].strip()]
        if unread:
            page_texts, recovered = _recover_unread_pages(
                data, filename, page_texts, unread
            )
            ocr_pages |= recovered

        if ocr_pages:
            method = "pdf_ocr"
    else:
        ocr_pages = set()

    return _classified(
        page_texts,
        method=method,
        page_count=page_count,
        ocr_pages=ocr_pages,
        data=data,
        filename=filename,
    )


def _merge_pages(
    page_texts: list[str], fresh: dict[int, str],
) -> tuple[list[str], set[int]]:
    """Fold freshly-read pages into the text-layer read, keeping page numbers."""
    merged = list(page_texts)
    read: set[int] = set()
    for number, text in fresh.items():
        if not text.strip():
            continue
        while len(merged) < number:
            merged.append("")
        merged[number - 1] = text
        read.add(number)
    return merged, read


def _recover_unread_pages(
    data: bytes, filename: str, page_texts: list[str], unread: list[int],
) -> "tuple[list[str], set[int]]":
    """Re-read the pages local OCR returned nothing for, through Veris.

    Only those pages, and only when there are some: a bundle that read cleanly
    never gets here, so this costs nothing on the common case.

    The pages are sent as a subset PDF for the same reason the resume and
    identity payloads are — a trimmed page keeps the scanner's full resolution,
    and the upload is most of the round trip — and the result is merged back at
    the page numbers it came from, so the classifier that runs next sees one
    complete document rather than a local read with holes in it.

    A page the cloud reader cannot read either stays empty. That is a real
    finding rather than a race, and it is logged as one.
    """
    if not (settings.veris_recover_unread_pages and settings.veris_ocr_api_key):
        log.warning(
            "Local OCR returned no text for page(s) %s of '%s' and cloud recovery "
            "is off; anything on those pages — an identity document included — "
            "cannot be classified (set VERIS_OCR_API_KEY and "
            "VERIS_RECOVER_UNREAD_PAGES=true)",
            unread, filename or "attachment",
        )
        return page_texts, set()

    ceiling = max(1, settings.veris_recover_max_pages)
    wanted = unread[:ceiling]
    if len(unread) > ceiling:
        log.warning(
            "'%s' has %d unread page(s) but only %d may be recovered; page(s) %s "
            "stay unread (raise VERIS_RECOVER_MAX_PAGES)",
            filename or "attachment", len(unread), ceiling, unread[ceiling:],
        )

    subset = pdf_pages.subset_pdf(data, wanted)
    if not subset:
        log.warning(
            "Could not isolate unread page(s) %s of '%s' for recovery",
            wanted, filename or "attachment",
        )
        return page_texts, set()

    log.info(
        "Local OCR read nothing on page(s) %s of '%s'; asking Veris for them",
        wanted, filename or "attachment",
    )
    payload = pdf_pages.compact_pdf(
        subset, settings.ocr_payload_max_bytes, settings.ocr_payload_dpi
    )
    try:
        read = ocr_via_veris_read(payload, filename or "attachment.pdf")
    except Exception as exc:  # noqa: BLE001 — the local read still stands
        log.warning(
            "Veris recovery of page(s) %s of '%s' failed (%s); those pages stay unread",
            wanted, filename or "attachment", exc,
        )
        return page_texts, set()

    merged = list(page_texts)
    recovered: set[int] = set()
    for index, number in enumerate(wanted):
        text = read.pages[index] if index < len(read.pages) else ""
        if not (text or "").strip():
            continue
        merged[number - 1] = text
        recovered.add(number)

    still_blank = [n for n in wanted if n not in recovered]
    if recovered:
        log.info(
            "Veris recovered page(s) %s of '%s' (%d chars) that local OCR could not read",
            sorted(recovered), filename or "attachment",
            sum(len(merged[n - 1]) for n in recovered),
        )
    if still_blank:
        log.warning(
            "Page(s) %s of '%s' read as empty both locally and at Veris; treating "
            "them as genuinely blank",
            still_blank, filename or "attachment",
        )
    return merged, recovered


def _refine_resume_pages(
    data: bytes, filename: str, page_texts: list[str], resume_pages: list[int],
) -> "tuple[list[str], dict | None]":
    """Re-read the résumé pages — and only those — through the Veris endpoint.

    This is the one place anything is uploaded, and it runs *after* the local
    read has established that these pages are a résumé. That ordering is the
    fix for the original complaint: a bank statement or a job-board digest is
    now identified locally and never reaches a paid extraction, while a real CV
    still gets the better read on the two pages that hold it.
    """
    if not (settings.veris_refine_resume_pages and settings.veris_ocr_api_key):
        return page_texts, None
    if not resume_pages:
        return page_texts, None

    # Page trim, then size trim — the same treatment the Aadhaar and passport
    # payloads get, for the same reason: a trimmed page still carries the
    # scanner's full resolution, and that upload is most of the round trip.
    payload = pdf_pages.compact_pdf(
        pdf_pages.subset_pdf(data, resume_pages) or data,
        settings.ocr_payload_max_bytes,
        settings.ocr_payload_dpi,
    )
    try:
        read = ocr_via_veris_read(payload, filename or "resume.pdf")
        texts = [t or "" for t in read.pages]
        extraction = read.result
    except Exception as exc:  # noqa: BLE001 — the local read already stands
        log.warning("Veris refinement of pages %s failed (%s); keeping the local read",
                    resume_pages, exc)
        return page_texts, None

    if not any(t.strip() for t in texts):
        return page_texts, extraction

    refined = list(page_texts)
    if len(texts) == len(resume_pages):
        for number, text in zip(resume_pages, texts):
            if len(text.strip()) > len(refined[number - 1].strip()):
                refined[number - 1] = text
    else:
        # Veris merged or dropped a page, so the per-page mapping cannot be
        # trusted. The combined text goes on the first résumé page: the boundary
        # is lost, the content is not.
        combined = "\n\n".join(t for t in texts if t.strip())
        if len(combined.strip()) > len(refined[resume_pages[0] - 1].strip()):
            refined[resume_pages[0] - 1] = combined
    log.info("Refined résumé page(s) %s of '%s' through Veris", resume_pages, filename)
    return refined, extraction



def _classified(
    page_texts: list[str],
    method: str,
    page_count: int | None = None,
    ocr_pages: set[int] | None = None,
    data: bytes | None = None,
    filename: str = "",
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

    # Whose CV is this? Asked here, of the local read, and before the upload
    # below — a candidate this desk cannot place must not cost a résumé
    # extraction. The classifier has already run the passport reader over every
    # booklet page in the bundle, so an MRZ naming the issuing state is evidence
    # already in hand rather than evidence to go and fetch.
    #
    # The answer is carried on the result, not recomputed by the pipeline: it
    # has to hold back the upload *and* keep the record out of the database, and
    # one policy evaluated twice is two chances to disagree with itself.
    whole_text = "\n\n".join(t for t in page_texts if (t or "").strip())
    nationality = rn.detect_resume_nationality(
        whole_text,
        passport_verdicts=rn.passport_verdicts_from(page_texts, result.page_kinds),
    )
    accepted, nationality_reason = rn.should_ingest(nationality)
    if not accepted:
        log.info("Nationality filter: %s", nationality_reason)

    # Only now — with the whole document read, the résumé located and the
    # candidate's nationality established — is anything sent out for a better
    # read, and only the pages that hold it.
    veris_resume_result: dict | None = None
    if (
        accepted
        and data is not None
        and result.is_resume
        and result.resume_pages
        and ocr_pages
    ):
        refined, veris_resume_result = _refine_resume_pages(
            data, filename, page_texts, result.resume_pages
        )
        if refined is not page_texts:
            page_texts = refined
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
        veris_resume_result=veris_resume_result,
        nationality=nationality.as_dict(),
        nationality_accepted=accepted,
        nationality_reason=nationality_reason,
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


def _extract_odt(data: bytes) -> ExtractedDocument:
    """OpenDocument text — a zip whose `content.xml` holds the paragraphs.

    Worth the twenty lines: LibreOffice exports .odt by default, so it is what
    arrives from candidates who do not own Word, and the alternative is telling
    them their application was unreadable.
    """
    import io
    import zipfile
    from xml.etree import ElementTree

    _TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("content.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise TextExtractionError(f"Could not read .odt archive: {exc}") from exc

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise TextExtractionError(f"Could not parse .odt content.xml: {exc}") from exc

    lines: list[str] = []
    # Paragraphs and headings, in document order. `itertext` on each keeps the
    # spans inside a paragraph (bold runs, hyperlinks) joined to their line.
    for node in root.iter():
        if node.tag in (f"{{{_TEXT_NS}}}p", f"{{{_TEXT_NS}}}h"):
            line = "".join(node.itertext()).strip()
            if line:
                lines.append(line)

    text = "\n".join(lines).strip()
    if not text:
        raise TextExtractionError("ODT contained no extractable text.")
    return _classified(_split_pages(text), method="odt")


def _extract_image(data: bytes, filename: str = "") -> ExtractedDocument:
    """A photographed or scanned page, read locally before it is judged.

    An image attachment carries no filename evidence worth anything and no text
    layer to inspect, so it used to be uploaded to find out what it was — which
    meant every signature graphic and every marketing banner that cleared the
    size floor was billed as a résumé extraction. It is read here first; only if
    the content reads as a résumé does `_classified` send it anywhere.
    """
    text = local_ocr.ocr_image_bytes(data).strip()
    if len(text) < settings.ocr_min_text_chars:
        log.warning("Image OCR produced only %d chars for '%s'", len(text), filename)
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
