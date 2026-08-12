"""Detect the true file type of an attachment.

We don't trust the extension or the email's declared MIME type alone (both lie).
We sniff the magic bytes with the pure-python ``filetype`` lib and fall back to
the extension. Returns a normalised category the extractor can dispatch on.
"""
from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass

import filetype

# Normalised categories the rest of the pipeline understands.
CATEGORY_PDF = "pdf"
CATEGORY_DOCX = "docx"
CATEGORY_DOC = "doc"
CATEGORY_ODT = "odt"
CATEGORY_IMAGE = "image"
CATEGORY_TEXT = "text"
CATEGORY_RTF = "rtf"
CATEGORY_UNKNOWN = "unknown"

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".gif",
    ".heic", ".heif",
}
_IMAGE_MIMES = {
    "image/jpeg", "image/png", "image/tiff", "image/bmp",
    "image/webp", "image/gif", "image/heic", "image/heif",
}


@dataclass
class FileType:
    category: str
    mime: str
    extension: str


def detect(data: bytes, filename: str = "") -> FileType:
    ext = os.path.splitext(filename)[1].lower()

    kind = filetype.guess(data)          # sniff magic bytes
    mime = kind.mime if kind else ""
    sniffed_ext = f".{kind.extension}" if kind else ""

    # --- PDF ---
    if mime == "application/pdf" or ext == ".pdf" or data[:5] == b"%PDF-":
        return FileType(CATEGORY_PDF, "application/pdf", ".pdf")

    # --- DOCX / legacy DOC ---
    # .docx is a zip; filetype reports it as application/zip or the office mime.
    if mime in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ) or ext == ".docx":
        return FileType(CATEGORY_DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx")
    if mime == "application/msword" or ext == ".doc":
        return FileType(CATEGORY_DOC, "application/msword", ".doc")
    # Zip magic + .docx name → treat as docx.
    if data[:2] == b"PK" and ext == ".docx":
        return FileType(CATEGORY_DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx")

    # --- OpenDocument text ---
    # Also a zip, and `filetype` reports it as such, so the mimetype entry a
    # conforming ODF writer stores first in the archive is the real evidence.
    if (
        mime == "application/vnd.oasis.opendocument.text"
        or ext == ".odt"
        or (data[:2] == b"PK" and b"opendocument.text" in data[:200])
    ):
        return FileType(CATEGORY_ODT, "application/vnd.oasis.opendocument.text", ".odt")

    # --- RTF ---
    if ext == ".rtf" or data[:5] == b"{\\rtf":
        return FileType(CATEGORY_RTF, "application/rtf", ".rtf")

    # --- Images ---
    if mime in _IMAGE_MIMES or ext in IMAGE_EXTENSIONS:
        return FileType(CATEGORY_IMAGE, mime or f"image/{ext.lstrip('.')}", sniffed_ext or ext)

    # --- Plain text ---
    if ext in (".txt", ".text") or mime == "text/plain":
        return FileType(CATEGORY_TEXT, "text/plain", ".txt")

    return FileType(CATEGORY_UNKNOWN, mime or "application/octet-stream", ext or sniffed_ext)


def is_resume_candidate_type(ft: FileType) -> bool:
    """Types we are willing to try to parse as a resume."""
    return ft.category in {
        CATEGORY_PDF, CATEGORY_DOCX, CATEGORY_DOC, CATEGORY_ODT,
        CATEGORY_IMAGE, CATEGORY_TEXT, CATEGORY_RTF,
    }


# --------------------------------------------------------------------------- #
#  MIME helpers, for the harvesters
# --------------------------------------------------------------------------- #
# Used to reconstruct a *display* name for a part that arrived without one, and
# to recognise an unnamed part as worth keeping. The authoritative type check is
# always `detect()` above, on the bytes.
_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/plain": ".txt",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

# Generic types that carry "some binary file" with no further claim. A CV sent
# by a client that could not identify its own attachment arrives as one of
# these, so they are admitted and left for the magic-byte sniff to judge.
_OPAQUE_MIMES = {
    "application/octet-stream",
    "application/binary",
    "binary/octet-stream",
    "application/x-download",
}


def normalize_mime(mime: str) -> str:
    """Bare lowercase type, without parameters (`; charset=…`, `; name=…`)."""
    return (mime or "").split(";")[0].strip().lower()


def ext_for_mime(mime: str) -> str:
    """A plausible extension for a part that arrived without a filename.

    Falls back to `.bin` deliberately: the detector admits that extension, and
    the magic-byte sniff is what actually decides. Refusing an unnamed part is
    how real résumés went missing.
    """
    mime = normalize_mime(mime)
    if mime in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[mime]
    guessed = mimetypes.guess_extension(mime) if mime else None
    return guessed or ".bin"


def is_document_mime(mime: str) -> bool:
    """Whether an unnamed part is worth opening, judged on its declared type."""
    mime = normalize_mime(mime)
    if not mime:
        return False
    return (
        mime in _MIME_EXTENSIONS
        or mime in _OPAQUE_MIMES
        or mime.startswith("image/")
        or mime.startswith("application/vnd.openxmlformats")
        or mime.startswith("application/vnd.oasis.opendocument")
    )
