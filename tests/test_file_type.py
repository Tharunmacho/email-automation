from app.extraction import file_type as ft


def test_pdf_magic_bytes():
    result = ft.detect(b"%PDF-1.7\n...", "whatever.bin")
    assert result.category == ft.CATEGORY_PDF


def test_png_detected():
    png_magic = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    result = ft.detect(png_magic, "photo.png")
    assert result.category == ft.CATEGORY_IMAGE


def test_docx_by_extension():
    # PK zip header + .docx name
    result = ft.detect(b"PK\x03\x04rest", "resume.docx")
    assert result.category == ft.CATEGORY_DOCX


def test_unknown():
    result = ft.detect(b"\x00\x01\x02random", "mystery.xyz")
    assert result.category == ft.CATEGORY_UNKNOWN
    assert not ft.is_resume_candidate_type(result)
