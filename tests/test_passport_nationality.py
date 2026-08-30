"""Only an Indian passport may be spent on the Veris passport endpoint.

The endpoint is trained on the Indian booklet. A Nepali or Philippine passport
fed to it is not refused — it comes back a confidently wrong record, and a wrong
passport number is discovered at an embassy counter months later. So the issuing
country is settled from the local text layer, before anything is uploaded, and
this file pins both halves of that promise:

  * a foreign passport is *recognised*, named, and never submitted;
  * an Indian passport still goes through — including the ones that arrive as a
    poor scan, where the MRZ is the only readable thing on the page, and the
    ones that arrive as a back page with no MRZ at all.

The second half is the one that quietly breaks. A filter that rejects every
passport passes any test that only checks foreign ones are blocked.
"""
from __future__ import annotations

import pytest

from app.extraction import page_classifier as pc
from app.extraction import passport_nationality as pn


# --------------------------------------------------------------------------- #
#  Fixtures, in the wording these documents really use
# --------------------------------------------------------------------------- #
INDIAN_PASSPORT = """
REPUBLIC OF INDIA
पासपोर्ट / PASSPORT
Type/टाईप   Country Code/राष्ट्र कोड   Passport No./पासपोर्ट नं.
P           IND                        Z3456789
Surname/उपनाम        KUMAR
Given Name(s)/दिया गया नाम   RAJESH
Nationality/राष्ट्रीयता   INDIAN
Date of Birth/जन्म तिथि   01/01/1985
Place of Issue   CHENNAI
Date of Expiry/समाप्ति की तिथि   01/01/2030
P<INDKUMAR<<RAJESH<<<<<<<<<<<<<<<<<<<<<<<<<<
Z3456789<4IND8501014M3001012<<<<<<<<<<<<<<06
"""

# The same booklet as a bad scan: the printed side is mush, the MRZ survives
# with Tesseract's usual chevron damage.
INDIAN_PASSPORT_NOISY = """
REPUBUC OF INOIA
Passporl No. Z3456789
P(1NDKUMAR((RAJESH((((((((((((((((((((((((((
Z3456789(4IND8501014M3001012((((((((((((((06
"""

# The back page. No MRZ at all — the Hindi labels are the only evidence.
INDIAN_PASSPORT_BACK = """
पिता / कानूनी अभिभावक का नाम / Name of Father/Legal Guardian
MURUGAN K
माता का नाम / Name of Mother
LAKSHMI M
पत्नी / पति का नाम / Name of Spouse
Old Passport No./File No. with Date and Place of Issue
K1234567 / CH1234567890  15/03/2011  CHENNAI
"""

US_PASSPORT_BORN_IN_INDIANA = """
UNITED STATES OF AMERICA
PASSPORT
Surname   MILLER
Given Names   JOHN ROBERT
Nationality   UNITED STATES OF AMERICA
Place of Birth   INDIANA, U.S.A.
Date of Expiry   12 MAR 2031
P<USAMILLER<<JOHN<ROBERT<<<<<<<<<<<<<<<<<<<<
5123456780USA8203015M3103127<<<<<<<<<<<<<<02
"""

NEPALI_PASSPORT = """
FEDERAL DEMOCRATIC REPUBLIC OF NEPAL
नेपाल
PASSPORT / PASSEPORT
Surname / Nom   THAPA
Given Names / Prenoms   BIKASH
Nationality   NEPALI
P<NPLTHAPA<<BIKASH<<<<<<<<<<<<<<<<<<<<<<<<<<
1234567890NPL9001012M2901015<<<<<<<<<<<<<<04
"""

PHILIPPINE_PASSPORT = """
REPUBLIKA NG PILIPINAS
REPUBLIC OF THE PHILIPPINES
PASAPORTE / PASSPORT
Surname / Nom   SANTOS
Given Names / Prenoms   MARIA CLARA
Nationality   FILIPINO
Place of Issue   MANILA
Date of Expiry   04/03/2030
P<PHLSANTOS<<MARIA<CLARA<<<<<<<<<<<<<<<<<<<<
P12345678PHL9203045F3003041<<<<<<<<<<<<<<<08
"""

# Nobody wrote a marker row for Estonia. Its MRZ still identifies it.
ESTONIAN_PASSPORT = """
EESTI VABARIIK
PASSPORT
Surname   TAMM
Given Names   JAAN
P<ESTTAMM<<JAAN<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
K12345678EST8501011M3001019<<<<<<<<<<<<<<<06
"""

