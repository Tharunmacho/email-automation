"""Whose CV is this — decided before a single byte is uploaded.

Why this exists
---------------
This desk recruits Indian candidates. A CV from someone who is not an Indian
national cannot be placed, so paying a Veris résumé extraction for it and then
filing the person in the CRM buys nothing twice: an API call that will never
earn its cost, and a record a recruiter has to find and delete.

So the rule is the one `passport_nationality` already enforces for booklets,
applied to the CV itself: **only an Indian candidate reaches the résumé
endpoint and the candidate database.** Everything else is recognised, named,
logged and refused, and the refusal says which country it read.

The decision is made from the *local* text — the PDF's own layer, or
Tesseract's read of the scan — which the classifier already has in hand. Nothing
is uploaded to reach it, so a foreign CV costs zero API calls rather than one
wasted one.

The asymmetry that shapes everything here
-----------------------------------------
The two mistakes available are not equally bad.

Letting a foreign CV through costs one extraction and a record somebody
deletes. Rejecting an Indian candidate loses a placement, and loses it
*silently* — nobody reviews what was never filed.

Most Indian CVs never write "Nationality: Indian" at all. A filter that demands
proof of Indian nationality therefore rejects most of the people it exists to
find. So the burden of proof runs one way only:

    INDIAN        — say so, or look Indian.        -> accept
    UNDETERMINED  — the CV does not say.           -> accept
    FOREIGN       — positive evidence of another   -> refuse
                    country, clear of any Indian
                    evidence on the same page.

Undetermined is the common case and it is accepted, exactly as the passport
reader accepts an unreadable booklet rather than throwing it away.

What counts as evidence
-----------------------
Strongest first, and the weights are the whole policy:

  1. **A passport in the same bundle** (`PASSPORT_EVIDENCE`). The classifier has
     already read every page and run `passport_nationality` over the ones that
     are booklets, so an MRZ reading `P<PAK` is sitting there for free. It is
     the best identity evidence a bundle can carry and it outranks everything
     printed on the CV.
  2. **A stated nationality** (`STATED_EVIDENCE`) — "Nationality: Pakistani",
     "Citizenship / Nationality : Nepali". Unambiguous when present.
  3. **A place of birth** (`BIRTHPLACE_EVIDENCE`). Birthplace is not
     citizenship, so this is deliberately weaker than a statement.
  4. **Addresses and phone numbers** (`PLACE_EVIDENCE`, `DIALLING_EVIDENCE`).
     The weakest signals here, and the ones that must never decide alone: an
     Indian driver working in Sharjah has a UAE address and a +971 mobile, and
     rejecting him would be precisely the expensive mistake. Two of these
     together still fall short of `min_score`; they can corroborate a stated
     nationality, and they cannot overrule one.

Every signal scores *a country*, not "foreign". That matters: a CV listing a
Dubai employer and an Indian home town scores both, and a verdict needs to beat
the runner-up by `margin` before it is a verdict at all. Contradiction produces
UNDETERMINED — which is accepted — rather than a coin flip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.config import settings
from app.extraction import passport_nationality as pn
from app.logging_config import get_logger

log = get_logger(__name__)

# The three verdicts, shared with the passport reader so a caller handling one
# handles the other.
INDIAN = pn.INDIAN
FOREIGN = pn.FOREIGN
UNDETERMINED = pn.UNDETERMINED
INDIA_CODE = pn.INDIA_CODE


# --------------------------------------------------------------------------- #
#  What each kind of evidence is worth
# --------------------------------------------------------------------------- #
#: A passport in the bundle. Decisive on its own — this is a government document
#: saying whose national the candidate is, which is the exact question.
PASSPORT_EVIDENCE = 6.0
#: "Nationality: Pakistani" in so many words.
STATED_EVIDENCE = 4.0
#: "Place of Birth: Lahore". Real evidence, but birth is not citizenship.
BIRTHPLACE_EVIDENCE = 1.5
#: A city or region named anywhere on the CV — an address, an employer, a school.
PLACE_EVIDENCE = 1.0
#: An international dialling code on a phone number.
DIALLING_EVIDENCE = 1.0


# --------------------------------------------------------------------------- #
#  Demonyms
# --------------------------------------------------------------------------- #
# What people write in a nationality field. Keyed by the word itself, because
# that is what the regex captures; several spellings map to one country.
#
# Deliberately not generated from the country list: "Indian" is the word, not
# "India", and half of these are irregular. A wrong demonym here is a wrongly
# rejected candidate, so the table is written out and reviewable.
_DEMONYMS: Dict[str, str] = {
    # India, and the spellings a scan produces.
    "indian": "IND", "indians": "IND", "bharatiya": "IND", "india": "IND",
    # The corridor this desk actually meets.
    "pakistani": "PAK", "pakistan": "PAK", "pakistanis": "PAK",
    "bangladeshi": "BGD", "bangladesh": "BGD", "bengali": "BGD",
    "nepali": "NPL", "nepalese": "NPL", "nepal": "NPL",
    "sri lankan": "LKA", "srilankan": "LKA", "sinhalese": "LKA",
    "lankan": "LKA", "sri lanka": "LKA",
    "filipino": "PHL", "filipina": "PHL", "philippine": "PHL",
    "philippines": "PHL", "pinoy": "PHL",
    "afghan": "AFG", "afghani": "AFG", "afghanistan": "AFG",
    "burmese": "MMR", "myanmar": "MMR", "myanmarese": "MMR",
    "indonesian": "IDN", "indonesia": "IDN",
    "malaysian": "MYS", "malaysia": "MYS",
    "vietnamese": "VNM", "vietnam": "VNM",
    "thai": "THA", "thailand": "THA",
    "chinese": "CHN", "china": "CHN",
    "bhutanese": "BTN", "bhutan": "BTN",
    "maldivian": "MDV", "maldives": "MDV",
    # Gulf nationals. Rare on this desk but unambiguous when written.
    "emirati": "ARE", "uae": "ARE", "emirian": "ARE",
    "saudi": "SAU", "saudi arabian": "SAU",
    "qatari": "QAT", "kuwaiti": "KWT", "omani": "OMN", "bahraini": "BHR",
    "yemeni": "YEM", "jordanian": "JOR", "lebanese": "LBN", "syrian": "SYR",
    "egyptian": "EGY", "sudanese": "SDN", "iraqi": "IRQ", "iranian": "IRN",
    # Elsewhere. Not expected on this desk, and listed anyway so a refusal can
    # say which country it read instead of "another country".
    "british": "GBR", "english": "GBR", "scottish": "GBR", "welsh": "GBR",
    "american": "USA", "canadian": "CAN", "australian": "AUS",
    "new zealander": "NZL", "irish": "IRL", "french": "FRA", "german": "DEU",
    "italian": "ITA", "spanish": "ESP", "portuguese": "PRT", "dutch": "NLD",
    "belgian": "BEL", "swiss": "CHE", "austrian": "AUT", "swedish": "SWE",
    "norwegian": "NOR", "danish": "DNK", "finnish": "FIN", "polish": "POL",
    "greek": "GRC", "romanian": "ROU", "bulgarian": "BGR", "hungarian": "HUN",
    "czech": "CZE", "slovak": "SVK", "croatian": "HRV", "serbian": "SRB",
    "albanian": "ALB", "nigerian": "NGA", "kenyan": "KEN", "ghanaian": "GHA",
    "ethiopian": "ETH", "ugandan": "UGA", "tanzanian": "TZA",
    "south african": "ZAF", "zimbabwean": "ZWE", "zambian": "ZMB",
    "malawian": "MWI", "mozambican": "MOZ", "angolan": "AGO",
    "cameroonian": "CMR", "senegalese": "SEN", "ivorian": "CIV",
    "moroccan": "MAR", "algerian": "DZA", "tunisian": "TUN", "libyan": "LBY",
    "somali": "SOM", "eritrean": "ERI", "rwandan": "RWA", "burundian": "BDI",
    "turkish": "TUR", "russian": "RUS", "ukrainian": "UKR", "belarusian": "BLR",
    "kazakh": "KAZ", "uzbek": "UZB", "tajik": "TJK", "kyrgyz": "KGZ",
    "turkmen": "TKM", "azerbaijani": "AZE", "armenian": "ARM",
    "georgian": "GEO", "israeli": "ISR", "palestinian": "PSE",
    "japanese": "JPN", "korean": "KOR", "taiwanese": "TWN",
    "singaporean": "SGP", "cambodian": "KHM", "laotian": "LAO",
    "mongolian": "MNG", "brazilian": "BRA", "argentine": "ARG",
    "argentinian": "ARG", "chilean": "CHL", "colombian": "COL",
    "peruvian": "PER", "venezuelan": "VEN", "mexican": "MEX", "cuban": "CUB",
    "jamaican": "JAM", "haitian": "HTI", "fijian": "FJI",
    "papua new guinean": "PNG", "trinidadian": "TTO", "icelandic": "ISL",
    "namibian": "NAM", "botswanan": "BWA", "lesotho": "LSO",
    "nepalese citizen": "NPL", "bolivian": "BOL", "ecuadorian": "ECU",
    "uruguayan": "URY", "paraguayan": "PRY", "panamanian": "PAN",
    "guyanese": "GUY", "surinamese": "SUR", "malagasy": "MDG",
    "mauritian": "MUS", "seychellois": "SYC", "comorian": "COM",
    "djiboutian": "DJI", "gambian": "GMB", "sierra leonean": "SLE",
    "liberian": "LBR", "togolese": "TGO", "beninese": "BEN",
    "burkinabe": "BFA", "malian": "MLI", "nigerien": "NER", "chadian": "TCD",
    "gabonese": "GAB", "congolese": "COG", "luxembourgish": "LUX",
    "maltese": "MLT", "cypriot": "CYP", "estonian": "EST", "latvian": "LVA",
    "lithuanian": "LTU", "slovenian": "SVN", "bosnian": "BIH",
    "macedonian": "MKD", "montenegrin": "MNE", "moldovan": "MDA",
}

#: Longest first, so "sri lankan" is matched before "lankan" and a two-word
#: demonym is never truncated to its second half.
_DEMONYM_ALTERNATION = "|".join(
    re.escape(word) for word in sorted(_DEMONYMS, key=len, reverse=True)
)

#: A sentinel for "this CV states a nationality, it is not Indian, and the word
#: is not one we can name". A CV saying "Nationality: Zimbabwean" is refused on
#: exactly the same footing as one saying "Pakistani": the demonym table decides
#: how well the refusal reads, never whether it happens.
OTHER_CODE = "OTH"
OTHER_NAME = "another country"

#: "Nationality: Indian", "Citizenship / Nationality : Pakistani", "Nationality- Nepali".
#:
#: The value is free text rather than an alternation of known demonyms. Matching
#: only known words meant an unlisted country read as *no answer at all*, and no
#: answer is accepted — so the filter silently passed every nationality nobody
#: had thought to write down. `_resolve_stated` classifies the value instead.
#:
#: The character class excludes newlines, so a bare "Nationality:" heading in a
#: table cannot reach down and adopt the next field's value.
_STATED_RE = re.compile(
    r"(?:nationality|citizenship|nationaliy|natonality|nationalty)"
    r"\s*(?:/\s*[A-Za-z]+)?\s*[:\-–—]?[ \t]*"
    r"(?P<rest>[^\n]{0,60})",
    re.IGNORECASE,
)

#: Words that begin a sentence *about* nationality rather than an answer to it.
#:
#: "Nationality and religion: Indian, Hindu" and "Nationality proof enclosed"
#: are both prose, and reading their first words as a country is how a filter
#: rejects the candidates it was built to keep. A value opening with one of
#: these is not an answer unless a country is named further along the line.
_PROSE_OPENERS = frozenset({
    "and", "or", "of", "is", "are", "was", "were", "the", "a", "an", "for",
    "in", "by", "with", "as", "to", "from", "at", "on", "if", "any", "all",
    "details", "detail", "proof", "document", "documents", "certificate",
    "copy", "copies", "attached", "enclosed", "status", "information",
    "particulars", "verification", "card", "number", "no", "id", "identity",
    "spouse", "father", "mother", "wife", "husband", "parent", "parents",
})

#: A nationality field left blank. Refusing on these would turn "the applicant
#: did not fill this in" into "the applicant is a foreigner", which is the
#: silent, expensive mistake this module is built to avoid.
_NON_ANSWERS = frozenset({
    "na", "n a", "n/a", "nil", "none", "not specified", "not mentioned",
    "not applicable", "not stated", "unknown", "tbd", "to be confirmed",
    "no", "yes", "male", "female", "single", "married", "date", "dob",
    "age", "sex", "gender", "address", "phone", "email", "name",
})

#: What "Indian" survives an OCR pass as. Consulted only when no exact demonym
#: matched, so "Indonesian" — a real entry in the table — is never caught here
#: by merely resembling "Indian".
_INDIAN_WORDS = ("indian", "india", "bharatiya", "indien")

#: "Place of Birth: Lahore", "Birth Place - Karachi", "POB: Kathmandu".
_BIRTHPLACE_RE = re.compile(
    r"(?:place\s+of\s+birth|birth\s*place|p\.?o\.?b\.?)\s*[:\-–—]?\s*"
    r"(?P<value>[A-Za-z][A-Za-z\s,.'-]{2,40})",
    re.IGNORECASE,
)

#: International dialling codes, longest first so +977 is not read as +97.
#: Only codes that identify one country usefully for this desk.
_DIALLING_CODES: Tuple[Tuple[str, str], ...] = (
    ("880", "BGD"), ("977", "NPL"), ("971", "ARE"), ("966", "SAU"),
    ("974", "QAT"), ("973", "BHR"), ("968", "OMN"), ("965", "KWT"),
    ("963", "SYR"), ("962", "JOR"), ("961", "LBN"), ("960", "MDV"),
    ("249", "SDN"), ("251", "ETH"), ("254", "KEN"), ("256", "UGA"),
    ("234", "NGA"), ("233", "GHA"), ("212", "MAR"), ("216", "TUN"),
    ("975", "BTN"), ("967", "YEM"), ("964", "IRQ"), ("998", "UZB"),
    ("95", "MMR"), ("94", "LKA"), ("93", "AFG"), ("92", "PAK"),
    ("91", "IND"), ("98", "IRN"), ("90", "TUR"), ("86", "CHN"),
    ("84", "VNM"), ("66", "THA"), ("63", "PHL"), ("62", "IDN"),
    ("60", "MYS"), ("65", "SGP"), ("20", "EGY"), ("44", "GBR"),
    ("61", "AUS"), ("81", "JPN"), ("82", "KOR"),
)

_DIALLING_RE = re.compile(
    r"(?:\+|\b00)\s?(?P<code>" +
    "|".join(code for code, _ in sorted(_DIALLING_CODES, key=lambda kv: -len(kv[0]))) +
    r")[\s\-().]*\d",
)
_DIALLING_BY_CODE: Dict[str, str] = dict(_DIALLING_CODES)


# --------------------------------------------------------------------------- #
#  The verdict
# --------------------------------------------------------------------------- #
@dataclass
class ResumeNationality:
    """Which country this CV belongs to, how sure we are, and why."""

    verdict: str = UNDETERMINED
    country_code: Optional[str] = None
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)

    @property
    def country(self) -> str:
        if self.country_code == OTHER_CODE:
            return OTHER_NAME
        return pn.country_name(self.country_code)

    @property
    def is_indian(self) -> bool:
        return self.verdict == INDIAN

    @property
    def is_foreign(self) -> bool:
        return self.verdict == FOREIGN

    def describe(self) -> str:
        """One line, for a log, a notification or an operator screen."""
        if self.verdict == INDIAN:
            head = "Indian candidate"
        elif self.verdict == FOREIGN:
            head = f"other nationality ({self.country})"
        else:
            head = "nationality not stated"
        why = "; ".join(self.evidence[:3]) or "no nationality evidence on the CV"
        return f"{head} [confidence {self.confidence:.2f}] — {why}"

    def as_dict(self) -> Dict[str, object]:
        """Serialisable form, for the ingestion row and the API."""
        return {
            "verdict": self.verdict,
            "country_code": self.country_code,
            "country": self.country,
            "confidence": round(self.confidence, 2),
            "evidence": list(self.evidence),
            "scores": {k: round(v, 2) for k, v in sorted(self.scores.items())},
        }


# --------------------------------------------------------------------------- #
#  Reading the evidence
# --------------------------------------------------------------------------- #
def _award(
    scores: Dict[str, float],
    evidence: List[str],
    code: Optional[str],
    points: float,
    why: str,
) -> None:
    if not code:
        return
    scores[code] = scores.get(code, 0.0) + points
    evidence.append(why)


#: Longest first, so "sri lankan" beats "lankan" and a two-word demonym is
#: never truncated to its second half. Built once; the table does not change.
_DEMONYMS_BY_LENGTH: Tuple[Tuple[str, str], ...] = tuple(
    (word, _DEMONYMS[word]) for word in sorted(_DEMONYMS, key=len, reverse=True)
)


def _named_country(line: str) -> Optional[Tuple[str, str]]:
    """The first country named anywhere on this line, and the word that named it.

    Scanned across the whole line rather than only the field's value, because
    "Nationality and religion: Indian, Hindu" answers the question — just not in
    the first word after the label.
    """
    for word, code in _DEMONYMS_BY_LENGTH:
        if re.search(rf"\b{re.escape(word)}\b", line):
            return code, word
    return None


def _resolve_stated(rest: str) -> Optional[Tuple[str, str]]:
    """Read what a nationality field actually answers, as ``(code, wording)``.

    Three outcomes, and the third is the one that makes this filter cover more
    than the countries somebody remembered to list:

      * a country code, when a country is named on the line;
      * ``OTHER_CODE``, when the field gives a plain answer that is not Indian
        and not a word we know — an unlisted country is still a foreign one;
      * ``None``, when the line is blank, holds a placeholder, or is prose
        *about* nationality rather than an answer to it.

    The order is what keeps it safe. A named country is looked for across the
    whole line first, so "Indian by birth" and "Indian, Hindu" resolve to India
    however they are punctuated. Only when no country is named at all does the
    unrecognised-answer path open, and it is fenced: one to three words, no
    prose opener, no placeholder. Without those fences "Nationality proof
    enclosed" reads as a foreign nationality and an Indian candidate is thrown
    away over a filing note.

    The Indian near-miss check sits between the two. It runs after the exact
    table — so "Indonesian", a real entry, can never be mistaken for a badly
    scanned "Indian" — and before the unrecognised path, so a genuine "lndian"
    with the lowercase L that Tesseract makes of a capital I still gets home.
    """
    line = " ".join(rest.casefold().replace("/", " ").split())
    if not line:
        return None

    named = _named_country(line)
    if named:
        return named

    # The field's own value: up to the first punctuation that ends an answer.
    value = re.split(r"[,;:|(]", line)[0].strip(" .-'")
    if not value or value in _NON_ANSWERS:
        return None

    words = value.split()
    if words[0] in _PROSE_OPENERS:
        return None

    from difflib import SequenceMatcher

    for indian in _INDIAN_WORDS:
        if SequenceMatcher(None, words[0], indian).ratio() >= 0.82:
            return INDIA_CODE, words[0]

    # A nationality runs to three words at most — "Papua New Guinean" is the
    # long case, and it is a real one. Beyond that it is a sentence, and a
    # sentence this module cannot parse is not evidence of a foreign national.
    #
    # The count is a backstop, not the defence: prose is stopped by its opening
    # word above, which catches "details are attached separately" at the first
    # token rather than the fourth.
    if len(words) > 3 or len("".join(c for c in value if c.isalpha())) < 3:
        return None

    return OTHER_CODE, value


#: A field label, its colon and its value, split across lines by the PDF's own
#: text layer. Two-column CVs produce this constantly — the label and the value
#: sit in different boxes, and the extracted order is
#: "Nationality \n : \n Pakistani".
_SPLIT_LABEL = re.compile(r"[ \t]*\n[ \t]*:[ \t]*\n?[ \t]*")


def _joined_labels(text: str) -> str:
    """Put "Label \\n : \\n Value" back on one line before anything reads it.

    Every pattern below expects a stated field to look like `Nationality :
    Pakistani`. In a PDF text layer it frequently does not: the label, the colon
    and the value come out on three separate lines, and the whole
    stated-nationality rule then matches nothing at all.

    A real Pakistani CV was accepted because of exactly this. The same detector
    reading the same résumé returned FOREIGN at 0.92 once the text had been
    de-columnised, and UNDETERMINED at 0.00 straight off the text layer. The
    difference was three newlines.

    Only newlines that sit immediately around a colon are closed up, so line
    structure the other rules rely on is left alone.
    """
    return _SPLIT_LABEL.sub(" : ", text)


def _stated_nationality(text: str, scores: Dict[str, float], evidence: List[str]) -> None:
    """A nationality the CV states outright."""
    for match in _STATED_RE.finditer(text):
        resolved = _resolve_stated(match.group("rest"))
        if resolved:
            code, wording = resolved
            _award(scores, evidence, code, STATED_EVIDENCE,
                   f"stated nationality '{wording}'")


def _birthplace(text: str, scores: Dict[str, float], evidence: List[str]) -> None:
    """A place of birth, matched against the country place tables."""
    markers = pn.place_markers()
    for match in _BIRTHPLACE_RE.finditer(text):
        value = match.group("value").strip()
        for code, patterns in markers.items():
            if any(p.search(value) for p in patterns):
                _award(scores, evidence, code, BIRTHPLACE_EVIDENCE,
                       f"place of birth '{value}'")
                break
        else:
            # The place may be a country or a demonym rather than a city.
            for word, code in _DEMONYMS.items():
                if re.search(rf"\b{re.escape(word)}\b", value, re.IGNORECASE):
                    _award(scores, evidence, code, BIRTHPLACE_EVIDENCE,
                           f"place of birth '{value}'")
                    break


def _places(text: str, scores: Dict[str, float], evidence: List[str]) -> None:
    """Cities and regions named anywhere on the CV.

    One award per country however many of its towns appear: a CV listing four
    Indian employers is not four times the evidence that one is, and letting a
    long work history outvote a stated nationality is how a filter starts
    deciding on volume instead of fact.
    """
    for code, patterns in pn.place_markers().items():
        for pattern in patterns:
            found = pattern.search(text)
            if found:
                _award(scores, evidence, code, PLACE_EVIDENCE,
                       f"mentions {pn.country_name(code)} ('{found.group(0)[:30]}')")
                break


def _dialling(text: str, scores: Dict[str, float], evidence: List[str]) -> None:
    """International dialling codes on phone numbers. One award per country."""
    seen: set = set()
    for match in _DIALLING_RE.finditer(text):
        code = _DIALLING_BY_CODE.get(match.group("code"))
        if code and code not in seen:
            seen.add(code)
            _award(scores, evidence, code, DIALLING_EVIDENCE,
                   f"phone number beginning +{match.group('code')}")


def _passports(
    verdicts: Mapping[int, "pn.NationalityVerdict"],
    scores: Dict[str, float],
    evidence: List[str],
) -> None:
    """Passports the classifier already read in this bundle.

    Free evidence and the best available: `page_classifier` runs
    `passport_nationality` over every page that reads as a booklet, so the
    issuing state is decided before this module is called.
    """
    for number, verdict in sorted(verdicts.items()):
        if verdict.country_code and verdict.verdict in (INDIAN, FOREIGN):
            _award(scores, evidence, verdict.country_code, PASSPORT_EVIDENCE,
                   f"{pn.country_name(verdict.country_code)} passport on page {number}")


def passport_verdicts_from(
    page_texts: Sequence[str], page_kinds: Mapping[int, str],
) -> Dict[int, "pn.NationalityVerdict"]:
    """Read the issuing state of every page the classifier called a passport.

    Only those pages. Running the passport reader over a CV would be a mistake
    with a specific failure: its markers are printed-document markers, and a
    résumé listing Karachi employers and a Lahore school scores Pakistan on the
    same table an actual booklet would. That reading would then arrive here
    weighted as `PASSPORT_EVIDENCE` — a government document's authority granted
    to a work history. The kind check is what keeps the strongest evidence
    attached to the only thing entitled to carry it.
    """
    from app.extraction import page_classifier as pc

    out: Dict[int, "pn.NationalityVerdict"] = {}
    for number, kind in sorted(page_kinds.items()):
        if kind != pc.PASSPORT:
            continue
        index = number - 1
        if 0 <= index < len(page_texts):
            out[number] = pn.detect_passport_country(page_texts[index] or "")
    return out


def detect_resume_nationality(
    text: str,
    passport_verdicts: Optional[Mapping[int, "pn.NationalityVerdict"]] = None,
) -> ResumeNationality:
    """Name the nationality of the candidate this CV belongs to.

    Never raises and never guesses. A CV that says nothing about where its owner
    is from comes back UNDETERMINED with an empty evidence list, which is a
    different answer from FOREIGN and is treated differently downstream.
    """
    result = ResumeNationality()
    text = _joined_labels(text or "")
    if not text.strip() and not passport_verdicts:
        return result

    scores: Dict[str, float] = {}
    evidence: List[str] = []

    _passports(passport_verdicts or {}, scores, evidence)
    _stated_nationality(text, scores, evidence)
    _birthplace(text, scores, evidence)
    _places(text, scores, evidence)
    _dialling(text, scores, evidence)

    result.scores = scores
    if not scores:
        return result

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_code, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

    min_score = float(settings.resume_nationality_min_score)
    margin = float(settings.resume_nationality_margin)

    # Not enough to name anybody, or too close to call. Both are UNDETERMINED,
    # and UNDETERMINED is accepted — a contradictory CV is not a foreign one.
    if top_score < min_score or (top_score - runner_up) < margin:
        result.evidence = [e for e in evidence][:4]
        return result

    result.country_code = top_code
    result.verdict = INDIAN if top_code == INDIA_CODE else FOREIGN
    result.confidence = round(min(0.5 + top_score / 12.0, 0.99), 2)
    named = OTHER_NAME if top_code == OTHER_CODE else pn.country_name(top_code)
    result.evidence = [e for e in evidence if named in e][:4] or evidence[:4]
    return result


# --------------------------------------------------------------------------- #
#  The gate
# --------------------------------------------------------------------------- #
def should_ingest(
    verdict: ResumeNationality,
    *,
    india_only: Optional[bool] = None,
    allow_undetermined: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Whether this CV may reach the résumé endpoint and the candidate database.

    Returns ``(accept, reason)``. The reason is written to the ingestion row and
    shown to the operator, so it is phrased for a human reading a refusal — the
    same contract as `passport_nationality.should_extract`, which this
    deliberately mirrors.
    """
    if india_only is None:
        india_only = bool(settings.resume_india_only)
    if allow_undetermined is None:
        allow_undetermined = bool(settings.resume_nationality_allow_undetermined)

    if not india_only:
        return True, "India-only candidate filter disabled"
    if verdict.verdict == INDIAN:
        return True, verdict.describe()
    if verdict.verdict == FOREIGN:
        return False, f"rejected: {verdict.describe()}"
    if allow_undetermined:
        return True, f"accepted under the undetermined-nationality policy: {verdict.describe()}"
    return False, f"rejected: {verdict.describe()}"


def refuse_foreign_candidate(filename: str, extracted) -> None:
    """Stop a CV this desk cannot place, before it costs anything more.

    Reads the decision `should_ingest` already made and carried on the
    extraction; it does not re-run the detector. One policy evaluated in one
    place is what keeps the upload gate and the database gate from ever
    disagreeing about the same document.

    It lives here rather than in the pipeline because both gates have to reach
    it: `ResumeParser.parse_file` calls it to avoid paying the résumé endpoint
    for a refusal already decided, and the pipeline and the manual-upload
    intake both act on what it raises. The pipeline cannot own it — the parser
    would have to import the pipeline to ask.

    Safe on an extraction that predates the field — an older cached result, a
    stub in a test — which comes back with `nationality_accepted` unset and is
    treated as "nothing to refuse".
    """
    from app.core.exceptions import ForeignNationalityError

    if getattr(extracted, "nationality_accepted", None) is not False:
        return
    reason = getattr(extracted, "nationality_reason", "") or "candidate is not an Indian national"
    raise ForeignNationalityError(
        f"Attachment '{filename}' was not ingested: {reason}",
        verdict=getattr(extracted, "nationality", None),
    )
