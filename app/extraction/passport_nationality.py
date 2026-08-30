"""Which country issued this passport — decided before a single byte is uploaded.

The passport endpoint is not a generic passport reader. It is tuned for the
Indian booklet: the Hindi/English field labels, the `P<IND` MRZ, the file-number
block, the back page carrying father / mother / spouse. Feed it a Philippine or
a Nepali passport and it does not politely decline — it returns a confidently
wrong record, which is worse than no record, because a wrong passport number on
a Gulf visa file is discovered at the embassy counter.

So the rule this module enforces is: **only an Indian passport is sent to the
Veris passport endpoint.** Everything else is recognised, named, logged and
skipped.

The decision is made from the *local* text layer (the PDF's own text, or
Tesseract's read of the scan) — the same text the page classifier already has in
hand. Nothing is uploaded to reach it, so a foreign passport costs zero API
calls rather than one wasted one.

How the country is established, strongest evidence first:

  1. **The MRZ issuing state.** Line 1 of a machine-readable passport is
     ``P<INDKUMAR<<RAJESH<<<...`` — positions 2-4 are the ISO 3166-1 alpha-3 of
     the issuing state. This is the field the document exists to make readable,
     and it outranks everything printed elsewhere on the page.
  2. **The MRZ nationality field**, line 2 positions 10-12.
  3. **The emblem line** — "REPUBLIC OF INDIA", "ISLAMIC REPUBLIC OF PAKISTAN",
     "KINGDOM OF SAUDI ARABIA" — printed at the head of the data page.
  4. **Script and bilingual labels.** Only the Indian booklet prints Devanagari
     field labels; only the Nepali one prints "नेपाल अधिराज्य". Script alone is
     never conclusive, but it is real evidence.
  5. **The nationality field as text**, e.g. "Nationality / राष्ट्रीयता  INDIAN".

Three verdicts come out, not two:

  ``INDIAN``        — send it.
  ``FOREIGN``       — a country was positively identified and it is not India.
                      Never sent, whatever the settings say.
  ``UNDETERMINED``  — the scan was too poor to name a country either way.
                      Whether these are sent is a policy call, and it is a
                      setting (`passport_allow_undetermined_nationality`),
                      because the two failure modes cost different things: send
                      them and an occasional foreign passport slips through;
                      drop them and a genuine Indian passport behind a bad scan
                      is silently lost.

Adding a country is adding a row to `_COUNTRY_MARKERS`. Recognising a country's
MRZ needs nothing at all: every ISO 3166-1 alpha-3 code is already in
`_ISO3_CODES`, so an Estonian passport is identified as foreign on the strength
of its MRZ even though nobody ever wrote a marker for Estonia.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
#  Verdicts
# --------------------------------------------------------------------------- #
INDIAN = "indian"
FOREIGN = "foreign"
UNDETERMINED = "undetermined"

#: The one issuing state the Veris passport endpoint is trained on.
INDIA_CODE = "IND"

# What a country needs to score before it is named at all. Below this the page
# is UNDETERMINED — we know it is a passport, we do not know whose.
_NAME_COUNTRY_SCORE = 2.0
# And how far clear of the runner-up it must be. Two countries within this of
# each other is a contradictory page (a passport photocopied onto a visa page,
# say), and naming either would be a guess.
_WINNING_MARGIN = 1.5


# --------------------------------------------------------------------------- #
#  ISO 3166-1 alpha-3, plus the codes only ever seen in an MRZ
# --------------------------------------------------------------------------- #
# The full set is here so that *any* passport's MRZ identifies it. The named
# markers below cover the countries that actually turn up in an Indian
# recruitment mailbox; this covers the rest.
_ISO3_CODES = frozenset("""
ABW AFG AGO AIA ALA ALB AND ARE ARG ARM ASM ATA ATF ATG AUS AUT AZE
BDI BEL BEN BES BFA BGD BGR BHR BHS BIH BLM BLR BLZ BMU BOL BRA BRB BRN BTN BVT BWA
CAF CAN CCK CHE CHL CHN CIV CMR COD COG COK COL COM CPV CRI CUB CUW CXR CYM CYP CZE
DEU DJI DMA DNK DOM DZA
ECU EGY ERI ESH ESP EST ETH
FIN FJI FLK FRA FRO FSM
GAB GBR GEO GGY GHA GIB GIN GLP GMB GNB GNQ GRC GRD GRL GTM GUF GUM GUY
HKG HMD HND HRV HTI HUN
IDN IMN IND IOT IRL IRN IRQ ISL ISR ITA
JAM JEY JOR JPN
KAZ KEN KGZ KHM KIR KNA KOR KWT
LAO LBN LBR LBY LCA LIE LKA LSO LTU LUX LVA
MAC MAF MAR MCO MDA MDG MDV MEX MHL MKD MLI MLT MMR MNE MNG MNP MOZ MRT MSR MTQ MUS MWI MYS MYT
NAM NCL NER NFK NGA NIC NIU NLD NOR NPL NRU NZL
OMN
PAK PAN PCN PER PHL PLW PNG POL PRI PRK PRT PRY PSE PYF
QAT
REU ROU RUS RWA
SAU SDN SEN SGP SGS SHN SJM SLB SLE SLV SMR SOM SPM SRB SSD STP SUR SVK SVN SWE SWZ SXM SYC SYR
TCA TCD TGO THA TJK TKL TKM TLS TON TTO TUN TUR TUV TWN TZA
UGA UKR UMI URY USA UZB
VAT VCT VEN VGB VIR VNM VUT
WLF WSM
YEM
ZAF ZMB ZWE
""".split())

# Codes an MRZ carries that are not ISO 3166-1: the UK's sub-classes of British
# nationality, the UN's, the stateless/refugee codes, and Germany's historic
# single-letter code padded with fillers.
_MRZ_ONLY_CODES: Dict[str, str] = {
    "GBD": "United Kingdom (British Overseas Territories citizen)",
    "GBN": "United Kingdom (British National Overseas)",
    "GBO": "United Kingdom (British Overseas citizen)",
    "GBP": "United Kingdom (British Protected Person)",
    "GBS": "United Kingdom (British Subject)",
    "D": "Germany",
    "UNO": "United Nations Organization",
    "UNA": "United Nations Agency",
    "UNK": "United Nations Interim Administration (Kosovo)",
    "XXA": "Stateless person",
    "XXB": "Refugee (1951 Convention)",
    "XXC": "Refugee (other)",
    "XXX": "Person of unspecified nationality",
    "XOM": "Sovereign Military Order of Malta",
    "XCC": "Caribbean Community",
    "EUE": "European Union",
    "RKS": "Kosovo",
}

_ALL_CODES = frozenset(_ISO3_CODES) | frozenset(_MRZ_ONLY_CODES)


# --------------------------------------------------------------------------- #
#  Printed markers, by country
# --------------------------------------------------------------------------- #
# One row per country. `strong` is wording that appears on the data page of that
# country's passport and essentially nowhere else — the emblem line, a
# country-specific bilingual label. `weak` is suggestive but shared: a city, a
# script, an authority that also issues other documents.
#
# To support a new country, add a row. Nothing else in this module changes.
_COUNTRY_MARKERS: Dict[str, Dict[str, object]] = {
    "IND": {
        "name": "India",
        "strong": (
            r"republic\s+of\s+india",
            r"भारत\s*गणराज्य",
            r"\bभारत\s*गणरा",
            # The Hindi field labels are printed on the Indian booklet only.
            r"पासपोर्ट",
            r"राष्ट्र\s*कोड",
            r"राष्ट्रीयता",
            r"जन्म\s*तिथि",
            r"समाप्ति\s+की\s+तिथि",
            r"उपनाम\s*/\s*surname",
            r"पिता\s*/\s*कानूनी\s*अभिभावक\s*का\s*नाम",
            r"माता\s*का\s*नाम",
            # The file number block and its wording are Indian-specific.
            r"\bfile\s*(?:no|number)\.?\s*[:\-]?\s*[a-z]{2}\d",
            r"old\s+passport\s+no\.?\s*/\s*(?:file|date|place)",
        ),
        "weak": (
            r"\bindian\b",
            r"\bbharat\b",
            r"\bhindi\b",
            r"passport\s+office",
            # Places of issue printed on Indian passports.
            r"\b(?:delhi|mumbai|chennai|kolkata|bengaluru|bangalore|hyderabad|"
            r"ahmedabad|pune|jaipur|lucknow|chandigarh|kochi|cochin|trivandrum|"
            r"thiruvananthapuram|madurai|trichy|tiruchirappalli|coimbatore|"
            r"visakhapatnam|bhopal|patna|ranchi|guwahati|bhubaneswar|surat|"
            r"nagpur|goa|panaji|shimla|dehradun|srinagar|jammu|raipur|amritsar|"
            r"jalandhar|malappuram|kozhikode|calicut|kannur|thrissur)\b",
        ),
    },
    "NPL": {
        "name": "Nepal",
        "strong": (r"नेपाल\s*अधिराज्य", r"federal\s+democratic\s+republic\s+of\s+nepal",
                   r"\bनेपाल\b", r"government\s+of\s+nepal"),
        "weak": (r"\bnepal(?:i|ese)?\b", r"\bkathmandu\b"),
    },
    "LKA": {
        "name": "Sri Lanka",
        "strong": (r"democratic\s+socialist\s+republic\s+of\s+sri\s+lanka",
                   r"ශ්‍රී\s*ලංකා", r"\bශ්‍රී\b"),
        "weak": (r"\bsri\s+lanka(?:n)?\b", r"\bcolombo\b"),
    },
    "BGD": {
        "name": "Bangladesh",
        "strong": (r"people'?s\s+republic\s+of\s+bangladesh", r"গণপ্রজাতন্ত্রী\s*বাংলাদেশ",
                   r"\bবাংলাদেশ\b"),
        "weak": (r"\bbangladesh(?:i)?\b", r"\bdhaka\b"),
    },
    "PAK": {
        "name": "Pakistan",
        "strong": (r"islamic\s+republic\s+of\s+pakistan", r"اسلامی\s*جمہوریہ\s*پاکستان"),
        "weak": (r"\bpakistan(?:i)?\b", r"\bislamabad\b", r"\bkarachi\b", r"\blahore\b"),
    },
    "BTN": {
        "name": "Bhutan",
        "strong": (r"kingdom\s+of\s+bhutan", r"royal\s+government\s+of\s+bhutan"),
        "weak": (r"\bbhutan(?:ese)?\b", r"\bthimphu\b"),
    },
    "MDV": {
        "name": "Maldives",
        "strong": (r"republic\s+of\s+maldives",),
        "weak": (r"\bmaldiv(?:es|ian)\b", r"\bmal[eé]\b"),
    },
    "AFG": {
        "name": "Afghanistan",
        "strong": (r"islamic\s+(?:republic|emirate)\s+of\s+afghanistan",),
        "weak": (r"\bafghan(?:istan)?\b", r"\bkabul\b"),
    },
    "MMR": {
        "name": "Myanmar",
        "strong": (r"republic\s+of\s+the\s+union\s+of\s+myanmar",),
        "weak": (r"\bmyanmar\b", r"\bburm(?:a|ese)\b", r"\byangon\b"),
    },
    "PHL": {
        "name": "Philippines",
        "strong": (r"republika\s+ng\s+pilipinas", r"republic\s+of\s+the\s+philippines",
                   r"\bpasaporte\b"),
        "weak": (r"\bphilippine?s?\b", r"\bfilipino\b", r"\bmanila\b"),
    },
    "IDN": {
        "name": "Indonesia",
        "strong": (r"republik\s+indonesia", r"republic\s+of\s+indonesia",
                   r"\bpaspor\b", r"kewarganegaraan"),
        "weak": (r"\bindonesian?\b", r"\bjakarta\b"),
    },
    "LKA_": {  # placeholder key never matched; kept out by the loader below
        "name": "",
        "strong": (),
        "weak": (),
    },
    "NGA": {
        "name": "Nigeria",
        "strong": (r"federal\s+republic\s+of\s+nigeria",),
        "weak": (r"\bnigerian?\b", r"\babuja\b", r"\blagos\b"),
    },
    "KEN": {
        "name": "Kenya",
        "strong": (r"republic\s+of\s+kenya", r"jamhuri\s+ya\s+kenya"),
        "weak": (r"\bkenyan?\b", r"\bnairobi\b"),
    },
    "GHA": {
        "name": "Ghana",
        "strong": (r"republic\s+of\s+ghana",),
        "weak": (r"\bghana(?:ian)?\b", r"\baccra\b"),
    },
    "EGY": {
        "name": "Egypt",
        "strong": (r"arab\s+republic\s+of\s+egypt", r"جمهورية\s*مصر\s*العربية"),
        "weak": (r"\begypt(?:ian)?\b", r"\bcairo\b"),
    },
    "ETH": {
        "name": "Ethiopia",
        "strong": (r"federal\s+democratic\s+republic\s+of\s+ethiopia",),
        "weak": (r"\bethiopian?\b", r"\baddis\s+ababa\b"),
    },
    "UGA": {
        "name": "Uganda",
        "strong": (r"republic\s+of\s+uganda",),
        "weak": (r"\bugandan?\b", r"\bkampala\b"),
    },
    "SDN": {
        "name": "Sudan",
        "strong": (r"republic\s+of\s+(?:the\s+)?sudan",),
        "weak": (r"\bsudan(?:ese)?\b", r"\bkhartoum\b"),
    },
    "ARE": {
        "name": "United Arab Emirates",
        "strong": (r"united\s+arab\s+emirates", r"الإمارات\s*العربية\s*المتحدة"),
        "weak": (r"\bemirat(?:i|es)\b", r"\babu\s+dhabi\b"),
    },
    "SAU": {
        "name": "Saudi Arabia",
        "strong": (r"kingdom\s+of\s+saudi\s+arabia", r"المملكة\s*العربية\s*السعودية"),
        "weak": (r"\bsaudi\b", r"\briyadh\b", r"\bjeddah\b"),
    },
    "QAT": {
        "name": "Qatar",
        "strong": (r"state\s+of\s+qatar", r"دولة\s*قطر"),
        "weak": (r"\bqatar(?:i)?\b", r"\bdoha\b"),
    },
    "OMN": {
        "name": "Oman",
        "strong": (r"sultanate\s+of\s+oman", r"سلطنة\s*عمان"),
        "weak": (r"\boman(?:i)?\b", r"\bmuscat\b"),
    },
    "KWT": {
        "name": "Kuwait",
        "strong": (r"state\s+of\s+kuwait", r"دولة\s*الكويت"),
        "weak": (r"\bkuwait(?:i)?\b",),
    },
    "BHR": {
        "name": "Bahrain",
        "strong": (r"kingdom\s+of\s+bahrain", r"مملكة\s*البحرين"),
        "weak": (r"\bbahrain(?:i)?\b", r"\bmanama\b"),
    },
    "GBR": {
        "name": "United Kingdom",
        "strong": (r"united\s+kingdom\s+of\s+great\s+britain",
                   r"british\s+citizen", r"her\s+britannic\s+majesty",
                   r"his\s+britannic\s+majesty"),
        "weak": (r"\bbritish\b", r"\bunited\s+kingdom\b", r"\blondon\b"),
    },
    "USA": {
        "name": "United States of America",
        "strong": (r"united\s+states\s+of\s+america",
                   r"the\s+secretary\s+of\s+state\s+of\s+the\s+united\s+states"),
        # "INDIANA" must never read as India: \b keeps them apart, and the
        # United States row is where a place-of-birth "INDIANA" actually lands.
        "weak": (r"\bunited\s+states\b", r"\bu\.?s\.?a\.?\b", r"\bindiana\b"),
    },
    "CAN": {
        "name": "Canada",
        "strong": (r"\bcanada\s*/\s*canada\b", r"passeport\s+canadien"),
        "weak": (r"\bcanad(?:a|ian)\b", r"\bottawa\b"),
    },
    "AUS": {
        "name": "Australia",
        "strong": (r"commonwealth\s+of\s+australia", r"australian\s+passport"),
        "weak": (r"\baustralian?\b", r"\bcanberra\b"),
    },
    "MYS": {
        "name": "Malaysia",
        "strong": (r"\bmalaysia\b\s*/?\s*\bmalaysia\b", r"warganegara",
                   r"jabatan\s+imigresen"),
        "weak": (r"\bmalaysian?\b", r"\bkuala\s+lumpur\b"),
    },
    "SGP": {
        "name": "Singapore",
        "strong": (r"republic\s+of\s+singapore",),
        "weak": (r"\bsingapore(?:an)?\b",),
    },
    "CHN": {
        "name": "China",
        "strong": (r"people'?s\s+republic\s+of\s+china", r"中华人民共和国"),
        "weak": (r"\bchin(?:a|ese)\b", r"\bbeijing\b"),
    },
    "THA": {
        "name": "Thailand",
        "strong": (r"kingdom\s+of\s+thailand", r"ประเทศไทย"),
        "weak": (r"\bthai(?:land)?\b", r"\bbangkok\b"),
    },
    "VNM": {
        "name": "Vietnam",
        "strong": (r"socialist\s+republic\s+of\s+viet\s*nam", r"cộng\s+hòa\s+xã\s+hội"),
        "weak": (r"\bviet\s*nam(?:ese)?\b", r"\bhanoi\b"),
    },
    "IRN": {
        "name": "Iran",
        "strong": (r"islamic\s+republic\s+of\s+iran", r"جمهوری\s*اسلامی\s*ایران"),
        "weak": (r"\biran(?:ian)?\b", r"\btehran\b"),
    },
    "IRQ": {
        "name": "Iraq",
        "strong": (r"republic\s+of\s+iraq", r"جمهورية\s*العراق"),
        "weak": (r"\biraq(?:i)?\b", r"\bbaghdad\b"),
    },
    "JOR": {
        "name": "Jordan",
        "strong": (r"hashemite\s+kingdom\s+of\s+jordan",),
        "weak": (r"\bjordan(?:ian)?\b", r"\bamman\b"),
    },
    "LBN": {
        "name": "Lebanon",
        "strong": (r"republic\s+of\s+lebanon", r"الجمهورية\s*اللبنانية"),
        "weak": (r"\bleban(?:on|ese)\b", r"\bbeirut\b"),
    },
    "SYR": {
        "name": "Syria",
        "strong": (r"syrian\s+arab\s+republic",),
        "weak": (r"\bsyria(?:n)?\b", r"\bdamascus\b"),
    },
    "YEM": {
        "name": "Yemen",
        "strong": (r"republic\s+of\s+yemen", r"الجمهورية\s*اليمنية"),
        "weak": (r"\byemen(?:i)?\b", r"\bsana'?a\b"),
    },
    "TUR": {
        "name": "Türkiye",
        "strong": (r"republic\s+of\s+t[uü]rkiye", r"t[uü]rkiye\s+cumhuriyeti"),
        "weak": (r"\bturk(?:ey|ish|iye)\b", r"\bankara\b"),
    },
    "RUS": {
        "name": "Russia",
        "strong": (r"russian\s+federation", r"РОССИЙСКАЯ\s+ФЕДЕРАЦИЯ"),
        "weak": (r"\bruss(?:ia|ian)\b", r"\bmoscow\b"),
    },
    "UKR": {
        "name": "Ukraine",
        "strong": (r"\bukraine\s*/\s*укра[їi]на\b", r"УКРАЇНА"),
        "weak": (r"\bukrain(?:e|ian)\b", r"\bkyiv\b", r"\bkiev\b"),
    },
    "ZAF": {
        "name": "South Africa",
        "strong": (r"republic\s+of\s+south\s+africa",),
        "weak": (r"\bsouth\s+africa(?:n)?\b", r"\bpretoria\b"),
    },
}
# The placeholder row above exists only to document the shape; drop it.
_COUNTRY_MARKERS.pop("LKA_", None)


def _compile_markers() -> Dict[str, Dict[str, Tuple[re.Pattern, ...]]]:
    compiled: Dict[str, Dict[str, Tuple[re.Pattern, ...]]] = {}
    for code, row in _COUNTRY_MARKERS.items():
        compiled[code] = {
            "strong": tuple(
                re.compile(p, re.IGNORECASE) for p in row.get("strong", ())  # type: ignore[arg-type]
            ),
            "weak": tuple(
                re.compile(p, re.IGNORECASE) for p in row.get("weak", ())  # type: ignore[arg-type]
            ),
        }
    return compiled


_COMPILED_MARKERS = _compile_markers()

#: Human-readable name for any code we might report.
_COUNTRY_NAMES: Dict[str, str] = {
    **{code: str(row["name"]) for code, row in _COUNTRY_MARKERS.items()},
    **_MRZ_ONLY_CODES,
}


def country_name(code: Optional[str]) -> str:
    """A printable name for an ISO3 code, falling back to the code itself."""
    if not code:
        return "unknown"
    return _COUNTRY_NAMES.get(code, code)


# --------------------------------------------------------------------------- #
#  MRZ
# --------------------------------------------------------------------------- #
# Tesseract reads the chevron filler as almost anything with a diagonal in it.
# Everything in this table collapses to '<' before the MRZ is sliced, so a line
# read as "P(INDKUMAR((RAJESH" still yields IND.
_FILLER_CHARS = "<({[]})«»|¦!¡†‡~^\u2039\u203a\u00ab\u00bb"
_FILLER_TRANSLATION = {ord(c): "<" for c in _FILLER_CHARS}

# Digit/letter confusions, applied *only* to a 3-character country code, where
# the alphabet is known to be letters. Applying them to the whole line would
# corrupt the passport number, which is legitimately alphanumeric.
_CODE_DIGIT_TO_LETTER = str.maketrans({
    "0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "7": "T", "8": "B",
})

# A line that could be an MRZ row: long, and made almost entirely of the MRZ
# alphabet once the filler variants are folded in.
_MRZ_ALPHABET_RE = re.compile(r"^[A-Z0-9<]+$")
_MIN_MRZ_LINE = 24


def _normalise_mrz_line(line: str) -> str:
    """Fold OCR noise into the MRZ alphabet: upper, filler-folded, despaced.

    Spaces are dropped rather than folded to '<' because Tesseract inserts them
    freely inside a chevron run; treating each as a filler would shift every
    field to the right and hand back the wrong three characters.
    """
    folded = line.upper().translate(_FILLER_TRANSLATION)
    return re.sub(r"[\s\u00a0]+", "", folded)


def mrz_lines(text: str) -> List[str]:
    """The lines of `text` that read as machine-readable-zone rows."""
    found: List[str] = []
    for raw in (text or "").splitlines():
        candidate = _normalise_mrz_line(raw)
        if len(candidate) < _MIN_MRZ_LINE:
            continue
        if not _MRZ_ALPHABET_RE.match(candidate):
            continue
        # A run of capitals with no filler at all is a heading in caps, not an
        # MRZ row. Every real TD3 row is filler-padded to 44 characters.
        if candidate.count("<") < 2:
            continue
        found.append(candidate)
    return found


def _resolve_code(raw: Optional[str]) -> Tuple[Optional[str], bool]:
    """Map three noisy characters onto an ISO3 code.

    Returns ``(code, exact)``. A near-miss is accepted only when exactly one
    known code sits one substitution away — "IND" and "IRN" are two apart, so
    this cannot quietly turn one country into another, but "1ND" and "INO"
    both resolve to IND.
    """
    if not raw:
        return None, False
    cleaned = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if not cleaned:
        return None, False

    # Germany's MRZ code is a bare "D" padded with fillers.
    if cleaned == "D":
        return "D", True

    lettered = cleaned.translate(_CODE_DIGIT_TO_LETTER)
    if lettered in _ALL_CODES:
        return lettered, True
    if len(lettered) != 3:
        return None, False

    near = [
        code
        for code in _ALL_CODES
        if len(code) == 3 and sum(a != b for a, b in zip(code, lettered)) == 1
    ]
    if len(near) == 1:
        return near[0], False
    return None, False


@dataclass
class MRZReading:
    """What the machine-readable zone said, if it could be read at all."""

    issuing_state: Optional[str] = None
    nationality: Optional[str] = None
    issuing_state_exact: bool = False
    nationality_exact: bool = False
    line_count: int = 0

    @property
    def present(self) -> bool:
        return bool(self.issuing_state or self.nationality)


def read_mrz(text: str) -> MRZReading:
    """Pull the issuing state and the nationality out of a TD3 MRZ.

    Both fields are read wherever they can be found rather than insisting on a
    well-formed pair of 44-character lines: a scan clipped at the bottom edge
    routinely yields line 1 alone, and line 1 is the field that matters most.
    """
    lines = mrz_lines(text)
    reading = MRZReading(line_count=len(lines))

    for line in lines:
        # Line 1: 'P', one type character, then the three-character state.
        if reading.issuing_state is None and line.startswith("P"):
            code, exact = _resolve_code(line[2:5])
            if code:
                reading.issuing_state, reading.issuing_state_exact = code, exact
                continue
            # Germany: "P<D<<SCHMIDT<<..." — the code is one letter plus filler.
            if line[2:3] == "D" and line[3:4] == "<":
                reading.issuing_state, reading.issuing_state_exact = "D", True
                continue

        # Line 2: nine characters of passport number, a check digit, then the
        # three-character nationality.
        if reading.nationality is None and len(line) >= 13 and not line.startswith("P<"):
            code, exact = _resolve_code(line[10:13])
            if code:
                reading.nationality, reading.nationality_exact = code, exact

    return reading


# --------------------------------------------------------------------------- #
#  The printed nationality field
# --------------------------------------------------------------------------- #
# "Nationality / राष्ट्रीयता    INDIAN" — the label, then the demonym within a
# short reach. Kept tight so a nationality on the facing page is not captured.
_NATIONALITY_FIELD_RE = re.compile(
    r"(?:nationality|nationalit[eé]|राष्ट्रीयता|الجنسية)\s*(?:/[^\n]{0,40})?"
    r"[\s:\-]{0,6}([A-Za-z][A-Za-z\s]{2,30})",
    re.IGNORECASE,
)

# Demonym -> ISO3, for the nationality field only. Deliberately small: it is a
# disambiguator on top of the marker table, not a second copy of it.
_DEMONYMS: Dict[str, str] = {
    "indian": "IND", "india": "IND",
    "nepali": "NPL", "nepalese": "NPL", "nepal": "NPL",
    "sri lankan": "LKA", "srilankan": "LKA",
    "bangladeshi": "BGD", "bangladesh": "BGD",
    "pakistani": "PAK", "pakistan": "PAK",
    "bhutanese": "BTN", "maldivian": "MDV", "afghan": "AFG",
    "myanmar": "MMR", "burmese": "MMR",
    "filipino": "PHL", "filipina": "PHL", "philippine": "PHL",
    "indonesian": "IDN", "malaysian": "MYS", "singaporean": "SGP",
    "thai": "THA", "vietnamese": "VNM", "chinese": "CHN",
    "nigerian": "NGA", "kenyan": "KEN", "ghanaian": "GHA",
    "egyptian": "EGY", "ethiopian": "ETH", "ugandan": "UGA", "sudanese": "SDN",
    "emirati": "ARE", "saudi": "SAU", "qatari": "QAT", "omani": "OMN",
    "kuwaiti": "KWT", "bahraini": "BHR",
    "british": "GBR", "american": "USA", "canadian": "CAN", "australian": "AUS",
    "iranian": "IRN", "iraqi": "IRQ", "jordanian": "JOR", "lebanese": "LBN",
    "syrian": "SYR", "yemeni": "YEM", "turkish": "TUR", "russian": "RUS",
    "ukrainian": "UKR", "south african": "ZAF",
}


def _nationality_field_code(text: str) -> Optional[str]:
    for match in _NATIONALITY_FIELD_RE.finditer(text or ""):
        value = re.sub(r"\s+", " ", match.group(1)).strip().lower()
        if not value:
            continue
        if value in _DEMONYMS:
            return _DEMONYMS[value]
        # The captured run can trail into the next label ("INDIAN Date of
        # birth"); the first one or two words carry the answer.
        words = value.split()
        for width in (2, 1):
            head = " ".join(words[:width])
            if head in _DEMONYMS:
                return _DEMONYMS[head]
    return None


# --------------------------------------------------------------------------- #
#  The verdict
# --------------------------------------------------------------------------- #
@dataclass
class NationalityVerdict:
    """Which country issued this passport, how sure we are, and why."""

    verdict: str = UNDETERMINED
    country_code: Optional[str] = None
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    mrz: MRZReading = field(default_factory=MRZReading)
    scores: Dict[str, float] = field(default_factory=dict)

    @property
    def country(self) -> str:
        return country_name(self.country_code)

    @property
    def is_indian(self) -> bool:
        return self.verdict == INDIAN

    @property
    def is_foreign(self) -> bool:
        return self.verdict == FOREIGN

    def describe(self) -> str:
        """One line, for a log or an operator screen."""
        if self.verdict == INDIAN:
            head = "Indian passport"
        elif self.verdict == FOREIGN:
            head = f"foreign passport ({self.country})"
        else:
            head = "passport of undetermined nationality"
        why = "; ".join(self.evidence[:3]) or "no country evidence on the page"
        return f"{head} [confidence {self.confidence:.2f}] — {why}"

    def as_dict(self) -> Dict[str, object]:
        """Serialisable form, for the ingestion row and the API."""
        return {
            "verdict": self.verdict,
            "country_code": self.country_code,
            "country": self.country,
            "confidence": round(self.confidence, 2),
            "evidence": list(self.evidence),
            "mrz_issuing_state": self.mrz.issuing_state,
            "mrz_nationality": self.mrz.nationality,
        }


def detect_passport_country(text: str) -> NationalityVerdict:
    """Name the issuing country of the passport on this page.

    Never raises and never guesses: a page it cannot read comes back
    UNDETERMINED with the evidence list empty, which is a different answer from
    FOREIGN and is treated differently downstream.
    """
    text = text or ""
    verdict = NationalityVerdict()
    if not text.strip():
        return verdict

    scores: Dict[str, float] = {}
    evidence: List[str] = []

    def award(code: Optional[str], points: float, why: str) -> None:
        if not code:
            return
        scores[code] = scores.get(code, 0.0) + points
        evidence.append(why)

    # ---- 1. The MRZ, which outranks anything printed on the page ---------- #
    reading = read_mrz(text)
    verdict.mrz = reading
    if reading.issuing_state:
        exact = reading.issuing_state_exact
        award(
            reading.issuing_state,
            5.0 if exact else 3.0,
            f"MRZ issuing state {reading.issuing_state}"
            + ("" if exact else " (OCR-corrected)"),
        )
    if reading.nationality:
        exact = reading.nationality_exact
        award(
            reading.nationality,
            3.0 if exact else 2.0,
            f"MRZ nationality {reading.nationality}"
            + ("" if exact else " (OCR-corrected)"),
        )

    # ---- 2. The printed nationality field --------------------------------- #
    field_code = _nationality_field_code(text)
    if field_code:
        award(field_code, 2.0, f"nationality field reads {country_name(field_code)}")

    # ---- 3. Emblem lines, bilingual labels, script ------------------------ #
    for code, groups in _COMPILED_MARKERS.items():
        hits = 0
        for pattern in groups["strong"]:
            if pattern.search(text):
                hits += 1
        if hits:
            award(code, min(2.0 * hits, 5.0), f"{country_name(code)} document markers x{hits}")
        weak = sum(1 for pattern in groups["weak"] if pattern.search(text))
        if weak:
            award(code, min(0.5 * weak, 1.5), f"{country_name(code)} incidental mentions x{weak}")

    if not scores:
        return verdict

    verdict.scores = {k: round(v, 2) for k, v in scores.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_code, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

    # An exact MRZ issuing state is the document's own machine-readable answer.
    # It wins outright — a US passport whose bearer was born in Indiana must not
    # be dragged towards India by the place-of-birth line.
    if reading.issuing_state and reading.issuing_state_exact:
        top_code, top_score = reading.issuing_state, scores[reading.issuing_state]
        runner_up = max(
            (v for k, v in scores.items() if k != top_code),
            default=0.0,
        )

    if top_score < _NAME_COUNTRY_SCORE or (top_score - runner_up) < _WINNING_MARGIN:
        # Contradictory or thin. Keep the evidence for the operator, name nothing.
        verdict.evidence = evidence[:6]
        verdict.confidence = round(min(top_score / 10.0, 0.45), 2)
        return verdict

    verdict.country_code = top_code
    verdict.verdict = INDIAN if top_code == INDIA_CODE else FOREIGN
    verdict.confidence = round(min(0.5 + top_score / 12.0, 0.99), 2)
    verdict.evidence = [e for e in evidence if country_name(top_code) in e or "MRZ" in e][:4] or evidence[:4]
    return verdict


def should_extract(
    verdict: NationalityVerdict,
    *,
    india_only: bool = True,
    allow_undetermined: bool = True,
) -> Tuple[bool, str]:
    """Whether this passport may be sent to the Veris passport endpoint.

    Returns ``(send, reason)``; the reason is written to the ingestion row and
    shown to the operator, so it is phrased for a human reading a skipped pass.
    """
    if not india_only:
        return True, "India-only passport filter disabled"
    if verdict.verdict == INDIAN:
        return True, verdict.describe()
    if verdict.verdict == FOREIGN:
        return False, f"not sent: {verdict.describe()}"
    if allow_undetermined:
        return True, f"sent under the undetermined-nationality policy: {verdict.describe()}"
    return False, f"not sent: {verdict.describe()}"


def detect_pages(texts: Sequence[str], pages: Sequence[int]) -> Dict[int, NationalityVerdict]:
    """Run the detector over specific 1-based page numbers of a bundle."""
    out: Dict[int, NationalityVerdict] = {}
    for number in pages:
        index = number - 1
        out[number] = detect_passport_country(
            texts[index] if 0 <= index < len(texts) else ""
        )
    return out


def combine(verdicts: Sequence[NationalityVerdict]) -> NationalityVerdict:
    """One verdict for a passport that spans several pages.

    A booklet scanned front and back gives a data page carrying the MRZ and a
    back page carrying only father/mother/spouse. The pages are one document, so
    the most confident page speaks for all of them — but a single FOREIGN page
    vetoes the set, because a bundle holding one Indian and one foreign passport
    must not send the foreign one on the Indian one's evidence.
    """
    real = [v for v in verdicts if v is not None]
    if not real:
        return NationalityVerdict()
    foreign = [v for v in real if v.is_foreign]
    if foreign:
        return max(foreign, key=lambda v: v.confidence)
    return max(real, key=lambda v: v.confidence)
