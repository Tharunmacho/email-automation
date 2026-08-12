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


# --------------------------------------------------------------------------- #
#  Types the widened Stage 1 filter now lets through
# --------------------------------------------------------------------------- #
def make_odt(body: str = "<office:text/>") -> bytes:
    """A minimal but conforming ODF package."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", body)
    return buf.getvalue()


def test_odt_by_extension():
    result = ft.detect(make_odt(), "resume.odt")
    assert result.category == ft.CATEGORY_ODT
    assert ft.is_resume_candidate_type(result)


def test_odt_without_a_filename():
    """LibreOffice output attached inline, with the name lost on the way."""
    result = ft.detect(make_odt(), "")
    assert result.category == ft.CATEGORY_ODT


def test_heic_is_an_image():
    """What an iPhone photo of a CV arrives as."""
    result = ft.detect(b"\x00\x00\x00\x18ftypheic", "IMG_4021.heic")
    assert result.category == ft.CATEGORY_IMAGE


def test_a_pdf_mislabelled_bin_is_still_a_pdf():
    """Several webmail clients label an unidentified attachment `.bin`."""
    result = ft.detect(b"%PDF-1.4 resume", "document_1.bin")
    assert result.category == ft.CATEGORY_PDF


# --------------------------------------------------------------------------- #
#  MIME helpers used by the harvesters
# --------------------------------------------------------------------------- #
def test_ext_for_mime_names_an_unnamed_part():
    assert ft.ext_for_mime("application/pdf") == ".pdf"
    assert ft.ext_for_mime("image/jpeg") == ".jpg"
    assert ft.ext_for_mime("application/pdf; name=x") == ".pdf"
    # Unknown types fall back to .bin, which Stage 1 admits on purpose — the
    # magic-byte sniff above is what actually decides.
    assert ft.ext_for_mime("application/x-nonsense") == ".bin"
    assert ft.ext_for_mime("") == ".bin"


def test_is_document_mime_admits_what_might_be_a_resume():
    for mime in (
        "application/pdf", "image/png", "image/heic", "text/plain",
        "application/octet-stream",          # "some binary file", i.e. no claim
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        assert ft.is_document_mime(mime), f"{mime} should be opened"


def test_is_document_mime_refuses_what_cannot_be_one():
    for mime in ("application/zip", "video/mp4", "audio/mpeg", "", "text/calendar"):
        assert not ft.is_document_mime(mime), f"{mime} should not be opened"
