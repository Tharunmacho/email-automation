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

Bounds
------
Every pass in here has a clock on it, because trying harder has to stop
somewhere. A page Tesseract cannot segment does not fail fast — it grinds, for
minutes, at a hundred times what a legible page of the same size costs — and
with nothing bounding it a single unreadable scan parked the ingestion thread
indefinitely. Since the inline poll runs inside the API process whenever no
Celery worker is up, "indefinitely" meant a task stuck PENDING and a dashboard
polling it until someone restarted the container.

So: `ocr_page_timeout_seconds` bounds one Tesseract invocation and
`ocr_document_budget_seconds` bounds the document. Both degrade a page to
*unread* rather than raising — the state every caller here already handles, and
the one that still lets the résumé through on the pages that did read. Nothing
is ever dropped silently; unread pages are named in the log with the setting
that would have bought them.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
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
    #: Degrees the page had to be turned to read it. Carried so the escalated
    #: re-read can apply the answer instead of searching for it again — which
    #: way up a page goes does not change with the DPI it is rendered at.
    angle: int = 0


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


def _page_timeout() -> float:
    """Seconds one Tesseract invocation may take. 0 means "no limit".

    `pytesseract` treats 0 as unlimited too, so this value is passed straight
    through. A read that runs out of time raises, and both call sites already
    treat a raising read as "this page did not answer" — which is the right
    outcome: the pass that follows gets its turn, and if none of them answer the
    page is reported unread rather than silently holding the document open.
    """
    return max(0.0, float(getattr(settings, "ocr_page_timeout_seconds", 0) or 0))


def _tesseract_read(image, psm: int) -> str:
    pytesseract = _tesseract()
    config = f"--oem 1 --psm {psm}"
    try:
        return pytesseract.image_to_string(
            image, lang=settings.ocr_languages, config=config, timeout=_page_timeout()
        )
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
#: Words a document page actually contains, in any orientation-independent
#: sense: ordinary English glue words plus the field labels that appear on the
#: identity papers and certificates this pipeline meets. Small on purpose — it
#: is not a dictionary, it is a test for "did this come out as language at all".
_KNOWN_WORDS = frozenset("""
the of and to in is for on with as by at from or an be this that are was were it
name date birth place issue expiry expire nationality type code country passport
republic india indian father mother spouse guardian legal holder signature
address police station file number sex male female given surname authority
government valid until observations emigration check required
certificate education board school college university degree diploma marks
experience company employee employer salary designation department position
work skills project training course years year month present
""".split())
_WORD_TOKEN = re.compile(r"[A-Za-z]{3,}")


def word_evidence(text: str) -> int:
    """How many real words came out of this read.

    The measure that decides which way up a page goes, and the only one tried
    that is not fooled. Confidence is not enough: on the back page of a real
    passport it preferred 180° by two points, and 180° was upside down. Turned
    the right way that page yields twelve recognisable words and every other
    orientation yields none — a margin wide enough to be a decision rather than
    a coin flip.

    `text_quality` cannot do this job either. It scores the *shape* of the
    output, so a page of scanner speckle that segments into hundreds of little
    tokens beats real text; it rates this bundle's résumé higher upside down
    than the right way up.
    """
    tokens = [t.lower() for t in _WORD_TOKEN.findall(text or "")]
    return sum(1 for t in tokens if t in _KNOWN_WORDS)


#: Wide enough that Tesseract can still segment lines and score its own
#: confidence, narrow enough that four probes cost a fraction of one real read.
#: Measured, not guessed: at 1000 the probe picks the wrong way up and the
#: passport data page is lost again. 1400 and above all decide correctly on
#: the bundle this was built from, and they cost the same; 1600 keeps the
#: margin without paying for it.
_PROBE_WIDTH = 1600


def _thumbnail(image):
    """A small copy, for deciding which way up a page goes."""
    if image.width <= _PROBE_WIDTH:
        return image
    from PIL import Image

    scale = _PROBE_WIDTH / image.width
    return image.resize((_PROBE_WIDTH, max(1, int(image.height * scale))), Image.LANCZOS)