UNREADABLE_PASSPORT = """
PASSPORT
Surname
Given Names
Date of Expiry
Place of Issue
"""

RESUME_PAGE = """
CURRICULUM VITAE

RAJESH KUMAR M
Mobile: +91 98765 43210
Email: rajesh.kumar87@gmail.com

CAREER OBJECTIVE
Seeking a challenging position as an EOT Crane Operator.

WORK EXPERIENCE
Al Faris Heavy Equipment LLC, Dubai
EOT Crane Operator
Mar 2019 - Present
 - Operated 50-ton EOT crane in the fabrication yard.

EDUCATION
Diploma in Mechanical Engineering, 2013
"""

AADHAAR_CARD = """
भारत सरकार
GOVERNMENT OF INDIA
UNIQUE IDENTIFICATION AUTHORITY OF INDIA
RAJESH KUMAR M
DOB: 01/01/1985
1234 5678 9012
मेरा आधार, मेरी पहचान
"""


# --------------------------------------------------------------------------- #
#  Indian passports must still get through
# --------------------------------------------------------------------------- #
def test_clean_indian_passport_is_indian():
    verdict = pn.detect_passport_country(INDIAN_PASSPORT)
    assert verdict.verdict == pn.INDIAN
    assert verdict.country_code == "IND"
    assert verdict.confidence > 0.9


def test_indian_passport_survives_ocr_damage_to_the_mrz():
    """`P(1NDKUMAR` is what Tesseract returns for `P<INDKUMAR` on a poor scan.

    If the filter cannot read that, it rejects real Indian passports on scan
    quality alone — the failure that would make this feature worse than nothing.
    """
    verdict = pn.detect_passport_country(INDIAN_PASSPORT_NOISY)
    assert verdict.verdict == pn.INDIAN
    assert verdict.mrz.issuing_state == "IND"


def test_indian_back_page_without_an_mrz_is_still_indian():
    verdict = pn.detect_passport_country(INDIAN_PASSPORT_BACK)
    assert verdict.verdict == pn.INDIAN


# --------------------------------------------------------------------------- #
#  Foreign passports must not be
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, code",
    [
        (US_PASSPORT_BORN_IN_INDIANA, "USA"),
        (NEPALI_PASSPORT, "NPL"),
        (PHILIPPINE_PASSPORT, "PHL"),
        (ESTONIAN_PASSPORT, "EST"),
    ],
)
def test_foreign_passports_are_named_and_blocked(text, code):
    verdict = pn.detect_passport_country(text)
    assert verdict.verdict == pn.FOREIGN
    assert verdict.country_code == code
    send, _ = pn.should_extract(verdict)
    assert send is False


def test_indiana_is_not_india():
    """A US passport whose bearer was born in Indiana.

    `\\bINDIA\\b` does not match INDIANA, and the MRZ issuing state settles it
    regardless — but this is the exact substring trap the filter has to survive,
    so it is pinned rather than left to the regex.
    """
    verdict = pn.detect_passport_country(US_PASSPORT_BORN_IN_INDIANA)
    assert verdict.country_code == "USA"
    assert not verdict.is_indian


def test_a_country_with_no_marker_row_is_still_caught_by_its_mrz():
    """Scalability, stated as a test: `_ISO3_CODES` covers every country.

    Nobody wrote markers for Estonia. Adding them must not be a prerequisite for
    an Estonian passport being kept out of the Indian endpoint.
    """
    assert "EST" not in pn._COUNTRY_MARKERS
    assert pn.detect_passport_country(ESTONIAN_PASSPORT).country_code == "EST"


# --------------------------------------------------------------------------- #
#  The third verdict
# --------------------------------------------------------------------------- #
def test_unreadable_passport_is_undetermined_not_foreign():
    verdict = pn.detect_passport_country(UNREADABLE_PASSPORT)
    assert verdict.verdict == pn.UNDETERMINED
    assert verdict.country_code is None


def test_undetermined_follows_policy_in_both_directions():
    verdict = pn.detect_passport_country(UNREADABLE_PASSPORT)
    assert pn.should_extract(verdict, allow_undetermined=True)[0] is True
    assert pn.should_extract(verdict, allow_undetermined=False)[0] is False


def test_a_confirmed_foreign_passport_is_blocked_under_every_policy():
    """`allow_undetermined` is about unreadable scans, never about known ones."""
    verdict = pn.detect_passport_country(NEPALI_PASSPORT)
    assert pn.should_extract(verdict, allow_undetermined=True)[0] is False
    assert pn.should_extract(verdict, allow_undetermined=False)[0] is False


