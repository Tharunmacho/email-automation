"""One bundle, four kinds of page, three of them worth money.

The scenario is the one that turns up in the live mailbox: a candidate applies
with a single scanned PDF holding fifty-odd certificates, a two-page CV, an
Aadhaar card and a passport data page, in that order, and nothing in the file
says which is which. Until the multipass split, everything but the CV was
discarded and a recruiter retyped the Aadhaar number by hand.

What is pinned here is both halves of the promise:

  * each document is found on its own pages and routed to its own endpoint;
  * and — the half that is easy to break — a résumé that merely *mentions* a
    passport number or an Aadhaar number is still a résumé. That distinction
    once cost a real candidate his employment history, and the ID pass is not
    allowed to reintroduce it.
"""
from __future__ import annotations

from app.extraction import page_classifier as pc

# --------------------------------------------------------------------------- #
#  Page fixtures, in the wording these documents really use.
# --------------------------------------------------------------------------- #
RESUME_PAGE_1 = """
CURRICULUM VITAE

RAJESH KUMAR M
Mobile: +91 98765 43210
Email: rajesh.kumar87@gmail.com
Tiruchirappalli, Tamil Nadu, India

CAREER OBJECTIVE
Seeking a challenging position as an EOT Crane Operator in a reputed organisation.

WORK EXPERIENCE
Al Faris Heavy Equipment LLC, Dubai
EOT Crane Operator
Mar 2019 - Present
 - Operated 50-ton EOT crane in fabrication yard.
 - Coordinated rigging and lifting plans with the site supervisor.

Bharat Fabricators Pvt Ltd, Trichy
Rigger
Jun 2015 - Feb 2019

EDUCATION
Diploma in Mechanical Engineering, Anna University, 2014
"""

# Page 2 of the same CV: no name, no email, no headings. It only survives as a
# continuation of page 1 — and it must not be mistaken for anything else.
RESUME_PAGE_2 = """
Responsibilities included daily toolbox talks, HIRA reviews and lift plan
sign-off with the site supervisor across the fabrication yard operations,
covering environmental aspect and impact registers in line with the client
EHS management system.
"""

# The personal-details block a Gulf CV really carries. Every phrase here is one
# the ID markers would love to claim.
RESUME_WITH_ID_DETAILS = """
CURRICULUM VITAE

SURESH BABU K
Email: suresh.babu@gmail.com   Mobile: +91 90000 11111

PERSONAL DETAILS
Date of Birth      : 02/11/1990
Nationality        : Indian
Sex                : Male
Passport No.       : N1234567
Date of Issue      : 12/03/2018
Date of Expiry     : 11/03/2028
Place of Issue     : Chennai
Aadhaar No.        : 998877665544
Marital Status     : Married

WORK EXPERIENCE
Gulf Steel Works, Dammam
Welder
Jan 2018 - Present

EDUCATION
ITI Welding, Government ITI Trichy, 2011
"""

CERTIFICATE = """
CERTIFICATE OF COMPLETION
This is to certify that Rajesh Kumar M has successfully completed the
Advanced Rigging and Slinging course conducted at our training centre.
Certificate No: TRG/2019/8842   Date of Issue: 12 March 2019
Authorised Signatory
"""

AADHAAR_CARD = """
GOVERNMENT OF INDIA
Rajesh Kumar M
DOB: 14/07/1987
Male
3521 8842 9017
AADHAAR - Mera Aadhaar, Meri Pehchan
Unique Identification Authority of India
Address: S/O Muthu Kumar, 12 Bharathi Street, Tiruchirappalli,
Tamil Nadu - 620001
"""

PASSPORT_PAGE = """
REPUBLIC OF INDIA
Type / Type   Country Code / Code du pays   Passport No. / No. du passeport
P             IND                           M4821779
Surname / Nom
KUMAR
Given Name(s) / Prenoms
RAJESH
Nationality / Nationalite  INDIAN    Sex / Sexe  M
Date of Birth 14/07/1987   Place of Birth TIRUCHIRAPPALLI
Date of Issue 08/05/2019   Place of Issue MADURAI   Date of Expiry 07/05/2029
P<INDKUMAR<<RAJESH<<<<<<<<<<<<<<<<<<<<<<<<<<
M48217794IND8707142M2905078<<<<<<<<<<<<<<02
"""


def sixty_page_bundle() -> list[str]:
    """Certificates 1-51, CV on 52-53, Aadhaar on 54, passport on 55, more certs."""
    return (
        [CERTIFICATE] * 51
        + [RESUME_PAGE_1, RESUME_PAGE_2]
        + [AADHAAR_CARD, PASSPORT_PAGE]
        + [CERTIFICATE] * 5
    )


# --------------------------------------------------------------------------- #
#  The bundle
# --------------------------------------------------------------------------- #
def test_each_document_is_found_on_its_own_pages():
    result = pc.classify_multipass(sixty_page_bundle())

    assert result.is_resume is True
    assert result.resume_pages == [52, 53]
    assert result.aadhaar_pages == [54]
    assert result.passport_pages == [55]