def _scored_read(image, psm: int) -> "Tuple[str, float]":
    """One read, with Tesseract's own mean confidence in it.

    Text and confidence come out of the same pass because they cost the same
    pass. Measuring orientation with `image_to_data` and then re-reading the
    winner with `image_to_string` runs the engine over every page twice, which
    is most of a minute on a thirty-page bundle for an answer already in hand.

    Not the same judgement as `text_quality`, and the difference is the point.
    `text_quality` asks "does this output look like language", which noise can
    fake — a page of scanner speckle segments into hundreds of plausible little
    tokens and scores well. Confidence asks Tesseract how sure it was of each
    character it committed to, and it is not fooled the same way.
    """
    pytesseract = _tesseract()
    try:
        data = pytesseract.image_to_data(
            image,
            config=f"--oem 1 --psm {psm}",
            output_type=pytesseract.Output.DICT,
            timeout=_page_timeout(),
        )
    except Exception:  # noqa: BLE001 — no confidence is not an orientation verdict
        return "", 0.0

    lines: List[str] = []
    current: List[str] = []
    key = None
    scores: List[int] = []
    for index, word in enumerate(data["text"]):
        if not str(word).strip():
            continue
        here = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        if key is not None and here != key:
            lines.append(" ".join(current))
            current = []
        key = here
        current.append(str(word))
        confidence = int(data["conf"][index])
        if confidence >= 0:
            scores.append(confidence)
    if current:
        lines.append(" ".join(current))

    return "\n".join(lines), (sum(scores) / len(scores) if scores else 0.0)


def _upright(prepared, original, page_number: int):
    """The page the right way up, and how far it had to be turned to get there.

    A booklet does not lie flat, so its pages come off the scanner on their
    side — and Tesseract reading a sideways page does not fail loudly. It
    returns *confident nonsense*: the data page of a real Indian passport read
    as "os | ne ee Ue rz sae =, 3 o o ry c =z 735", scored 24.9 on quality,
    cleared the quality floor, and was then judged — reasonably, on the evidence
    it had — to contain no passport at all. Turned a quarter turn the same page
    reads "Type / Code / Nationality" and carries the full machine-readable zone.

    Orientation is chosen on `word_evidence` and never on `text_quality`,
    because quality picks the wrong way up: it scores this bundle's résumé
    higher upside down than the right way up, and its certificates too. Counting
    words a document page actually contains gets every one of them right.

    Recognisable words also decide whether to look at all. A page that reads as
    language upright is left as it is, so the common case pays one small probe
    and no rotation at all; only a page that comes back with nothing legible on
    it is turned.
    """
    if not settings.ocr_detect_rotation:
        return prepared, 0

    # Probed small.
    #
    # Which way up a page goes is a coarse property — it is legible at a
    # fraction of the resolution the words themselves need — so all four probes
    # run on a thumbnail. Probing at full resolution means four real reads of
    # every doubtful page, which took one thirty-page bundle from 28 seconds to
    # 63 for the same answer.
    probe = _thumbnail(prepared)
    text, _confidence = _scored_read(probe, settings.ocr_psm)
    words = word_evidence(text)
    if words >= settings.ocr_rotation_word_floor:
        return prepared, 0

    # A turn has to *prove* itself, in words, or the page stays as it is.
    #
    # Confidence must not be allowed to break the tie. On a page of visa stamps
    # where no orientation yields a single recognisable word, confidence still
    # names a winner — by half a point, on noise — and turning that page threw
    # away a legible "KINGDOM OF CAMBODIA" that the upright read had. A rotation
    # that cannot show more words than upright is not evidence of anything.
    # Each candidate is turned from the *prepared* page, not from the original.
    #
    # `prepared` is already greyscale and contrast-stretched, and a quarter turn
    # is lossless on it — PIL transposes exact multiples of 90 rather than
    # resampling — so re-deriving it from `original` cost a full-resolution RGB
    # rotate, a greyscale conversion and an autocontrast pass per angle, three
    # times per doubtful page, for pixels identical to these.
    #
    # The thumbnail is taken *after* the turn and never before it. `_thumbnail`
    # fits a width, so thumbnailing first and rotating second hands a quarter
    # turn a probe as wide as the page is tall — on a landscape scan that is
    # 1131px against 1600px, and it cost this bundle's sideways page nine of
    # the ten recognisable words the decision is made on. The orientation
    # verdict has to be taken at the resolution it was calibrated at.
    best_angle, best_words = 0, words
    for angle in (90, 270, 180):
        try:
            turned, _confidence = _scored_read(
                _thumbnail(prepared.rotate(angle, expand=True)), settings.ocr_psm
            )
        except Exception:  # noqa: BLE001 — a page that will not turn is not fatal
            break
        turned_words = word_evidence(turned)
        if turned_words > best_words:
            best_angle, best_words = angle, turned_words

    if not best_angle:
        return prepared, 0

    # Only now, and only once: the winner at full resolution.
    try:
        upright = _prepare(original.rotate(best_angle, expand=True))
    except Exception:  # noqa: BLE001
        return prepared, 0

    log.info(
        "Page %d was scanned sideways; read at %d° instead — %d recognisable "
        "word(s) there against %d upright",
        page_number, best_angle, best_words, words,
    )
    return upright, best_angle


