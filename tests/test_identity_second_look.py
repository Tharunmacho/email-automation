"""A trace of an identity document earns a second look, not a shrug.

Two defects sat behind "the passport was not extracted", and neither was in the
reader:

1. A strong marker scores 2.0 and the routing seed is 3.0, so a page whose only
   surviving evidence was one unmistakable caption — "Name of Father / Legal
   Guardian", printed on the back of an Indian passport and essentially nothing
   else — scored 2.0 and was discarded, fourteen pages from a data page already
   confirmed as a passport.
2. A page can carry a bare, half-read trace of a document and score 0.0, because
   `id_document_scores` only counts markers precise enough to be evidence. Zero
   was then read as "nothing here" when it meant "something was here and the
   reader fumbled it".

The answers are symmetrical: a bundle that has proved it holds a passport
vouches for its other pages, and any page showing a trace at all is read again
at higher resolution before the bundle concludes it has none.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.extraction import page_classifier as pc
from app.extraction import text_extractor as tx


# --------------------------------------------------------------------------- #
#  The loose hint test
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "Name of Father / Legal Guardian ANGURAJ",
    "p@ssport no T2143612",                       # OCR mangled the 'a'
    "REPUBLIC OF INDIA",
    "2345 6789 0123",                             # a bare Aadhaar number
    "Government of India",
    "date of expiry 01/01/2030",
])
def test_a_trace_of_an_identity_document_is_noticed(text):
    assert pc.has_identity_hint(text), f"no hint found in {text!r}"


@pytest.mark.parametrize("text", [
    "Diploma in Mechanical Engineering, First Class",
    "Experience letter for services rendered from 2019 to 2024",
    "This is to certify that the candidate completed the safety course",
    "",
])
def test_ordinary_bundle_filler_is_not_a_hint(text):
    assert not pc.has_identity_hint(text), f"false hint in {text!r}"


def test_the_hint_test_is_looser_than_the_scoring_one():
    """It must catch pages the scorer gives up on — that is its whole job."""
    half_read = "for (erent afters a ars / Name of Father / Legal Guardian ANGURAJ"

    assert pc.id_document_scores(half_read)[pc.PASSPORT] < pc.ID_SEED_SCORE
    assert pc.has_identity_hint(half_read)


# --------------------------------------------------------------------------- #
#  A bundle vouches for its own pages
# --------------------------------------------------------------------------- #

DATA_PAGE = (
    "REPUBLIC OF INDIA PASSPORT / PASSEPORT Type / Type Country Code / Code du pays "
    "Surname / Nom SARAVANAN Given Names / Prenoms A Nationality INDIAN "
    "Date of Expiry 01/01/2030 Place of Issue MADURAI "
    "P<INDSARAVANAN<<A<<<<<<<<<<<<<<<<<<<<<<<<<<<< "
    "Z1234567<7IND8501011M3001019<<<<<<<<<<<<<<02"
)
#: One strong marker and nothing else — 2.0, under the 3.0 seed.
BACK_PAGE = (
    "Name of Father / Legal Guardian ANGURAJ T2143612 ATHI LAKSHMI ESAKKI ESWARI "
    "203/292 KEELAPALAYAM STREET TIRUNELVELI TAMIL NADU 627811 INDIA "
    "This page is part of the booklet and carries the holder's family particulars."
)
FILLER = (
    "This is to certify that the candidate has successfully completed the "
    "Diploma in Mechanical Engineering with First Class honours in the year 2019."
)


def test_a_lone_strong_marker_is_below_the_routing_seed():
    """The premise. If this ever changes, the tests below stop meaning anything."""
    score = pc.id_document_scores(BACK_PAGE)[pc.PASSPORT]

    assert 0 < score < pc.ID_SEED_SCORE, f"back page scores {score}"


def test_a_confirmed_passport_vouches_for_the_rest_of_its_booklet():
    """The reported bug: page 27 dropped while pages 10 and 11 were kept."""
    pages = [FILLER, DATA_PAGE, FILLER, FILLER, BACK_PAGE]

    result = pc.classify_multipass(pages)

    assert 2 in result.passport_pages, "the data page itself was not routed"
    assert 5 in result.passport_pages, (
        "the back page was dropped — the passport reaches Veris with half of "
        "itself missing"
    )
    assert 5 not in result.ignored_pages


def test_without_a_passport_in_the_bundle_nothing_is_adopted():
    """The weaker bar must not be able to invent a document that is not there."""
    pages = [FILLER, FILLER, BACK_PAGE, FILLER]

    result = pc.classify_multipass(pages)

    assert result.passport_pages == [], (
        f"a passport was conjured from one marker: {result.passport_pages}"
    )
    assert 3 in result.ignored_pages


def test_filler_pages_are_not_swept_up_by_the_adoption():
    """Adoption needs a marker of its own, not merely a passport in the bundle."""
    pages = [DATA_PAGE, FILLER, FILLER, FILLER]

    result = pc.classify_multipass(pages)

    assert result.passport_pages == [1]
    assert set(result.ignored_pages) >= {2, 3, 4}


# --------------------------------------------------------------------------- #
#  The second look
# --------------------------------------------------------------------------- #

@pytest.fixture
def second_look(monkeypatch):
    """Capture which pages get re-read, and let the test say what comes back."""
    monkeypatch.setattr(settings, "ocr_deep_read_enabled", True)
    monkeypatch.setattr(settings, "ocr_deep_read_max_pages", 12)
    asked: list[int] = []
    better: dict[int, str] = {}

    def fake_single_pass(data, pages=None, filename="", *, dpi=None, label=""):
        from app.extraction.local_ocr import PageRead

        wanted = sorted(pages or [])
        asked.extend(wanted)
        return {
            n: PageRead(n, better.get(n, ""), dpi or 450, label, 50.0) for n in wanted
        }

    monkeypatch.setattr(tx.local_ocr, "single_pass_pages", fake_single_pass)
    return asked, better


def test_a_page_scoring_nothing_but_hinting_is_read_again(second_look):
    """Score 0.0 is not "nothing here"; it is often "the caption was fumbled"."""
    asked, better = second_look
    texts = [DATA_PAGE, "PAS 5 sf CO @ tT passport", FILLER]

    tx._deepen_identity_hints(b"pdf", "bundle.pdf", texts, {1, 2, 3})

    assert 2 in asked, "a page with a bare trace was never looked at again"
    assert 3 not in asked, "an ordinary certificate was re-read for nothing"


def test_the_better_read_replaces_the_worse_one(second_look):
    """2.0 -> 7.0 on page 27 is the whole point of the exercise."""
    asked, better = second_look
    texts = [DATA_PAGE, BACK_PAGE]
    better[2] = DATA_PAGE  # the re-read resolves the captions

    merged, improved = tx._deepen_identity_hints(b"pdf", "bundle.pdf", texts, {1, 2})

    assert improved == {2}
    assert pc.id_document_scores(merged[1])[pc.PASSPORT] >= pc.ID_SEED_SCORE


def test_a_worse_read_is_discarded(second_look):
    """More characters is not better; a page of speckle scores well and says nothing."""
    asked, better = second_look
    texts = [DATA_PAGE, BACK_PAGE]
    better[2] = "aaa bbb ccc ddd eee fff ggg hhh iii jjj kkk lll mmm nnn ooo ppp"

    merged, improved = tx._deepen_identity_hints(b"pdf", "bundle.pdf", texts, {1, 2})

    assert improved == set()
    assert merged[1] == BACK_PAGE, "the better first read was thrown away"


def test_a_page_that_already_routes_is_left_alone(second_look):
    """It routes; a re-read could only change an answer that is already right."""
    asked, _better = second_look

    tx._deepen_identity_hints(b"pdf", "bundle.pdf", [DATA_PAGE], {1})

    assert asked == []


def test_the_second_look_is_bounded(second_look):
    """'Look harder at everything' is how a bundle costs an hour."""
    asked, _better = second_look
    settings.ocr_deep_read_max_pages = 3
    texts = [BACK_PAGE] * 10

    tx._deepen_identity_hints(b"pdf", "bundle.pdf", texts, set(range(1, 11)))

    assert len(asked) == 3, f"{len(asked)} pages re-read against a ceiling of 3"


def test_it_can_be_switched_off(second_look, monkeypatch):
    asked, _better = second_look
    monkeypatch.setattr(settings, "ocr_deep_read_enabled", False)

    merged, improved = tx._deepen_identity_hints(b"pdf", "b.pdf", [BACK_PAGE], {1})

    assert asked == [] and improved == set() and merged == [BACK_PAGE]


def test_a_failing_second_look_keeps_the_first_read(second_look, monkeypatch):
    """The first read still stands; this step may never cost a page its text."""
    def boom(*args, **kwargs):
        raise RuntimeError("tesseract is gone")

    monkeypatch.setattr(tx.local_ocr, "single_pass_pages", boom)

    merged, improved = tx._deepen_identity_hints(b"pdf", "b.pdf", [BACK_PAGE], {1})

    assert merged == [BACK_PAGE] and improved == set()