def test_the_three_page_sets_are_disjoint():
    """A page routed to two endpoints would be extracted — and billed — twice."""
    result = pc.classify_multipass(sixty_page_bundle())

    all_pages = result.resume_pages + result.aadhaar_pages + result.passport_pages
    assert len(all_pages) == len(set(all_pages))


def test_certificates_are_ignored_rather_than_uploaded():
    result = pc.classify_multipass(sixty_page_bundle())

    # 60 pages, 4 of them wanted. The other 56 never reach an OCR endpoint.
    assert len(result.ignored_pages) == 56
    assert 1 in result.ignored_pages
    assert 60 in result.ignored_pages


def test_modes_names_exactly_the_work_to_submit():
    """`modes()` is what the extractor iterates, so it is the real contract."""
    modes = pc.classify_multipass(sixty_page_bundle()).modes()

    assert modes == {"resume": [52, 53], "aadhaar": [54], "passport": [55]}
    # Résumé first: the candidate record the other two hang off comes from it.
    assert list(modes) == ["resume", "aadhaar", "passport"]


def test_a_bundle_with_no_id_documents_submits_only_the_resume():
    pages = [CERTIFICATE] * 3 + [RESUME_PAGE_1, RESUME_PAGE_2] + [CERTIFICATE]

    result = pc.classify_multipass(pages)

    assert result.resume_pages == [4, 5]
    assert result.aadhaar_pages == []
    assert result.passport_pages == []
    assert result.modes() == {"resume": [4, 5]}


# --------------------------------------------------------------------------- #
#  The rule the ID pass must not break
# --------------------------------------------------------------------------- #
def test_a_resume_listing_a_passport_number_is_not_a_passport():
    """The failure this whole scoring scheme is shaped around.

    A Gulf CV prints Passport No, Nationality, Sex, Date of Expiry and Place of
    Issue in one block. Reading that as a passport would both lose the résumé
    page and send a CV to the MRZ extractor.
    """
    result = pc.classify_multipass([RESUME_WITH_ID_DETAILS])

    assert result.is_resume is True
    assert result.resume_pages == [1]
    assert result.passport_pages == []
    assert result.aadhaar_pages == []


def test_a_resume_page_scores_nothing_on_either_id_scale():
    scores = pc.id_document_scores(RESUME_PAGE_1)

    assert scores[pc.AADHAAR] == 0.0
    assert scores[pc.PASSPORT] == 0.0


def test_the_mrz_is_what_makes_a_passport_a_passport():
    """Strip the MRZ and the same page is still recognisable; strip the printed
    labels too and it is not — which is the intended ordering of evidence."""
    with_mrz = pc.id_document_scores(PASSPORT_PAGE)[pc.PASSPORT]
    without_mrz = pc.id_document_scores(
        "\n".join(l for l in PASSPORT_PAGE.splitlines() if "<<" not in l)
    )[pc.PASSPORT]

    assert with_mrz > without_mrz >= pc._ID_SEED_SCORE


def test_the_uidai_wording_is_what_makes_an_aadhaar_an_aadhaar():
    card = pc.id_document_scores(AADHAAR_CARD)

    assert card[pc.AADHAAR] >= pc._ID_SEED_SCORE
    # And it outranks the passport reading, so the routing is unambiguous.
    assert card[pc.AADHAAR] > card[pc.PASSPORT]


def test_a_standalone_aadhaar_attachment_needs_no_resume_to_be_found():
    """Candidates often attach the card as its own file, alongside the CV."""
    result = pc.classify_multipass([AADHAAR_CARD])

    assert result.is_resume is False
    assert result.aadhaar_pages == [1]
    assert result.modes() == {"aadhaar": [1]}


def test_an_invoice_is_routed_nowhere():
    invoice = """
    TAX INVOICE
    Invoice No: INV-2026-0044      Date: 12/03/2026
    Bill To: Sunrise Traders, GSTIN 33AABCS1429B1ZP
    Qty  Description               Amount
    2    Welding rods (5kg)        1,240.00
    Subtotal 1,240.00   Grand Total 1,463.20
    Terms and conditions apply.
    """
    result = pc.classify_multipass([invoice])

    assert result.modes() == {}
    assert result.ignored_pages == [1]


def test_page_kinds_name_the_specific_document():
    """The generic `id_document` bucket is not enough to pick an endpoint."""
    result = pc.classify_multipass(sixty_page_bundle())
    kinds = result.page_kinds

    assert kinds[54] == pc.AADHAAR
    assert kinds[55] == pc.PASSPORT
    assert kinds[52] == pc.RESUME


def test_the_reason_says_where_everything_was_found():
    """Operators read this line in the logs when an extraction looks wrong."""
    reason = pc.classify_multipass(sixty_page_bundle()).reason

    assert "52, 53" in reason
    assert "aadhaar on page(s) 54" in reason
    assert "passport on page(s) 55" in reason


def test_an_empty_document_is_not_an_id_document():
    result = pc.classify_multipass([])

    assert result.modes() == {}
    assert result.ignored_pages == []
