"""The passport that was in the bundle all along.

A real 32-page application bundle went through the pipeline and its passport was
never extracted. Three separate things had to be wrong for that to happen, and
each one alone would have been enough to hide it. All three are pinned here,
because each is invisible from the outside: the pipeline reported success, the
candidate was created, and nothing anywhere said a passport had been missed.

The page numbers and the OCR fragments below are from that bundle.

The identity values below are redacted. The OCR *garbling* is preserved exactly
as the reader produced it — that is what these tests are about — but the
passport number, dates, names and address are replaced with placeholders. A
real person's passport does not belong in a test fixture.
"""
from __future__ import annotations

import pytest

from app.extraction import page_classifier as pc

# Page 4: the request page, printed inside every Indian passport. It read at ten
# times the OCR quality floor — this is genuinely what the reader returned.
REQUEST_PAGE = (
    "a 7 ees\n\nUla aed a NE i,\nSear, ra me sot === REPUBLIC OF INDIA i\n"
    "Pe Sartor ote ot we ore A Reh wee ok GUT j in Fast s\n\n"
    "eee. THESE ARE TO REQUEST AND REQUIRE IN THE NAME By RNG\n\n"
    "Ey) 232) OF THE PRESIDENT OF THE REPUBLIC OF INDIA ALL THOSE F 2] SRE r7\n\n"
    "ee ae TO WHOM IT MAY C"
)

# Page 5, the data page, as it read *turned a quarter turn*. Upright the same
# page returned "os | ne ee Ue rz sae =, 3 o o ry c =z 735" — see below.
DATA_PAGE_TURNED = (
    # Verbatim from the reader, turned. Upright the same page is below.
    "aa yisa\nee\nAl PATS = So age\nELUDAMM Agape EEL I Type iE | Code RAM | Naticonnsity urantd 4.1 Pi No,\nWOM Oo 5 Baapest No, _\nP IND sedi\n(NON Z9999999\nz FFF Sumame\nSURNAME iia\nGIVEN NAME saa ie =\nTHAR | Date of Bath Ra Sex =o\n01/01/1990 (_ ao\nFT TAFT! Place of faeth . a a\nCHENNAI,TAMIL NADU = = \u2018e\nwr BE BUF Place of tue eae ; ?\nard 24 & PR Date of sve arihe Bt Pre / Date of Expiry\n= 01/01/2020 01/01/2030\nP<INDSURNAME<K<UDAYA<SANKAR<<<<<<<<<<< <<< <<\nY\u00a56380814<21ND9001014M39123199999999999999<44 ;\nLRP PDR tA iach\n"
)

DATA_PAGE_UPRIGHT = (
    "os | ne ee Ue\nrz sae =,\n3 o o ry c =z 735 | Sly!\nFi nd it SSZeE5 57 >42 ty\n"
    "3 WA 85 suRsEsiz z* Pn t\nF Ww wf fesiaas ze = o | oh 3\nE S52 4 im me it"
)

# Page 21 — fourteen pages behind the data page, with nothing on it that says
# which country issued it.
BACK_PAGE = (
    # Verbatim, read at 270deg. It names the holder's parents and no country.
    ": aaa \u2014 pt heey cena ia apples shboegeemne ote ocabbwal ina\n; ta dos VASA\nDene nerrreemeen-eemansaturnereatnnnttienttnnere-tttentn arp $\u2014snere-etstnweenetistenyen ene et\u2014enaseas\u2014\u2014wirereeeg\ni] H i cosa\nUNDER LUA\nPray argh seftrepra: ot 4PL/ Name of Fathes ! Legel Guardian Z9999999 i\nSURNAME ins\nTRG By AR Name of Mother iene\nPARENT NAME * ie\nGR ar FH 1 FFT Name of Spouse a\n. SPOUSE NAME cae\na Address -[Se:\n| NO 1 SAMPLE STREET 5 ADO.\n: | SAMPLE NAGAR,CHENNAI Ad og\n| PIN:600001, TAMIL NADU, INDIA Bh\nGe rend ary 1, afte ts art OP Rae a TFT Old Passport No. with Dase and Place of issue = 4\nU0000000 01/01/2015 CHENNAL = doo\nOQYa 5! Fle Na,\nMA0000000000000 - OLD PASSPORT REPORTED LOST ;\n"
)

