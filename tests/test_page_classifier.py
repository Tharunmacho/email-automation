"""The résumé has to be found wherever it is, and only when it is really there.

Two failures this pins down, both seen in the live mailbox:

  * a 30-page application bundle whose CV sat on page 15 was parsed as though it
    were the passport scan on page 1, because every page went into one blob and
    the blob was truncated from the front;
  * a payment receipt named `cv.pdf` was ingested as a candidate, because the
    filename was the only thing anyone checked.
"""
from __future__ import annotations

from app.extraction import page_classifier as pc

# --------------------------------------------------------------------------- #
#  Page fixtures — deliberately in the wording these documents really use.
# --------------------------------------------------------------------------- #
RESUME_PAGE = """
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
Diploma in Mechanical Engineering, 2015
"""

RESUME_PAGE_TWO = """
TECHNICAL SKILLS
 - EOT Crane operation (up to 50 ton)
 - Rigging and slinging
 - TIG welding, arc welding
 - Blueprint and isometric drawing reading

CERTIFICATIONS
 - Third Party Crane Operator Certificate
 - Basic Fire Fighting

LANGUAGES KNOWN
Tamil, English, Hindi

DECLARATION
I hereby declare that the above information is true to the best of my knowledge.
"""

CERTIFICATE_PAGE = """
NATIONAL SKILL TRAINING INSTITUTE

CERTIFICATE OF COMPLETION

This is to certify that Rajesh Kumar M has successfully completed the
training programme in Crane Operation and Rigging Safety.

Certificate No. NSTI/2018/44219
Date of issue: 12 August 2018
Authorised Signatory
"""

EXPERIENCE_LETTER_PAGE = """
BHARAT FABRICATORS PVT LTD
Trichy - 620015

TO WHOM IT MAY CONCERN

This is to certify that Mr. Rajesh Kumar M has been working with us as a Rigger
from June 2015 to February 2019. During his tenure his conduct was found to be
satisfactory. We wish him all the best for his future endeavours.

Yours faithfully,
Human Resources Department
"""

PASSPORT_PAGE = """
REPUBLIC OF INDIA

Passport No. M4471902
Surname: KUMAR
Given Name: RAJESH
Nationality: INDIAN
Date of Issue: 04/07/2017
Date of Expiry: 03/07/2027
Place of Issue: TRICHY
Holder's Signature
"""

INVOICE_PAGE = """
TAX INVOICE

Invoice No. INV-2024-00817
GSTIN: 33AABCU9603R1ZM
Bill To: Sunrise Traders, Chennai 600001

Description            Qty      Rate      Amount
Steel plate 10mm        12    4,500.00   54,000.00

Subtotal                                 54,000.00
Grand Total                              63,720.00
Amount Paid: 63,720.00
Terms and conditions apply. Due date: 30/04/2024
Contact: 044 2851 9900
"""

HALL_TICKET_PAGE = """
ANNA UNIVERSITY

HALL TICKET / ADMIT CARD

Roll No: 812021104033
Name: RAJESH KUMAR M
Examination Centre: Trichy Zone 4
Seat No: B-118
Date: 14/05/2024
"""


# --------------------------------------------------------------------------- #
#  Rule 1 — find the résumé wherever it is
# --------------------------------------------------------------------------- #
def test_resume_on_page_one_of_a_bundle():
    result = pc.classify_document([
        RESUME_PAGE, RESUME_PAGE_TWO, CERTIFICATE_PAGE, PASSPORT_PAGE,
    ])
    assert result.is_resume
    assert result.resume_pages == [1, 2]


def test_resume_buried_deep_in_a_thirty_page_bundle():
    """The CV starts on page 15. Nothing before it may end the scan."""
    pages = [CERTIFICATE_PAGE, EXPERIENCE_LETTER_PAGE, PASSPORT_PAGE] * 4      # 1-12
    pages += [CERTIFICATE_PAGE, PASSPORT_PAGE]                                 # 13-14
    pages += [RESUME_PAGE, RESUME_PAGE_TWO]                                    # 15-16
    pages += [EXPERIENCE_LETTER_PAGE, CERTIFICATE_PAGE] * 7                    # 17-30
    assert len(pages) == 30

    result = pc.classify_document(pages)
    assert result.is_resume
    assert result.resume_pages == [15, 16]
    assert "15" in result.reason


def test_continuation_page_is_kept_even_without_contact_details():
    """A spillover page has no name, no email, no phone and no heading of its
    own — it is only ever recognisable as the page after the CV."""
    spillover = """
    - Carried out daily pre-use inspection of lifting gear and slings.
    - Maintained shift logs and reported defects to the maintenance foreman.
    - Assisted in erection of structural steel during plant shutdown.
    """
    standalone = pc.classify_page(spillover)
    assert standalone.kind != pc.RESUME, "the page cannot qualify on its own"

    result = pc.classify_document([RESUME_PAGE, spillover])
    assert result.resume_pages == [1, 2]


def test_continuation_growth_stops_at_the_next_document():
    """Growing outward must not swallow the certificate stapled behind the CV."""
    result = pc.classify_document([RESUME_PAGE, CERTIFICATE_PAGE, RESUME_PAGE_TWO])
    assert 2 not in result.resume_pages


def test_single_page_resume():
    result = pc.classify_document([RESUME_PAGE])
    assert result.is_resume
    assert result.resume_pages == [1]
    assert result.confidence >= 0.55