# --------------------------------------------------------------------------- #
def read_image(
    image, *, dpi: int, page_number: int = 1, known_angle: "int | None" = None,
) -> PageRead:
    """Read one already-rendered page, trying harder while the result is poor.

    The passes are ordered by cost. Most pages are answered by the first one;
    the rest are the reason the others exist.

    ``known_angle`` short-circuits the orientation search. Which way up a page
    goes is a property of the page, not of the DPI it was rendered at, so the
    escalated re-read is told the answer rather than made to find it again —
    four Tesseract probes and a full-size rotation per escalated page, for
    something the base pass already established.
    """
    if known_angle is None:
        prepared, turned_by = _upright(_prepare(image), image, page_number)
    else:
        turned_by = known_angle
        prepared = _prepare(image.rotate(turned_by, expand=True) if turned_by else image)

    best_text = _tesseract_read(prepared, settings.ocr_psm)
    best_quality = text_quality(best_text)
    best_engine = f"tesseract:psm{settings.ocr_psm}"
    if turned_by:
        best_engine += f"+rot{turned_by}"

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
        angle=turned_by,
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
            retry = read_image(
                bigger, dpi=settings.ocr_dpi * 2, known_angle=read.angle
            )
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


def _read_pdf_page(
    data: bytes, page_number: int, deadline: "float | None" = None,
) -> PageRead:
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

        # Escalation is the expensive half of the page. A document that has
        # already spent its budget keeps the read it has rather than starting a
        # second, larger one it cannot finish.
        out_of_time = deadline is not None and time.monotonic() >= deadline
        if out_of_time and read.quality < settings.ocr_page_quality_floor:
            log.info(
                "Page %d read poorly (quality %.2f) but the document budget is "
                "spent; not re-reading at %ddpi",
                page_number, read.quality, settings.ocr_escalate_dpi,
            )

        if (
            not out_of_time
            and read.quality < settings.ocr_page_quality_floor
            and settings.ocr_escalate_dpi > dpi
        ):
            # More pixels per character. This is what rescues a faint fax or a
            # small-print experience letter, and it is why no page is written
            # off after one attempt.
            bigger_dpi = settings.ocr_escalate_dpi
            bigger, _raw = _render(page, bigger_dpi)
            retry = read_image(
                bigger, dpi=bigger_dpi, page_number=page_number, known_angle=read.angle,
            )
            if retry.quality > read.quality:
                log.info(
                    "Page %d re-read at %ddpi: quality %.2f -> %.2f",
                    page_number, bigger_dpi, read.quality, retry.quality,
                )
                read = retry

    _remember(key, read.text)
    return read