CV_PAGE = (
    "CURRICULUM VITAE\nSample Candidate\ncandidate@example.com  +91 90000 00000\n"
    "OBJECTIVE\nTo pursue a dynamic and challenging career.\n"
    "WORK EXPERIENCE\nEOT Crane Operator, Jan 2019 - Present\n"
    "EDUCATION\nDiploma in Mechanical Engineering, 2015 - 2018\nSKILLS\nRigging, welding"
)
CERTIFICATE_PAGE = (
    "CENTRAL BOARD OF SECONDARY EDUCATION\nThis is to certify that SURNAME "
    "GIVEN NAME has successfully completed the examination held in 2007.\n"
    "Serial No. SGD 000000"
)


def test_the_request_page_identifies_a_passport():
    """"REPUBLIC OF INDIA" plus the President's request is a passport, full stop.

    This page read perfectly and still scored 0.0 for every kind, because the
    only marker that mentioned "republic of india" also demanded the literal
    word "passport" within 120 characters — and that word is on the cover, a
    different page, where OCR had rendered it "PAS 5 sf CO @ tT".
    """
    scores = pc.id_document_scores(REQUEST_PAGE)

    assert scores[pc.PASSPORT] >= pc.ID_SEED_SCORE, (
        f"the request page of an Indian passport scored {scores[pc.PASSPORT]}"
    )


def test_a_cv_and_a_certificate_are_not_passports():
    """The markers above must not have made everything a passport."""
    assert pc.id_document_scores(CV_PAGE)[pc.PASSPORT] < pc.ID_SEED_SCORE
    assert pc.id_document_scores(CERTIFICATE_PAGE)[pc.PASSPORT] < pc.ID_SEED_SCORE


def test_the_data_page_is_worthless_upright_and_conclusive_turned():
    """Why orientation is not a detail.

    The upright read is not *bad* — it is confident nonsense. It clears the
    quality floor, so no amount of "re-read the pages that failed" would ever
    have reached it. Only turning the page does.
    """
    from app.extraction import local_ocr

    assert local_ocr.text_quality(DATA_PAGE_UPRIGHT) > settings_floor(), (
        "the upright read cleared the quality floor — which is the whole problem"
    )
    assert pc.id_document_scores(DATA_PAGE_UPRIGHT)[pc.PASSPORT] == 0.0
    assert pc.id_document_scores(DATA_PAGE_TURNED)[pc.PASSPORT] >= pc.ID_SEED_SCORE


def settings_floor() -> float:
    from app.config import settings

    return settings.ocr_page_quality_floor


def test_a_page_with_no_nationality_takes_the_bundle_s():
    """Nationality belongs to the passport, not to the page.

    Only the data page carries a nationality field. The back page of the same
    booklet lists the holder's parents and says nothing about India, so the
    filter that exists to hold back *foreign* passports read "undetermined" and
    dropped a genuine Indian one — fourteen pages from the data page it had
    already confirmed as India.
    """
    pages = [CV_PAGE, CV_PAGE, REQUEST_PAGE, DATA_PAGE_TURNED] + [CERTIFICATE_PAGE] * 16 + [BACK_PAGE]

    found = pc.classify_multipass(pages)

    assert 4 in found.passport_pages, "the data page must be extracted"
    assert 21 in found.passport_pages, (
        "the back page of the same passport was held back as a foreign document"
    )
    assert not found.foreign_passport_pages


def test_the_certificates_around_them_are_still_never_uploaded():
    """The pages between the passport pages are certificates and stay put."""
    pages = [CV_PAGE, CV_PAGE, REQUEST_PAGE, DATA_PAGE_TURNED] + [CERTIFICATE_PAGE] * 16 + [BACK_PAGE]

    found = pc.classify_multipass(pages)

    certificates = set(range(5, 21))
    assert not certificates & set(found.passport_pages), (
        "a certificate was swept into the passport payload"
    )
    assert not certificates & set(found.aadhaar_pages)


def test_a_foreign_passport_still_speaks_for_itself():
    """The filter this could have broken. A page that names its own issuer keeps
    that verdict, and does not inherit India from a page beside it."""
    nepali = (
        "GOVERNMENT OF NEPAL\nPASSPORT / PASSEPORT\nSurname / Nom BHANDARI\n"
        "Nationality NEPALESE\nPlace of Issue KATHMANDU  Date of Expiry 2030"
    )
    found = pc.classify_multipass([CV_PAGE, CV_PAGE, REQUEST_PAGE, nepali])

    assert 4 not in found.passport_pages, "a Nepali passport was sent for extraction"
