"""Carve a PDF into page subsets, without ever touching the original bytes.

Both the OCR path and the parser path need "this PDF, but only these pages":
OCR to keep each cloud call small enough to answer inside its timeout, the
parser to keep certificates and passport scans out of the extraction. The
original file is never modified — every function here returns new bytes, and
what gets stored for the recruiter to download is always the untouched upload.
"""
from __future__ import annotations

from typing import Iterable, List

from app.logging_config import get_logger

log = get_logger(__name__)


def page_count(data: bytes) -> int:
    import fitz  # PyMuPDF

    with fitz.open(stream=data, filetype="pdf") as doc:
        return doc.page_count


def subset_pdf(data: bytes, pages: Iterable[int]) -> bytes | None:
    """A new PDF holding only ``pages`` (1-based), or None if that isn't possible.

    Returns None rather than raising when the subset would be the whole file,
    the numbers are out of range, or PyMuPDF cannot read the document — every
    caller's correct response to that is "use the original bytes".
    """
    wanted = sorted({int(p) for p in pages})
    if not wanted:
        return None

    try:
        import fitz  # PyMuPDF

        with fitz.open(stream=data, filetype="pdf") as src:
            total = src.page_count
            if wanted[0] < 1 or wanted[-1] > total:
                log.warning("Page subset %s is out of range for a %d-page PDF", wanted, total)
                return None
            if len(wanted) >= total:
                return None
            out = fitz.open()
            try:
                for number in wanted:
                    out.insert_pdf(src, from_page=number - 1, to_page=number - 1)
                return out.tobytes()
            finally:
                out.close()
    except Exception as exc:  # noqa: BLE001 — never fatal; fall back to the whole file
        log.warning("Could not build a %d-page subset PDF (%s)", len(wanted), exc)
        return None


def chunks(numbers: List[int], size: int) -> List[List[int]]:
    """Split page numbers into consecutive groups of at most ``size``."""
    size = max(1, int(size))
    return [numbers[i:i + size] for i in range(0, len(numbers), size)]
