"""Detect the true file type of an attachment.

We don't trust the extension or the email's declared MIME type alone (both lie).
We sniff the magic bytes with the pure-python ``filetype`` lib and fall back to
the extension. Returns a normalised category the extractor can dispatch on.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import filetype

# Normalised categories the rest of the pipeline understands.
CATEGORY_PDF = "pdf"
CATEGORY_DOCX = "docx"
CATEGORY_DOC = "doc"
CATEGORY_IMAGE = "image"
CATEGORY_TEXT = "text"
CATEGORY_RTF = "rtf"
CATEGORY_UNKNOWN = "unknown"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".gif"}
_IMAGE_MIMES = {
    "image/jpeg", "image/png", "image/tiff", "image/bmp",
    "image/webp", "image/gif",
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

    # --- RTF ---
    if ext == ".rtf" or data[:5] == b"{\\rtf":
        return FileType(CATEGORY_RTF, "application/rtf", ".rtf")

    # --- Images ---
    if mime in _IMAGE_MIMES or ext in _IMAGE_EXTS:
        return FileType(CATEGORY_IMAGE, mime or f"image/{ext.lstrip('.')}", sniffed_ext or ext)

    # --- Plain text ---
    if ext in (".txt", ".text") or mime == "text/plain":
        return FileType(CATEGORY_TEXT, "text/plain", ".txt")

    return FileType(CATEGORY_UNKNOWN, mime or "application/octet-stream", ext or sniffed_ext)


def is_resume_candidate_type(ft: FileType) -> bool:
    """Types we are willing to try to parse as a resume."""
    return ft.category in {
        CATEGORY_PDF, CATEGORY_DOCX, CATEGORY_DOC,
        CATEGORY_IMAGE, CATEGORY_TEXT, CATEGORY_RTF,
    }