def available_cpus() -> int:
    """Cores this *process* may actually use — not the ones the machine has.

    `os.cpu_count()` reports the host's cores and knows nothing about cgroups,
    so inside a container it answers with the whole machine. On a Dokploy host
    with eight cores and a one-CPU quota it says 8, and every caller that sizes
    a worker pool from it starts eight Tesseract processes to share one CPU.
    That is worse than starting two: the work does not go faster, the pages
    contend, and each concurrent page is holding a 300-DPI raster (~26 MB for
    A4) plus Tesseract's own working set — so a container that would have been
    merely slow becomes slow *and* memory-pressured.

    Three sources, smallest wins, each ignored where it does not apply:

    * the cgroup v2 CPU quota (`cpu.max`), which is what a container limit is;
    * the cgroup v1 equivalent, for older hosts;
    * the scheduler affinity mask, which catches a pinned process.
    """
    limits = [os.cpu_count() or 4]

    # cgroup v2: "<quota> <period>", or "max <period>" when unlimited.
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            limits.append(max(1, int(float(quota) / float(period))))
    except Exception:  # noqa: BLE001 — not a cgroup v2 host, or not readable
        pass

    # cgroup v1: quota and period in separate files, -1 meaning unlimited.
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            limits.append(max(1, quota // period))
    except Exception:  # noqa: BLE001 — not a cgroup v1 host, or not readable
        pass

    # Affinity: Linux only, and it respects taskset/CPU pinning.
    try:
        limits.append(len(os.sched_getaffinity(0)))
    except AttributeError:  # not on Windows or macOS
        pass

    return max(1, min(limits))


def local_worker_count() -> int:
    """How many pages to read at once.

    `OCR_LOCAL_WORKERS` was a flat 4, which left most of a modern host idle: a
    30-page bundle spent 28 seconds in Tesseract on an 8-core machine that could
    have done it in under half that. Zero (the new default) means "size it from
    the host". `pytesseract` shells out, so the GIL is not the limit — the cores
    are — and the cap keeps a big scan from starving the web workers sharing the
    process.

    Sized from what this process may *use*, not what the machine has — see
    `available_cpus`. The floor is 2 rather than 1 because a page read is not
    pure CPU: it waits on rasterisation and on a subprocess, so a second worker
    still earns its place on a one-CPU box. Measured on an 11 MB, 28-page scan:
    1 worker 7.06 s/page, 2 workers 4.37, 10 workers 2.71.
    """
    configured = int(getattr(settings, "ocr_local_workers", 0) or 0)
    if configured > 0:
        return configured
    return max(2, min(8, available_cpus()))


def ocr_pdf_page_reads(
    data: bytes,
    dpi: int | None = None,
    pages: "set[int] | None" = None,
    filename: str = "",
) -> Dict[int, PageRead]:
    """``{page number: PageRead}`` for every page asked for, read in parallel.

    The full read is returned, not just the text, because *how badly* a page
    read is the only signal there is that the text is not to be trusted. A
    passport photographed under a desk lamp comes back as sixty characters of
    noise, which scores as confidently-not-an-ID rather than as unreadable —
    and the caller cannot tell those apart from the text alone.

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

    workers = max(1, min(local_worker_count(), len(wanted)))
    out: Dict[int, PageRead] = {}

    # A wall clock on the whole document.
    #
    # Every pass in here is bounded now, but "bounded" times pages times retries
    # is still unbounded in practice, and the caller that suffers for it is the
    # inline poll: it runs on a thread inside the API process, so a document
    # that will not read leaves its task PENDING and the dashboard polling that
    # task ID until the container is restarted. Pages that do not fit the budget
    # come back unread, which is a state every caller already handles — the
    # text-layer read stands and the pages are named below.
    budget = max(0.0, float(getattr(settings, "ocr_document_budget_seconds", 0) or 0))
    deadline = time.monotonic() + budget if budget else None
    skipped: List[int] = []
    skipped_lock = threading.Lock()

    def _one(number: int) -> Tuple[int, PageRead | None]:
        if deadline is not None and time.monotonic() >= deadline:
            with skipped_lock:
                skipped.append(number)
            return number, None
        try:
            return number, _read_pdf_page(data, number, deadline)
        except TextExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad page is not the document
            log.warning("Local OCR failed on page %d of '%s': %s", number, filename, exc)
            return number, None

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for number, read in pool.map(_one, wanted):
            if read is not None:
                out[number] = read

    if skipped:
        log.warning(
            "Local OCR ran out of its %.0fs budget on '%s'; page(s) %s were not read "
            "(raise OCR_DOCUMENT_BUDGET_SECONDS)",
            budget, filename or "attachment", sorted(skipped),
        )

    read_chars = sum(len(r.text) for r in out.values())
    log.info(
        "Local OCR read %d/%d page(s) of '%s' across %d worker(s): %d chars in %.1fs",
        len(out), total, filename or "attachment", workers, read_chars,
        time.monotonic() - started,
    )
    # Named explicitly, because "this page was read badly" is the whole basis on
    # which it is later offered to the cloud reader. A page that fails here and
    # is never mentioned again is how a passport goes missing.
    weak = sorted(n for n, r in out.items() if r.quality < settings.ocr_page_quality_floor)
    if weak:
        log.info(
            "Local OCR could not bring page(s) %s of '%s' up to the quality floor (%.1f)",
            weak, filename or "attachment", settings.ocr_page_quality_floor,
        )
    return out


def ocr_pdf_page_texts(
    data: bytes,
    dpi: int | None = None,
    pages: "set[int] | None" = None,
    filename: str = "",
) -> Dict[int, str]:
    """``{page number: text}``, for callers with no use for the read quality."""
    reads = ocr_pdf_page_reads(data, dpi=dpi, pages=pages, filename=filename)
    return {number: read.text for number, read in reads.items()}


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
        "workers": local_worker_count(),
        "secondary_engine": "rapidocr" if _secondary() is not None else "none",
        "cached_pages": len(_CACHE),
    }