def test_bundle_pages_are_labelled_by_what_they_are():
    result = pc.classify_document([
        RESUME_PAGE, CERTIFICATE_PAGE, EXPERIENCE_LETTER_PAGE, PASSPORT_PAGE,
    ])
    kinds = result.page_kinds
    assert kinds[1] == pc.RESUME
    assert kinds[2] == pc.CERTIFICATE
    assert kinds[3] == pc.EXPERIENCE_LETTER
    assert kinds[4] == pc.ID_DOCUMENT


# --------------------------------------------------------------------------- #
#  Rule 2 — reject on content, never on the filename
# --------------------------------------------------------------------------- #
def test_invoice_named_cv_is_rejected():
    result = pc.classify_document([INVOICE_PAGE])
    assert not result.is_resume
    assert result.confidence < 0.30
    assert result.resume_pages == []


def test_hall_ticket_is_rejected_despite_carrying_a_name():
    result = pc.classify_document([HALL_TICKET_PAGE])
    assert not result.is_resume
    assert result.confidence < 0.30


def test_certificates_alone_are_not_a_resume():
    """A bundle of supporting documents with no CV in it is not an application."""
    result = pc.classify_document([
        CERTIFICATE_PAGE, EXPERIENCE_LETTER_PAGE, PASSPORT_PAGE, CERTIFICATE_PAGE,
    ])
    assert not result.is_resume
    assert result.confidence < 0.30


def test_empty_document_is_rejected():
    assert not pc.classify_document([]).is_resume
    assert not pc.classify_document(["", "   "]).is_resume


# --------------------------------------------------------------------------- #
#  Rule 4 — the selection is what makes selective OCR possible
# --------------------------------------------------------------------------- #
def test_select_text_returns_only_the_resume_pages():
    pages = [CERTIFICATE_PAGE, RESUME_PAGE, RESUME_PAGE_TWO, INVOICE_PAGE]
    result = pc.classify_document(pages)
    text = pc.select_text(pages, result.resume_pages)

    assert "EOT Crane Operator" in text
    assert "TIG welding" in text
    assert "GSTIN" not in text
    assert "Authorised Signatory" not in text


def test_selection_shrinks_the_payload_for_a_large_bundle():
    pages = [CERTIFICATE_PAGE] * 14 + [RESUME_PAGE] + [PASSPORT_PAGE] * 15
    result = pc.classify_document(pages)
    selected = pc.select_text(pages, result.resume_pages)
    assert len(selected) < len("".join(pages)) * 0.25


def test_unmarked_prose_behind_the_resume_is_part_of_it():
    """The page that was silently dropped from a real candidate's extraction.

    Page 2 of MOHAMMEDASIF's CV was 2,974 characters of responsibilities with no
    heading, no date range, no contact block and no job-title noun the scorer
    knew — it scored 0.00 and fell out of the AI payload while page 1 went on.
    Prose directly behind the resume, carrying none of the markings of another
    document, belongs to the resume.
    """
    spillover = (
        "EHS activities-EnvironmentalAspect/Impact, HIRA, in line with the plant "
        "requirement and statutory obligations. Conducted layered process audits "
        "and drove closure of non-conformances raised during internal reviews. "
        "Coordinated with cross functional teams for shop floor issue resolution "
        "and sustained the improvements through standard operating procedures. "
    ) * 4
    assert pc.classify_page(spillover).score < 1.0, "fixture must be low-scoring"

    result = pc.classify_document([RESUME_PAGE, spillover])
    assert result.resume_pages == [1, 2]
    assert "EHS activities" in pc.select_text([RESUME_PAGE, spillover], result.resume_pages)


def test_weak_continuation_does_not_run_away_down_the_document():
    """Two unmarked pages of grace, not fourteen."""
    filler = "General notes about the plant and its operations. " * 30
    pages = [RESUME_PAGE] + [filler] * 8

    result = pc.classify_document(pages)
    assert len(result.resume_pages) <= 3, result.resume_pages


def test_a_blank_page_ends_the_resume():
    result = pc.classify_document([RESUME_PAGE, "   ", CERTIFICATE_PAGE])
    assert result.resume_pages == [1]


PERSONAL_DETAILS_PAGE = """
COMPANY DETAILS
1 - Company: Tata Motors Limited (Prolife)
    Duration: 2019 - Present
    Designation: Maintenance Executive

PERSONAL DETAILS
Date of Birth : 12/05/1991
Nationality   : Indian
Blood Group   : B+
Passport No   : M4471902
Date of Issue : 04/07/2017
Date of Expiry: 03/07/2027
Place of Issue: Lucknow
Marital Status: Married
"""


def test_a_resume_listing_its_passport_details_is_not_a_passport():
    """Indian and Gulf CVs carry these fields in a PERSONAL DETAILS block.

    A real candidate lost his employment history to this: the page opened with
    "COMPANY DETAILS ... Tata Motors Limited" and was classified as an ID scan
    because it also stated a passport number and a nationality.
    """
    page = pc.classify_page(PERSONAL_DETAILS_PAGE)
    assert page.kind != pc.ID_DOCUMENT, f"scored {page.score}, kind {page.kind}"

    result = pc.classify_document([RESUME_PAGE, PERSONAL_DETAILS_PAGE])
    assert result.resume_pages == [1, 2]


def test_an_actual_passport_scan_is_still_an_id_document():
    """The loosening must not blind it to the real thing."""
    result = pc.classify_document([RESUME_PAGE, PASSPORT_PAGE])
    assert result.page_kinds[2] == pc.ID_DOCUMENT
    assert result.resume_pages == [1]