def test_the_filter_can_be_turned_off_entirely():
    verdict = pn.detect_passport_country(NEPALI_PASSPORT)
    send, why = pn.should_extract(verdict, india_only=False)
    assert send is True
    assert "disabled" in why


# --------------------------------------------------------------------------- #
#  Multi-page booklets
# --------------------------------------------------------------------------- #
def test_one_foreign_page_vetoes_a_combined_verdict():
    """A bundle holding two passports must not send the foreign one.

    The front page of an Indian booklet and the data page of a Nepali one land
    in the same scan often enough; the confident Indian reading must not carry
    the Nepali page along with it.
    """
    combined = pn.combine([
        pn.detect_passport_country(INDIAN_PASSPORT),
        pn.detect_passport_country(NEPALI_PASSPORT),
    ])
    assert combined.verdict == pn.FOREIGN
    assert combined.country_code == "NPL"


def test_a_booklet_speaks_with_its_most_confident_page():
    combined = pn.combine([
        pn.detect_passport_country(UNREADABLE_PASSPORT),
        pn.detect_passport_country(INDIAN_PASSPORT),
    ])
    assert combined.verdict == pn.INDIAN


# --------------------------------------------------------------------------- #
#  Through the classifier, which is what actually routes the upload
# --------------------------------------------------------------------------- #
def test_classifier_routes_the_indian_passport_and_holds_the_foreign_one():
    result = pc.classify_multipass(
        [RESUME_PAGE, INDIAN_PASSPORT, PHILIPPINE_PASSPORT, AADHAAR_CARD]
    )
    assert result.passport_pages == [2]
    assert result.foreign_passport_pages == [3]
    assert result.aadhaar_pages == [4]
    assert result.resume_pages == [1]


def test_a_held_passport_never_reaches_the_modes_map():
    """`modes()` is what the extractor submits. A blocked page cannot be in it."""
    modes = pc.classify_multipass([RESUME_PAGE, PHILIPPINE_PASSPORT]).modes()
    assert pc.PASSPORT not in modes


def test_the_reason_says_which_country_was_held_and_why():
    result = pc.classify_multipass([RESUME_PAGE, NEPALI_PASSPORT])
    assert "Nepal" in result.reason
    assert "not sent" in result.reason

    report = result.nationality_report()
    assert len(report) == 1
    assert report[0]["routed"] == "skipped"
    assert report[0]["country"] == "Nepal"
    assert report[0]["mrz_issuing_state"] == "NPL"


def test_aadhaar_routing_is_untouched_by_the_passport_filter():
    """The filter is the passport pass's alone; Aadhaar must not feel it."""
    result = pc.classify_multipass([RESUME_PAGE, AADHAAR_CARD])
    assert result.aadhaar_pages == [2]
    assert result.foreign_passport_pages == []


def test_a_resume_mentioning_a_passport_number_is_still_only_a_resume():
    """The pre-existing guarantee, re-pinned: the filter must not disturb it."""
    resume = RESUME_PAGE + "\nPassport No: Z3456789\nNationality: Indian\n"
    result = pc.classify_multipass([resume])
    assert result.passport_pages == []
    assert result.foreign_passport_pages == []


# --------------------------------------------------------------------------- #
#  MRZ mechanics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line1, expected",
    [
        ("P<INDKUMAR<<RAJESH<<<<<<<<<<<<<<<<<<<<<<<<<<", "IND"),
        ("P(INDKUMAR((RAJESH((((((((((((((((((((((((((", "IND"),
        ("P«IND«KUMAR«RAJESH«««««««««««««««««««««««««", "IND"),
        ("P<1NDKUMAR<<RAJESH<<<<<<<<<<<<<<<<<<<<<<<<<<", "IND"),
        ("P<USAMILLER<<JOHN<<<<<<<<<<<<<<<<<<<<<<<<<<<", "USA"),
    ],
)
def test_mrz_issuing_state_survives_the_usual_ocr_damage(line1, expected):
    assert pn.read_mrz(line1).issuing_state == expected


def test_a_line_of_capitals_is_not_an_mrz():
    """Every real TD3 row is filler-padded. A shouted heading is not one."""
    assert pn.mrz_lines("PERSONAL PARTICULARS AND EMPLOYMENT HISTORY OF THE") == []


def test_an_ambiguous_code_resolves_to_nothing_rather_than_to_a_guess():
    code, exact = pn._resolve_code("XQZ")
    assert code is None or exact is False
