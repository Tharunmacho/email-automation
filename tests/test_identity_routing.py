"""One bundle, three endpoints — and nothing sent to the wrong one.

A candidate posts a single PDF holding a CV, some certificates, an Aadhaar card
and a passport. Each of those has its own Veris endpoint, trained on that
document and no other, so the bundle has to be split before anything is
uploaded:

    résumé pages    -> the résumé endpoint
    Aadhaar pages   -> the Aadhaar endpoint
    Indian passport -> the passport endpoint
    foreign passport-> nowhere: the endpoint is trained on the Indian booklet and
                       answers a foreign one confidently wrong
    everything else -> nowhere at all

Each pass carries only its own pages, and only at a size worth uploading. The
page trim and the byte trim are separate problems: cutting a sixty-page bundle
down to page 54 leaves one sheet at whatever resolution the scanner used, which
was eleven megabytes in the case that prompted this, and that upload was most of
what the job spent its wait budget on.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.db.ingestion_state import MODE_AADHAAR, MODE_PASSPORT
from app.extraction import page_classifier as pc
from app.extraction import pdf_pages
from app.ingestion.multipass import MultipassExtractor
from tests.test_page_classifier import CERTIFICATE_PAGE, RESUME_PAGE, RESUME_PAGE_TWO
from tests.test_resume_location import make_pdf

fitz = pytest.importorskip("fitz", reason="PyMuPDF is needed to build test PDFs")


AADHAAR_PAGE = """Government of India
Unique Identification Authority of India
Aadhaar
Name: Rajesh Kumar
DOB: 12/05/1995
Male
2345 6789 0123
VID : 9101 2345 6789 0123
Mera Aadhaar, Meri Pehchan"""

INDIAN_PASSPORT_PAGE = """REPUBLIC OF INDIA
PASSPORT / PASSEPORT
Type / Type  Country Code / Code du pays  Passport No.
P            IND                          Z1234567
Surname / Nom
KUMAR
Given Name(s) / Prenoms
RAJESH
Nationality / Nationalite  INDIAN
Date of Birth  12/05/1995
Place of Issue  CHENNAI
Date of Expiry  11/05/2032
P<INDKUMAR<<RAJESH<<<<<<<<<<<<<<<<<<<<<<<<<<
Z12345675IND9505124M3205118<<<<<<<<<<<<<<02"""

PAKISTANI_PASSPORT_PAGE = """ISLAMIC REPUBLIC OF PAKISTAN
PASSPORT / PASSEPORT
Type / Type  Country Code / Code du pays  Passport No.
P            PAK                          AB1234567
Surname / Nom
KHAN
Given Name(s) / Prenoms
IMRAN
Nationality / Nationalite  PAKISTANI
Date of Birth  03/08/1990
Place of Issue  LAHORE
Date of Expiry  02/08/2030
P<PAKKHAN<<IMRAN<<<<<<<<<<<<<<<<<<<<<<<<<<<<
AB12345674PAK9008034M3008027<<<<<<<<<<<<<<08"""


def bundle() -> list[str]:
    """The shape a real application arrives in, ID pages behind the CV."""
    return [
        CERTIFICATE_PAGE,          # 1
        RESUME_PAGE,               # 2
        RESUME_PAGE_TWO,           # 3
        CERTIFICATE_PAGE,          # 4
        AADHAAR_PAGE,              # 5
        INDIAN_PASSPORT_PAGE,      # 6
        PAKISTANI_PASSPORT_PAGE,   # 7
    ]


# --------------------------------------------------------------------------- #
#  Routing
# --------------------------------------------------------------------------- #
def test_each_document_goes_to_its_own_endpoint():
    found = pc.classify_multipass(bundle())

    assert found.resume_pages == [2, 3]
    assert found.aadhaar_pages == [5]
    assert found.passport_pages == [6]


def test_the_certificates_are_never_uploaded_anywhere():
    found = pc.classify_multipass(bundle())

    routed = set(found.resume_pages + found.aadhaar_pages + found.passport_pages)
    assert 1 not in routed and 4 not in routed
    assert 1 in found.ignored_pages and 4 in found.ignored_pages


def test_the_identity_pages_are_found_behind_the_resume():
    """The CV is on page 2; the Aadhaar and passport are on 5 and 6. A pass that
    stopped at the résumé — as the old one did — found neither."""
    found = pc.classify_multipass(bundle())

    assert min(found.aadhaar_pages + found.passport_pages) > max(found.resume_pages)


# --------------------------------------------------------------------------- #
#  The nationality gate
# --------------------------------------------------------------------------- #
def test_a_foreign_passport_is_held_back():
    """The endpoint is trained on the Indian booklet. Fed a Pakistani one it does
    not decline — it returns a confidently wrong record, and a wrong passport
    number is found at an embassy counter."""
    found = pc.classify_multipass(bundle())

    assert found.passport_pages == [6], "only the Indian passport may be sent"
    assert found.foreign_passport_pages == [7]
    assert 7 not in found.passport_pages


def test_the_held_back_passport_says_which_country_it_was():
    """'The passport was in the PDF and no record appeared' has to be answerable."""
    found = pc.classify_multipass(bundle())

    verdict = found.passport_nationality[7]
    assert verdict.country == "Pakistan"
    assert "Pakistan" in found.reason
    assert "not sent" in verdict.describe() or "Pakistan" in verdict.describe()


def test_a_bundle_with_only_a_foreign_passport_uploads_nothing():
    extractor = MultipassExtractor(state=object(), client=object())

    result = extractor.run(
        [PAKISTANI_PASSPORT_PAGE], b"",
        message_id="m-1", attachment_id="a-1", filename="passport.pdf",
    )

    assert [p.status for p in result.passes] == ["skipped"]
    assert result.passes[0].mode == MODE_PASSPORT
    assert not result.succeeded


def test_the_filter_can_be_turned_off(monkeypatch):
    """Deliberately reversible — the gate is a policy, not a law of nature."""
    monkeypatch.setattr(settings, "passport_india_only", False)

    found = pc.classify_multipass(bundle())

    assert found.passport_pages == [6, 7]
    assert found.foreign_passport_pages == []


# --------------------------------------------------------------------------- #
#  What actually gets uploaded
# --------------------------------------------------------------------------- #
def test_each_pass_uploads_only_its_own_page():
    data = make_pdf(bundle())

    for mode, pages in ((MODE_AADHAAR, [5]), (MODE_PASSPORT, [6])):
        payload, name = MultipassExtractor._payload_for(data, pages, "bundle.pdf", mode)
        with fitz.open(stream=payload, filetype="pdf") as trimmed:
            assert trimmed.page_count == 1, f"{mode} was sent {trimmed.page_count} pages"
        assert mode in name
        assert len(payload) < len(data)


def test_the_uploaded_page_is_the_right_page():
    """Carving the wrong page would send the passport to the Aadhaar endpoint."""
    data = make_pdf(bundle())

    payload, _name = MultipassExtractor._payload_for(data, [5], "bundle.pdf", MODE_AADHAAR)
    with fitz.open(stream=payload, filetype="pdf") as trimmed:
        assert "Unique Identification Authority" in trimmed[0].get_text()


# --------------------------------------------------------------------------- #
#  The byte trim
# --------------------------------------------------------------------------- #
def _fat_scan() -> bytes:
    """One page of scanned photograph, the way a phone camera produces it."""
    import random

    source = fitz.open()
    page = source.new_page()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 1700, 2200), False)
    random.seed(7)
    # Noise, so the image cannot be compressed away to nothing by the writer.
    pixmap.set_rect(pixmap.irect, (255, 255, 255))
    for _ in range(4000):
        x = random.randint(0, 1600)
        y = random.randint(0, 2100)
        pixmap.set_rect(fitz.IRect(x, y, x + 40, y + 40),
                        (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    page.insert_image(page.rect, pixmap=pixmap)
    data = source.tobytes()
    source.close()
    return data


def test_an_oversized_payload_is_shrunk_before_it_is_uploaded():
    """The page trim removes pages, not resolution. Eleven megabytes for one
    sheet was most of what an identity job spent its wait budget on."""
    fat = _fat_scan()

    smaller = pdf_pages.compact_pdf(fat, max_bytes=200_000, dpi=200)

    assert len(smaller) < len(fat)
    with fitz.open(stream=smaller, filetype="pdf") as doc:
        assert doc.page_count == 1


def test_a_payload_under_the_threshold_is_returned_untouched():
    """Rasterising a PDF that still has a text layer would throw away the most
    accurate reading of it there is."""
    readable = make_pdf([RESUME_PAGE])

    assert pdf_pages.compact_pdf(readable, max_bytes=10_000_000, dpi=300) is readable
    with fitz.open(stream=readable, filetype="pdf") as doc:
        assert "EOT Crane Operator" in doc[0].get_text()


def test_compaction_never_returns_something_larger():
    small = make_pdf([RESUME_PAGE])

    out = pdf_pages.compact_pdf(small, max_bytes=1, dpi=300)

    assert len(out) <= len(small)


# --------------------------------------------------------------------------- #
#  Idempotency keys
# --------------------------------------------------------------------------- #
def test_the_key_names_the_upload_not_just_the_email():
    """The same mail may send different pages, which need distinct upload keys."""
    from app.extraction.jobs import JobContext

    context = JobContext(account_id="cv@adiragroups.com", message_id="2603",
                         attachment_id="2603_5")

    first = context.key_for("resume_parse", "aaaaaaaaaaaaaaaa")
    second = context.key_for("resume_parse", "bbbbbbbbbbbbbbbb")

    assert first != second, "different uploads must not share one key"
    assert "2603" in first, "the key must still name the mail it came from"


def test_the_same_bytes_still_re_attach_to_the_running_job():
    """The other half: a genuine redelivery must not be billed twice."""
    from app.extraction.jobs import JobContext

    context = JobContext(account_id="cv@adiragroups.com", message_id="2603",
                         attachment_id="2603_5")

    assert context.key_for("resume_parse", "same-digest") == context.key_for(
        "resume_parse", "same-digest"
    )


def test_repeated_carves_reuse_the_upload_key_and_preserve_page_content():
    """A failed candidate insert must not buy another OCR of the same pages."""
    from app.extraction.jobs import JobContext, content_key

    data = make_pdf(bundle())
    context = JobContext(account_id="cv@adiragroups.com", message_id="2603",
                         attachment_id="2603_5")

    once = MultipassExtractor._payload_for(data, [2, 3], "bundle.pdf", "resume")[0]
    twice = MultipassExtractor._payload_for(data, [2, 3], "bundle.pdf", "resume")[0]

    assert once == twice
    assert context.key_for("resume", content_key(once)) == context.key_for(
        "resume", content_key(twice)
    )
    with fitz.open(stream=data, filetype="pdf") as source, \
            fitz.open(stream=once, filetype="pdf") as trimmed:
        assert source.page_count == len(bundle())
        assert trimmed.page_count == 2
        for original_index, trimmed_index in ((1, 0), (2, 1)):
            original = source[original_index]
            kept = trimmed[trimmed_index]
            assert kept.rect == original.rect
            assert kept.get_text() == original.get_text()
            assert kept.get_pixmap().samples == original.get_pixmap().samples


def test_changed_pages_produce_a_different_upload_key():
    """Stable serialization still distinguishes a genuinely changed upload."""
    from app.extraction.jobs import JobContext, content_key

    pages = bundle()
    changed_pages = list(pages)
    changed_pages[1] = RESUME_PAGE.replace("EOT Crane Operator", "Forklift Operator")
    assert changed_pages[1] != pages[1]
    context = JobContext(account_id="cv@adiragroups.com", message_id="2603",
                         attachment_id="2603_5")

    original = pdf_pages.subset_pdf(make_pdf(pages), [2, 3])
    changed = pdf_pages.subset_pdf(make_pdf(changed_pages), [2, 3])

    assert original and changed and original != changed
    assert context.key_for("resume", content_key(original)) != context.key_for(
        "resume", content_key(changed)
    )


def test_repeated_compaction_reuses_the_upload_key_and_rendered_content():
    """Shrinking a large scan must be just as stable as carving its pages."""
    from app.extraction.jobs import JobContext, content_key

    data = _fat_scan()
    once = pdf_pages.compact_pdf(data, max_bytes=200_000, dpi=200)
    twice = pdf_pages.compact_pdf(data, max_bytes=200_000, dpi=200)
    context = JobContext(account_id="cv@adiragroups.com", message_id="2603",
                         attachment_id="2603_5")

    assert len(once) < len(data), "this test must exercise actual compaction"
    assert once == twice
    assert context.key_for("resume", content_key(once)) == context.key_for(
        "resume", content_key(twice)
    )
    with fitz.open(stream=data, filetype="pdf") as source, \
            fitz.open(stream=once, filetype="pdf") as first, \
            fitz.open(stream=twice, filetype="pdf") as second:
        assert first.page_count == second.page_count == source.page_count == 1
        assert first[0].rect == second[0].rect == source[0].rect
        assert first[0].get_text() == second[0].get_text() == source[0].get_text() == ""
        assert first[0].get_pixmap().samples == second[0].get_pixmap().samples
