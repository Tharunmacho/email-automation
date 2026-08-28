"""The local reader: every page of a document, read here rather than bought.

Why this module exists
----------------------
The pipeline used to find out what a file *was* by sending it to the Veris
résumé endpoint and reading the answer. That is backwards, and it is what put
bank statements, hall tickets and marketing PDFs through a paid résumé
extraction: the upload happened before anything knew what the document held.
Worse, it was also incomplete — the scan stopped as soon as something scored as
a CV, so a passport on page 30 of a 40-page bundle was never looked at.

So the order is inverted. This module reads *the whole document* locally, at
whatever cost in CPU that takes, and only then does the classifier decide which
pages are a résumé, which are an Aadhaar card and which are a passport. Nothing
leaves the building until that question has been answered from the page's own
content, and each document then goes to the endpoint trained for it.

What "well read" means here
---------------------------
Tesseract's default settings are tuned for clean scans of printed prose, which
is not what arrives. A phone photograph of a CV under a desk lamp, a fax-quality
experience letter, a passport page shot at an angle — each fails differently, so
each page is read more than once and the best answer is kept:

* **Preprocessing.** Greyscale, autocontrast, and an upscale for anything that
  arrives below the resolution Tesseract needs to segment characters at all. A
  1000-pixel-wide phone photo of a CV yields almost nothing raw and reads
  cleanly at 2x.
* **Several page-segmentation modes.** ``--psm 6`` (a uniform block) and
  ``--psm 4`` (columns) disagree constantly on résumé layouts, and which one is
  right depends on the page, not the document. Both are tried on pages that read
  poorly the first time.
* **DPI escalation.** A page that comes back nearly empty is re-rendered larger
  and read again. This is the single biggest win on scanned bundles, and it is
  why the pass is not simply "render at 300 and hope".
* **A second engine, when one is installed.** RapidOCR/PaddleOCR handle rotated
  and low-contrast text that Tesseract gives up on. Optional by design: this
  module must work on a bare host with nothing but Tesseract.

Scale
-----
Pages are independent, so they are read in parallel, and `pytesseract` shells
out — the GIL is released for the part that takes the time. Results are cached
on the rendered page's own bytes, so re-processing the same mail (a retry, a
redelivery, a reconciler pass) costs nothing the second time.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import re
import shutil
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.exceptions import TextExtractionError
from app.logging_config import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
#  Reading quality
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def text_quality(text: str) -> float:
    """How much of this looks like language rather than OCR noise.

    Not a confidence score from the engine — a judgement about the *output*,
    which is what has to be compared when two passes over the same page
    disagree. Tesseract will happily return three hundred characters of
    punctuation soup at high confidence; that has to lose to eighty characters
    of real words.
    """
    if not text:
        return 0.0
    stripped = text.strip()
    if not stripped:
        return 0.0

    words = _WORD_RE.findall(stripped)
    alnum = len(_ALNUM_RE.findall(stripped))
    # The ratio of readable characters to everything else. Noise is dominated by
    # punctuation and single stray letters, so this is what separates them.
    density = alnum / max(1, len(stripped))
    return len(words) * density


@dataclass
class PageRead:
    """One page, and how it came to be read that way."""

    page_number: int
    text: str
    dpi: int
    engine: str
    quality: float


# --------------------------------------------------------------------------- #
#  Engines
# --------------------------------------------------------------------------- #
def _tesseract():
    import pytesseract

    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
        return pytesseract
    if shutil.which("tesseract") is None:
        raise TextExtractionError(
            "Tesseract OCR is required to read scanned documents but was not found. "
            "Install it (https://github.com/tesseract-ocr/tesseract) and either add it "
            "to PATH or set TESSERACT_CMD in your .env."
        )
    return pytesseract


_SECOND_ENGINE_LOCK = threading.Lock()
_second_engine: object | None = None
_second_engine_tried = False


def _secondary():
    """RapidOCR, if this host has it. Loaded once, on first use.

    Deliberately optional and deliberately lazy: the model load costs a second
    or two and several hundred megabytes, which a deployment that never meets a
    difficult scan should not pay at import time.
    """
    global _second_engine, _second_engine_tried
    if not settings.ocr_secondary_engine_enabled:
        return None
    with _SECOND_ENGINE_LOCK:
        if _second_engine_tried:
            return _second_engine
        _second_engine_tried = True
        try:
            from rapidocr_onnxruntime import RapidOCR

            _second_engine = RapidOCR()
            log.info("Secondary OCR engine available: RapidOCR")
        except Exception as exc:  # noqa: BLE001 — absence is the normal case
            log.debug("No secondary OCR engine installed (%s)", exc)
            _second_engine = None
        return _second_engine


def _read_with_secondary(image) -> str:
    engine = _secondary()
    if engine is None:
        return ""
    try:
        import numpy as np

        result, _elapsed = engine(np.array(image))
        if not result:
            return ""
        return "\n".join(line[1] for line in result if len(line) > 1)
    except Exception as exc:  # noqa: BLE001
        log.debug("Secondary OCR engine failed: %s", exc)
        return ""


# --------------------------------------------------------------------------- #
#  Image preparation
# --------------------------------------------------------------------------- #
#: Below this width Tesseract cannot segment characters reliably, whatever the
#: page actually contains. Phone photographs routinely arrive under it.
_MIN_READABLE_WIDTH = 1600


def _prepare(image):
    """Greyscale, contrast-stretch, and upscale a page that is too small to read."""
    from PIL import Image, ImageOps

    prepared = image.convert("L") if image.mode not in ("L", "1") else image
    prepared = ImageOps.autocontrast(prepared)

    if prepared.width < _MIN_READABLE_WIDTH:
        scale = min(3.0, _MIN_READABLE_WIDTH / max(1, prepared.width))
        prepared = prepared.resize(
            (int(prepared.width * scale), int(prepared.height * scale)),
            Image.LANCZOS,
        )
    return prepared


def _tesseract_read(image, psm: int) -> str:
    pytesseract = _tesseract()
    config = f"--oem 1 --psm {psm}"
    try:
        return pytesseract.image_to_string(image, lang=settings.ocr_languages, config=config)
    except Exception as exc:  # noqa: BLE001
        log.debug("Tesseract psm=%d failed: %s", psm, exc)
        return ""


# --------------------------------------------------------------------------- #
#  The per-page cache
# --------------------------------------------------------------------------- #
_CACHE: Dict[str, str] = {}
_CACHE_LOCK = threading.Lock()
#: Bounded so a long-lived worker cannot grow without limit. Pages are large and
#: the reuse that matters is within one document and its immediate retries.
_CACHE_LIMIT = 512


def _cache_key(payload: bytes, dpi: int) -> str:
    return f"{hashlib.sha256(payload).hexdigest()}:{dpi}:{settings.ocr_languages}"


def _cached(key: str) -> Optional[str]:
    with _CACHE_LOCK:
        return _CACHE.get(key)


def _remember(key: str, text: str) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_LIMIT:
            # Oldest first. Plain insertion order is enough: this exists to stop
            # unbounded growth, not to be an optimal eviction policy.
            for stale in list(_CACHE)[: _CACHE_LIMIT // 4]:
                _CACHE.pop(stale, None)
        _CACHE[key] = text


# --------------------------------------------------------------------------- #
#  Reading one image
# --------------------------------------------------------------------------- #
def read_image(image, *, dpi: int, page_number: int = 1, escalated: bool = False) -> PageRead:
    """Read one already-rendered page, trying harder while the result is poor.

    The passes are ordered by cost. Most pages are answered by the first one;
    the rest are the reason the others exist.
    """
    prepared = _prepare(image)

    best_text = _tesseract_read(prepared, settings.ocr_psm)
    best_quality = text_quality(best_text)
    best_engine = f"tesseract:psm{settings.ocr_psm}"

    if best_quality < settings.ocr_page_quality_floor:
        # A different segmentation, not a different picture. Columnar résumés
        # and single-column letters need opposite assumptions, and the file
        # gives no clue which it is.
        for psm in settings.ocr_alternate_psms:
            if psm == settings.ocr_psm:
                continue
            text = _tesseract_read(prepared, psm)
            quality = text_quality(text)
            if quality > best_quality:
                best_text, best_quality, best_engine = text, quality, f"tesseract:psm{psm}"
            if best_quality >= settings.ocr_page_quality_floor:
                break

    if best_quality < settings.ocr_page_quality_floor:
        text = _read_with_secondary(prepared)
        quality = text_quality(text)
        if quality > best_quality:
            best_text, best_quality, best_engine = text, quality, "rapidocr"

    return PageRead(
        page_number=page_number,
        text=best_text or "",
        dpi=dpi,
        engine=best_engine,
        quality=round(best_quality, 2),
    )


def ocr_image_bytes(data: bytes) -> str:
    """Read a standalone image attachment — a photographed or scanned CV."""
    from PIL import Image

    key = _cache_key(data, settings.ocr_dpi)
    cached = _cached(key)
    if cached is not None:
        return cached

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        read = read_image(image, dpi=settings.ocr_dpi)
        if read.quality < settings.ocr_page_quality_floor:
            # The image is what it is — there is no higher DPI to render it at —
            # but an upscale gives Tesseract more pixels per character, which is
            # the same lever escalation pulls on a PDF.
            from PIL import Image as _Image

            bigger = image.convert("L").resize(
                (image.width * 2, image.height * 2), _Image.LANCZOS
            )
            retry = read_image(bigger, dpi=settings.ocr_dpi * 2, escalated=True)
            if retry.quality > read.quality:
                read = retry

    _remember(key, read.text)
    log.info(
        "Local OCR read an image: %d chars, quality=%.2f via %s",
        len(read.text), read.quality, read.engine,
    )
    return read.text


# --------------------------------------------------------------------------- #
#  Reading a PDF
# --------------------------------------------------------------------------- #
def _render(page, dpi: int):
    import fitz  # PyMuPDF
    from PIL import Image

    zoom = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples), pixmap.tobytes()


def _read_pdf_page(data: bytes, page_number: int) -> PageRead:
    """One page of a PDF, rendered and read, escalating if it comes back thin.

    Opening the document per page rather than sharing one handle is deliberate:
    PyMuPDF documents are not safe to use from several threads at once, and the
    open is cheap next to the OCR that follows it.
    """
    import fitz  # PyMuPDF

    dpi = settings.ocr_dpi
    with fitz.open(stream=data, filetype="pdf") as doc:
        page = doc[page_number - 1]
        image, raw = _render(page, dpi)
        key = _cache_key(raw, dpi)
        cached = _cached(key)
        if cached is not None:
            return PageRead(page_number, cached, dpi, "cache", text_quality(cached))

        read = read_image(image, dpi=dpi, page_number=page_number)

        if read.quality < settings.ocr_page_quality_floor and settings.ocr_escalate_dpi > dpi:
            # More pixels per character. This is what rescues a faint fax or a
            # small-print experience letter, and it is why no page is written
            # off after one attempt.
            bigger_dpi = settings.ocr_escalate_dpi
            bigger, _raw = _render(page, bigger_dpi)
            retry = read_image(bigger, dpi=bigger_dpi, page_number=page_number, escalated=True)
            if retry.quality > read.quality:
                log.info(
                    "Page %d re-read at %ddpi: quality %.2f -> %.2f",
                    page_number, bigger_dpi, read.quality, retry.quality,
                )
                read = retry

    _remember(key, read.text)
    return read


def ocr_pdf_page_texts(
    data: bytes,
    dpi: int | None = None,
    pages: "set[int] | None" = None,
    filename: str = "",
) -> Dict[int, str]:
    """``{page number: text}`` for every page asked for, read in parallel.

    ``dpi`` is accepted for callers that predate the escalation logic and is
    otherwise ignored — the DPI a page needs is decided per page, from how well
    it reads, not passed in by a caller that cannot know.
    """
    import fitz  # PyMuPDF

    with fitz.open(stream=data, filetype="pdf") as doc:
        total = doc.page_count

    wanted = sorted(pages) if pages is not None else list(range(1, total + 1))
    wanted = [n for n in wanted if 1 <= n <= total]
    if not wanted:
        return {}

    ceiling = max(1, settings.ocr_max_pages)
    if len(wanted) > ceiling:
        log.warning(
            "'%s' has %d page(s) to read but the safety ceiling is %d; pages %s "
            "will not be read (raise OCR_MAX_PAGES)",
            filename or "attachment", len(wanted), ceiling, wanted[ceiling:],
        )
        wanted = wanted[:ceiling]

    workers = max(1, min(settings.ocr_local_workers, len(wanted)))
    out: Dict[int, str] = {}

    def _one(number: int) -> Tuple[int, PageRead | None]:
        try:
            return number, _read_pdf_page(data, number)
        except TextExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad page is not the document
            log.warning("Local OCR failed on page %d of '%s': %s", number, filename, exc)
            return number, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for number, read in pool.map(_one, wanted):
            if read is not None:
                out[number] = read.text

    read_chars = sum(len(t) for t in out.values())
    log.info(
        "Local OCR read %d/%d page(s) of '%s' across %d worker(s): %d chars",
        len(out), total, filename or "attachment", workers, read_chars,
    )
    return out


def ocr_pdf_pages(data: bytes, dpi: int | None = None) -> str:
    """Every page of a PDF as one string, in page order."""
    texts = ocr_pdf_page_texts(data, dpi=dpi)
    return "\n".join(texts[n] for n in sorted(texts))


def engine_report() -> Dict[str, object]:
    """What this host can actually read with. Surfaced by the ops endpoint."""
    tesseract_version = ""
    try:
        pytesseract = _tesseract()
        tesseract_version = str(pytesseract.get_tesseract_version())
    except Exception as exc:  # noqa: BLE001
        tesseract_version = f"unavailable: {exc}"

    return {
        "tesseract": tesseract_version,
        "languages": settings.ocr_languages,
        "base_dpi": settings.ocr_dpi,
        "escalate_dpi": settings.ocr_escalate_dpi,
        "workers": settings.ocr_local_workers,
        "secondary_engine": "rapidocr" if _secondary() is not None else "none",
        "cached_pages": len(_CACHE),
    }
