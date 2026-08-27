"""Find the résumé inside a document that is really an application bundle.

Candidates rarely send a clean two-page CV. They send one PDF holding a CV, a
trade certificate, three experience letters, a passport scan and a safety card,
in whatever order the pages came off the scanner. The CV can start on page 1 or
on page 15, and the file is called ``cv.pdf`` either way.

This module does two page-level jobs:

  * **Locate** the pages that actually carry the candidate's profile, so OCR and
    the LLM run on those and not on the twelve certificate scans around them.
  * **Verify** that a résumé is present at all, from the *content*. A file named
    ``resume.pdf`` whose pages are an invoice or a hall ticket is rejected here,
    before any money is spent on it.

Everything is scored from signals that appear in real résumés across collar
colours — a contact block, section headings, employment date ranges, job titles
(``EOT Crane Operator`` counts exactly as much as ``Backend Engineer``) — and
scored *down* by the vocabulary that only ever appears on the other documents in
the bundle. No filename is consulted, by design.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.config import settings
from app.extraction import passport_nationality as pn
from app.logging_config import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
#  Page kinds
# --------------------------------------------------------------------------- #
RESUME = "resume"
CERTIFICATE = "certificate"
EXPERIENCE_LETTER = "experience_letter"
ID_DOCUMENT = "id_document"
# Two *specialisations* of ID_DOCUMENT. The generic bucket is what the résumé
# scorer penalises with; these two are what the multipass extractor routes to
# the Aadhaar and passport OCR endpoints, and they are only ever assigned from
# document-level evidence (see `classify_multipass`).
AADHAAR = "aadhaar"
PASSPORT = "passport"
DOCUMENT = "document"    # generic ID documents (CNIC, Iqama, etc)
OTHER = "other"          # invoice, hall ticket, receipt — the reject bucket
UNKNOWN = "unknown"      # has content, commits to nothing — never a rejection
BLANK = "blank"

# A page must clear this to seed a résumé region on its own.
_RESUME_SEED_SCORE = 2.5
# A page next to a résumé page needs far less to be read as its continuation:
# page 2 of a CV routinely has no name, no email and no phone.
_CONTINUATION_SCORE = 1.0
# And sometimes it has nothing at all to score on. A real CV's page 2 read
# "EHS activities - Environmental Aspect/Impact, HIRA, in line with ..." — three
# thousand characters of job responsibilities, no heading, no date, no contact
# block, so it scored 0.00 and was dropped from the extraction. Prose that sits
# directly behind the résumé and carries none of the markings of another
# document is part of the résumé. Bounded, so an unmarked tail cannot run away.
_WEAK_CONTINUATION_PAGES = 2
# Below this a page holds no usable text at all (a scan with a failed OCR).
_MIN_PAGE_CHARS = 40
# The most any amount of other-document wording can cost one page. Without a
# ceiling an invoice's twelve markers would swamp every positive signal, and a
# CV that quotes a few of them would never recover.
_MAX_MARKER_PENALTY = 4.0


# --------------------------------------------------------------------------- #
#  Résumé signals
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
# Deliberately the same shape the parser uses, so a page that scores a phone is
# a page the parser can actually pull one from.
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")

# A page whose title is literally "CURRICULUM VITAE" needs no other evidence.
_RESUME_TITLE_RE = re.compile(
    r"^\W*(curriculum\s*vitae|resum[eé]|c\.?\s*v\.?|bio[\s-]*data|"
    r"personal\s+(?:profile|resume)|candidate\s+profile)\W*$",
    re.IGNORECASE,
)

# Section headings, in the wording used by both software and trade résumés.
_HEADINGS = {
    "objective", "career objective", "professional objective",
    "summary", "profile summary", "professional summary", "career summary",
    "personal details", "personal information", "personal profile",
    "personal data", "about me", "personal particulars",
    "work experience", "professional experience", "employment history",
    "employment record", "experience", "work history", "career history",
    "job responsibilities", "duties and responsibilities", "responsibilities",
    "education", "educational qualification", "educational qualifications",
    "academic qualification", "academic qualifications", "academics",
    "qualifications", "educational background",
    "skills", "key skills", "technical skills", "core competencies",
    "competencies", "areas of expertise", "skill set", "trade skills",
    "machinery handled", "equipment handled", "tools handled", "strengths",
    "certifications", "certificates", "licenses", "licences", "trainings",
    "training", "courses", "safety training",
    "projects", "key projects", "major projects", "site experience",
    "achievements", "accomplishments", "awards", "honors", "honours",
    "languages", "languages known", "linguistic proficiency",
    "declaration", "references", "referees", "hobbies", "interests",
    "passport details", "visa status", "availability",
}
_MAX_HEADING_LEN = 40
# How many words of a flattened line may be read as its heading — enough for
# "DUTIES AND RESPONSIBILITIES", not enough for a sentence in capitals.
_MAX_INLINE_HEADING_WORDS = 4

# Employment / education date ranges: "Jan 2019 - Present", "2015 – 2018".
_MONTH = (
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
)
_DATE_TOKEN = rf"(?:{_MONTH}\.?[\s,'-]*\d{{2,4}}|\d{{1,2}}[/-]\d{{4}}|(?:19|20)\d{{2}})"
_DATE_RANGE_RE = re.compile(
    rf"{_DATE_TOKEN}\s*(?:[-–—]|to|till|until)\s*(?:{_DATE_TOKEN}|present|current|till\s*date|now)",
    re.IGNORECASE,
)

# Job titles. Trade roles are first-class here: a bundle from a fabrication yard
# is the case this whole module exists for.
_JOB_TITLE_RE = re.compile(
    r"\b(?:"
    r"engineer|developer|programmer|analyst|architect|administrator|consultant|"
    r"manager|supervisor|foreman|superintendent|coordinator|executive|officer|"
    r"technician|mechanic|fitter|welder|fabricator|machinist|operator|driver|"
    r"electrician|plumber|carpenter|mason|rigger|scaffolder|painter|storekeeper|"
    r"inspector|surveyor|draughtsman|draftsman|designer|estimator|planner|"
    r"helper|labour|labor|assistant|trainee|apprentice|intern|"
    r"nurse|accountant|teacher|lecturer|clerk|receptionist|chef|steward"
    r")s?\b",
    re.IGNORECASE,
)

# Machinery and trade nouns that only ever show up in a work-history bullet.
_TRADE_NOUN_RE = re.compile(
    r"\b(?:eot\s*crane|overhead\s*crane|mobile\s*crane|forklift|cnc|vmc|lathe|"
    r"tig|mig|arc\s*weld|smaw|gtaw|gmaw|piping|pipe\s*fitting|hvac|plc|scada|"
    r"hydraulic|pneumatic|boiler|turbine|shutdown|fabrication|erection|"
    r"scaffolding|rigging|blueprint|isometric|welding)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
#  The other documents in the bundle
# --------------------------------------------------------------------------- #
# `strong` markers identify a page on their own; the rest need corroboration.
_DOC_MARKERS: Dict[str, Dict[str, Sequence[str]]] = {
    CERTIFICATE: {
        "strong": (
            r"this\s+is\s+to\s+certify\s+that",
            r"certificate\s+of\s+(?:completion|participation|achievement|merit|training)",
            r"has\s+successfully\s+completed\s+the",
            r"is\s+hereby\s+awarded",
        ),
        # "date of issue" is not here: a résumé's passport block states one.
        "weak": (
            r"certificate\s+no\.?", r"awarded\s+to", r"course\s+duration",
            r"grade\s+obtained", r"authorised\s+signatory",
        ),
    },
    EXPERIENCE_LETTER: {
        "strong": (
            r"to\s+whom\s+it\s+may\s+concern",
            r"experience\s+certificate",
            r"service\s+certificate",
            r"relieving\s+letter",
            r"we\s+(?:hereby\s+)?certify\s+that\s+(?:mr|ms|mrs)",
            r"has\s+been\s+working\s+with\s+(?:us|our)",
            r"was\s+employed\s+with\s+(?:us|our)",
        ),
        "weak": (
            r"his\s+conduct", r"her\s+conduct", r"we\s+wish\s+(?:him|her)",
            r"last\s+drawn\s+salary", r"yours\s+faithfully", r"human\s+resources?\s+department",
        ),
    },
    ID_DOCUMENT: {
        # Only the wording of the *document itself* is strong. "Passport No",
        # "Driving Licence" and "Aadhaar" are demoted to weak because a trade
        # résumé lists all three as personal details — stating a credential is
        # not the same as being it.
        "strong": (
            r"republic\s+of\s+\w+", r"government\s+of\s+\w+", r"identity\s+card",
            r"emirates\s+id", r"labour\s+card", r"resident\s+identity",
            r"id\s+card", r"\bid\s+no\b", r"computerized\s+national",
            r"\bcnic\b", r"kingdom\s+of\s+saudi\s+arabia", r"\biqama\b",
            r"state\s+of\s+qatar", r"civil\s+id", r"ministry\s+of\s+interior",
        ),
        # "Nationality", "Blood group", "Date of expiry" and "Place of issue"
        # were weak markers here until a real CV lost a page to them: Indian and
        # Gulf résumés put exactly those fields in a PERSONAL DETAILS block, and
        # this module's own heading list accepts "passport details" as a résumé
        # section. A CV stating its passport number is not a passport.
        "weak": (
            r"holder'?s\s+signature", r"machine\s+readable\s+zone",
            r"see\s+reverse\s+of\s+this\s+card", r"driving\s+licen[cs]e",
            r"aadhaar", r"passport\s+no\.?", r"\bid\b", r"identification",
            r"card\s+number", r"national\s+no",
        ),
    },
    OTHER: {
        "strong": (
            r"tax\s+invoice", r"\bgstin\b", r"hall\s+ticket", r"admit\s+card",
            r"payment\s+receipt", r"amount\s+(?:paid|due)", r"invoice\s+no\.?",
            r"order\s+(?:id|no)\.?", r"transaction\s+id", r"bill\s+to",
            r"one[\s-]*time\s+password", r"\botp\b",
        ),
        "weak": (
            r"\bsubtotal\b", r"\bgrand\s+total\b", r"\bdue\s+date\b", r"\bqty\b",
            r"roll\s+(?:no|number)", r"examination\s+centre", r"seat\s+no",
            r"terms\s+and\s+conditions", r"unsubscribe",
        ),
    },
}
_DOC_MARKERS_COMPILED = {
    kind: {
        weight: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        for weight, patterns in groups.items()
    }
    for kind, groups in _DOC_MARKERS.items()
}


# --------------------------------------------------------------------------- #
@dataclass
class PageSignals:
    """What was actually found on one page. Kept for logging and for tests."""

    chars: int = 0
    has_resume_title: bool = False
    has_email: bool = False
    has_phone: bool = False
    headings: List[str] = field(default_factory=list)
    date_ranges: int = 0
    job_titles: int = 0
    trade_nouns: int = 0
    marker_hits: Dict[str, float] = field(default_factory=dict)

    @property
    def positive_count(self) -> int:
        return sum((
            self.has_resume_title, self.has_email, self.has_phone,
            bool(self.headings), bool(self.date_ranges),
            bool(self.job_titles), bool(self.trade_nouns),
        ))


@dataclass
class PageClassification:
    page_number: int            # 1-based, as a human would cite it
    kind: str
    score: float
    signals: PageSignals


@dataclass
class DocumentClassification:
    """Where the résumé is, and whether there is one."""

    is_resume: bool
    confidence: float                       # 0.0–1.0, < 0.30 when rejected
    resume_pages: List[int]                 # 1-based page numbers, in order
    pages: List[PageClassification]
    reason: str

    @property
    def page_kinds(self) -> Dict[int, str]:
        return {p.page_number: p.kind for p in self.pages}


# --------------------------------------------------------------------------- #
#  Signal collection
# --------------------------------------------------------------------------- #
_BULLET_PREFIX = re.compile(r"^[\s•●▪·*\-–—\d.)]+")


def _canonical_heading(text: str) -> str | None:
    """Return the canonical heading `text` spells, or None."""
    cleaned = text.strip().rstrip(":").strip()
    if not cleaned:
        return None
    # Headings survive OCR as "E D U C A T I O N" often enough to be worth it.
    normalised = re.sub(r"[^a-z&\s]", " ", cleaned.lower())
    normalised = " ".join(normalised.split())
    if not normalised:
        return None
    if normalised in _HEADINGS:
        return normalised
    despaced = normalised.replace(" ", "")
    for heading in _HEADINGS:
        if despaced == heading.replace(" ", ""):
            return heading
    return None


def _heading_on(line: str) -> str | None:
    """Return the canonical heading if this line *is* a section heading."""
    cleaned = _BULLET_PREFIX.sub("", line).strip()
    if not cleaned or len(cleaned) > _MAX_HEADING_LEN:
        return None
    return _canonical_heading(cleaned)


def _is_capitalised_word(word: str) -> bool:
    """True for a word set in capitals — "EDUCATION", "B.E.", "&" is not one."""
    return word == word.upper() and any(ch.isalpha() for ch in word)


def _inline_heading_on(line: str) -> str | None:
    """Return the heading this line *opens with*, when OCR lost its line break.

    Veris returns a flattened one-page CV as "EDUCATION B.E. Computer Science,
    Anna University, 2020 - 2024" — every heading sharing a line with the
    section beneath it. Headings are the strongest structural signal a page has,
    and losing them to a missing newline is what left a real CV with nothing to
    score on and got it rejected as a certificate.

    Two conditions keep this from inventing headings. The run has to be set in
    capitals, so "Experience with React and Django" stays a sentence; and the
    remainder has to read like body text, so a page typeset entirely in capitals
    is not mistaken for a heading followed by its section.
    """
    words = line.split()
    if len(words) < 2:
        return None

    run = 0
    while (
        run < len(words)
        and run < _MAX_INLINE_HEADING_WORDS
        and _is_capitalised_word(words[run])
    ):
        run += 1
    if run == 0 or run == len(words):
        return None

    remainder = " ".join(words[run:])
    if not any(ch.islower() for ch in remainder):
        return None

    # Longest prefix wins, so "WORK EXPERIENCE Full Stack Intern" is read as
    # "work experience" and not as whatever "work" alone might match.
    for length in range(run, 0, -1):
        heading = _canonical_heading(" ".join(words[:length]))
        if heading:
            return heading
    return None


def collect_signals(text: str) -> PageSignals:
    """Read one page's text and report every résumé/non-résumé signal on it."""
    text = text or ""
    signals = PageSignals(chars=len(text.strip()))
    if signals.chars < _MIN_PAGE_CHARS:
        return signals

    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]

    for line in lines[:12]:
        if _RESUME_TITLE_RE.match(line):
            signals.has_resume_title = True
            break

    seen_headings: set[str] = set()
    for line in lines:
        heading = _heading_on(line) or _inline_heading_on(line)
        if heading and heading not in seen_headings:
            seen_headings.add(heading)
            signals.headings.append(heading)

    signals.has_email = bool(_EMAIL_RE.search(text))
    signals.has_phone = bool(_PHONE_RE.search(text))
    signals.date_ranges = len(_DATE_RANGE_RE.findall(text))
    signals.job_titles = len({m.group(0).lower() for m in _JOB_TITLE_RE.finditer(text)})
    signals.trade_nouns = len({m.group(0).lower() for m in _TRADE_NOUN_RE.finditer(text)})

    for kind, groups in _DOC_MARKERS_COMPILED.items():
        weight = 0.0
        for pattern in groups["strong"]:
            if pattern.search(text):
                weight += 2.0
        for pattern in groups["weak"]:
            if pattern.search(text):
                weight += 0.75
        if weight:
            signals.marker_hits[kind] = weight

    return signals


def _resume_structure_strength(signals: PageSignals) -> int:
    """How much of a résumé's *structure* is on this page.

    Deliberately counts kinds of evidence rather than quantity: a page with six
    section headings and no contact block is less obviously a CV than one with
    three headings, an address and an employment date range. Certificates score
    0–1 here — they carry a name and a date and nothing else a CV is made of.
    """
    strength = 0
    if signals.has_resume_title:
        strength += 2
    strength += min(len(signals.headings), 3)
    if signals.has_email:
        strength += 1
    if signals.has_phone:
        strength += 1
    if signals.date_ranges:
        strength += 1
    if signals.job_titles:
        strength += 1
    return strength


def _marker_penalty(signals: PageSignals) -> float:
    """What the other-document wording costs this page, given what surrounds it.

    A CERTIFICATIONS section quotes the exact wording a certificate uses — "this
    is to certify that", "certificate of completion", a certificate number — and
    at full weight that scored a genuine CV at 0.00 and rejected it in
    production. Naming what you have earned is not the same as being the
    document.

    So the penalty is discounted by how much résumé structure the page has
    around those phrases, and the discount has to be earned: a bare certificate
    has no headings, no contact block and no employment dates, so it keeps the
    full penalty and is still rejected.
    """
    raw = min(sum(signals.marker_hits.values()), _MAX_MARKER_PENALTY)
    if not raw:
        return 0.0

    strength = _resume_structure_strength(signals)
    if strength >= 5:
        factor = 0.25          # unmistakably a CV; the wording is a section of it
    elif strength >= 3:
        factor = 0.5
    else:
        factor = 1.0           # nothing here says résumé — take the marker at its word
    return round(raw * factor, 2)


def score_signals(signals: PageSignals) -> float:
    """Turn signals into a single "how résumé-like is this page" number."""
    if signals.chars < _MIN_PAGE_CHARS:
        return 0.0

    score = 0.0
    if signals.has_resume_title:
        score += 2.0
    if signals.has_email:
        score += 1.0
    if signals.has_phone:
        score += 0.5
    # Headings are the strongest structural evidence, but three of them say
    # about as much as six — cap the contribution so a keyword-stuffed page
    # cannot outrank a real one.
    score += min(len(signals.headings), 4) * 1.0
    score += min(signals.date_ranges, 3) * 0.5
    score += min(signals.job_titles, 3) * 0.5
    score += min(signals.trade_nouns, 3) * 0.25

    # Every certificate carries a name and a date; that is not a résumé.
    score -= _marker_penalty(signals)
    return round(score, 2)


def _document_kind(signals: PageSignals) -> str:
    """Which non-résumé document this page looks like, if any."""
    if not signals.marker_hits:
        return OTHER
    return max(signals.marker_hits.items(), key=lambda kv: kv[1])[0]


def classify_page(text: str, page_number: int = 1) -> PageClassification:
    signals = collect_signals(text)
    score = score_signals(signals)

    if signals.chars < _MIN_PAGE_CHARS:
        kind = BLANK
    elif score >= _RESUME_SEED_SCORE:
        kind = RESUME
    elif score < 0:
        # Only name the page as another kind of document when that evidence
        # actually outweighs the résumé evidence. A CV page that lists a
        # passport number is not a passport, and calling it one is what dropped
        # a candidate's employment history out of the extraction.
        kind = _document_kind(signals)
    else:
        kind = UNKNOWN
    return PageClassification(page_number, kind, score, signals)


# --------------------------------------------------------------------------- #
#  Document level
# --------------------------------------------------------------------------- #
def _confidence_for(is_resume: bool, best_score: float, total_signals: int) -> float:
    """Map evidence onto 0.0–1.0.

    A rejection is capped below 0.30 so that it lands under every ingest gate,
    and an acceptance is capped below 1.0 because a classifier that has only
    read section headings has not yet read the résumé.
    """
    if not is_resume:
        return round(min(0.29, 0.05 + 0.04 * max(total_signals, 0)), 2)
    # 2.5 (the seed threshold) → 0.55; 8.0 and up → 0.95.
    scaled = 0.55 + (min(best_score, 8.0) - _RESUME_SEED_SCORE) * (0.40 / 5.5)
    return round(max(0.35, min(0.95, scaled)), 2)


def classify_document(page_texts: Sequence[str]) -> DocumentClassification:
    """Locate the résumé across every page of a bundle.

    Pages are scored independently, then résumé *regions* are grown outward from
    each high-scoring page: page 2 of a CV has no name, no email and no phone,
    so it only ever passes as the continuation of the page before it.
    """
    texts = list(page_texts or [])
    if not texts:
        return DocumentClassification(False, 0.05, [], [], "document had no pages")

    pages = [classify_page(text, i + 1) for i, text in enumerate(texts)]
    seeds = [p.page_number for p in pages if p.kind == RESUME]

    if seeds:
        selected = _grow_regions(pages, seeds)
        best = max(p.score for p in pages if p.page_number in selected)
        total_signals = sum(
            p.signals.positive_count for p in pages if p.page_number in selected
        )
        reason = (
            f"resume content found on page(s) "
            f"{', '.join(str(n) for n in selected)} of {len(pages)}"
        )
        return DocumentClassification(
            True, _confidence_for(True, best, total_signals), selected, pages, reason,
        )

    # No single page cleared the bar. A one-page résumé whose headings the OCR
    # mangled can still be recognisable when the page is read as a whole, so
    # score the concatenation before giving up — but only that, never the
    # filename.
    whole = "\n".join(texts)
    whole_signals = collect_signals(whole)
    whole_score = score_signals(whole_signals)
    if whole_score >= _RESUME_SEED_SCORE:
        selected = [p.page_number for p in pages if p.signals.chars >= _MIN_PAGE_CHARS]
        return DocumentClassification(
            True,
            _confidence_for(True, whole_score, whole_signals.positive_count),
            selected or [1],
            pages,
            "no single page qualified; whole-document signals did",
        )

    dominant = _document_kind(whole_signals) if whole_signals.marker_hits else OTHER
    reason = (
        f"no resume content on any of {len(pages)} page(s); "
        f"document reads as '{dominant}' (best page score {max(p.score for p in pages):.2f})"
    )
    return DocumentClassification(
        False,
        _confidence_for(False, whole_score, whole_signals.positive_count),
        [],
        pages,
        reason,
    )


def _grow_regions(pages: List[PageClassification], seeds: Sequence[int]) -> List[int]:
    """Extend each seed page outward over its continuation pages."""
    by_number = {p.page_number: p for p in pages}
    selected = set(seeds)

    for seed in seeds:
        for step in (1, -1):
            number = seed + step
            weak_used = 0
            while number in by_number and number not in selected:
                page = by_number[number]
                # A certificate that happens to mention a job title is not the
                # next page of the CV. But markers alone are not enough to stop
                # on: a résumé page listing a passport number trips the ID
                # markers, and breaking there cost a real candidate his
                # employment history. Stop only where the other-document
                # evidence actually outweighs the résumé evidence — which is
                # what a negative-to-low score means.
                if (
                    page.signals.marker_hits
                    and page.kind != RESUME
                    and page.score < _CONTINUATION_SCORE
                ):
                    break
                # A blank page ends a document; it never bridges two.
                if page.signals.chars < _MIN_PAGE_CHARS:
                    break

                if page.score >= _CONTINUATION_SCORE:
                    weak_used = 0
                elif page.score >= 0 and weak_used < _WEAK_CONTINUATION_PAGES:
                    # Unmarked prose behind the résumé: responsibilities,
                    # duties, a spilled-over employment history.
                    weak_used += 1
                else:
                    break

                selected.add(number)
                number += step
    return sorted(selected)


def select_text(page_texts: Sequence[str], pages: Sequence[int]) -> str:
    """Join the selected pages back into one string, in document order."""
    out = []
    for number in sorted(pages):
        index = number - 1
        if 0 <= index < len(page_texts):
            text = (page_texts[index] or "").strip()
            if text:
                out.append(text)
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
#  Multipass: which pages are an Aadhaar, and which are a passport
# --------------------------------------------------------------------------- #
# One 60-page bundle routinely holds a CV, a stack of certificates, an Aadhaar
# card and a passport scan. The résumé half of this module deliberately refuses
# to call a page an ID on the strength of the words "Aadhaar" or "Passport No"
# alone, because every Indian and Gulf CV prints both in its personal-details
# block — and doing so once cost a real candidate his employment history.
#
# Routing a page to the Aadhaar or passport OCR endpoint needs the opposite
# evidence: not "this page mentions the document" but "this page *is* the
# document". So the markers below are the wording that only ever appears on the
# card or the data page itself — the issuing authority, the bilingual field
# labels, the MRZ — and a bare mention is worth a fraction of a hit.

# What a page must score to be sent for ID extraction at all.
_ID_SEED_SCORE = 3.0
# What it must score when it *also* reads as a résumé page. A CV listing a
# passport number, a nationality and a date of expiry can drift over the seed
# score on weak markers alone; only unmistakable document evidence — the MRZ,
# the issuing authority — outranks the résumé reading.
_ID_OVERRIDE_SCORE = 5.0

_AADHAAR_MARKERS = {
    "strong": (
        r"unique\s+identification\s+authority\s+of\s+india",
        r"\buidai\b",
        r"mera\s+aadhaar,?\s*meri\s+pehchan",
        r"aadhaar\s+is\s+(?:a\s+)?proof\s+of\s+identity",
        # The card prints the issuer and the word within a few lines of each
        # other. A CV that merely states an Aadhaar number does not.
        r"government\s+of\s+india[\s\S]{0,160}?\baadha?ar\b",
        r"\baadha?ar\b[\s\S]{0,160}?government\s+of\s+india",
        r"enrol(?:l)?ment\s+(?:no|id)\.?\s*[:\-]?\s*\d",
        r"\bvid\b\s*[:\-]?\s*\d{4}",
        r"आधार",
        r"भारत\s*सरकार",
    ),
    "weak": (
        r"\baadha?ar\b",
        r"\bmy\s+aadhaar\b",
        r"aadhaar\s+(?:no|number)\.?",
        r"download\s+date",
        r"\bc/o\b|\bs/o\b|\bd/o\b|\bw/o\b",
        r"year\s+of\s+birth",
        r"\bpin\s*code\b",
    ),
}

# UIDAI prints the number as three space-separated groups of four. A résumé
# usually runs it together or omits it, so the *formatting* carries real signal.
# We also support masked Aadhaar cards (XXXX XXXX 1234) and hyphen separation.
_AADHAAR_NUMBER_RE = re.compile(r"\b(?:[xX]{4}|\d{4})[\s\-]+(?:[xX]{4}|\d{4})[\s\-]+\d{4}\b")

_PASSPORT_MARKERS = {
    "strong": (
        # The bilingual field labels are printed on the data page and nowhere else.
        r"passport\s*/\s*passeport",
        r"\bpasseport\b",
        r"surname\s*/\s*nom",
        r"given\s+names?\s*\(?s?\)?\s*/\s*pr[eé]noms?",
        r"type\s*/\s*type[\s\S]{0,80}?country\s+code",
        r"country\s+code\s*/\s*code\s+du\s+pays",
        r"republic\s+of\s+india[\s\S]{0,120}?passport",
        r"machine\s+readable\s+zone",
        r"holder.?s\s+signature",
        r"place\s+of\s+issue[\s\S]{0,240}?date\s+of\s+expiry",
        # Back page of Indian Passport
        r"name\s+of\s+father\s*/\s*legal\s+guardian",
        r"name\s+of\s+mother",
        r"name\s+of\s+spouse",
        r"old\s+passport\s+no\.?",
        # Indian Passport (Hindi/English) bilingual labels
        r"पासपोर्ट",
        r"टाईप\s*/\s*type",
        r"राष्ट्र\s+कोड\s*/\s*country\s+code",
        r"उपनाम\s*/\s*surname",
        r"दिया\s+गया\s+नाम",
        r"राष्ट्रीयता\s*/\s*nationality",
        r"जन्म\s+तिथि\s*/\s*date\s+of\s+birth",
        r"समाप्ति\s+की\s+तिथि\s*/\s*date\s+of\s+expiry",
        r"पिता\s*/\s*कानूनी\s*अभिभावक\s*का\s*नाम",
        r"माता\s*का\s*नाम",
        r"पत्नी\s*/\s*पति\s*का\s*नाम",
    ),
    "weak": (
        r"passport\s+no\.?",
        r"date\s+of\s+expiry",
        r"place\s+of\s+issue",
        r"place\s+of\s+birth",
        r"\bnationality\b",
        r"\bsurname\b",
        r"given\s+names?\b",
        r"\bfile\s+no\.?\b",
        r"\bsex\b",
    ),
}

# The MRZ is the single most reliable thing on a passport. However, if the scan
# is blurry or rotated, Tesseract often misreads the '<' character as '(', '{', 
# '[', 'C', 'K', 'E', '«', or '|'. We use a highly forgiving character class and
# avoid strict ^/$ anchors so it detects MRZs even if the OCR result is messy.
_MRZ_LINE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9<({\[\]})«»|l1Ii\.,cKE\-_=+'\"]{28,60}(?![A-Za-z0-9])")
_MRZ_HEAD_RE = re.compile(r"\bP[<K(C\[{\«\|l1I][A-Z0-9]{3}[A-Z0-9<({\[\]})«»|l1Ii\.,cKE\-_=+'\"]{3,}")

_DOCUMENT_MARKERS = {
    "strong": (
        r"national\s+identity\s+card",
        r"identity\s+card",
        r"id\s+card",
        r"\bid\s+no\b",
        r"computerized\s+national",
        r"\bcnic\b",
        r"government\s+of\s+[a-zA-Z]+",
        r"united\s+arab\s+emirates",
        r"emirates\s+id",
        r"kingdom\s+of\s+saudi\s+arabia",
        r"\biqama\b",
        r"resident\s+identity",
        r"resident\s+permit",
        r"state\s+of\s+qatar",
        r"civil\s+id",
        r"ministry\s+of\s+interior",
    ),
    "weak": (
        r"\bid\b",
        r"identity",
        r"identification",
        r"card\s+number",
        r"date\s+of\s+issue",
        r"date\s+of\s+expiry",
        r"\bissue\s+date\b",
        r"\bexpiry\s+date\b",
        r"national\s+no",
        r"date\s+of\s+birth",
        r"\bdob\b",
        r"nationality",
        r"\bgender\b",
        r"\bsex\b",
        r"blood\s+group",
        r"signature",
    ),
}

_ID_MARKERS_COMPILED = {
    AADHAAR: {
        weight: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        for weight, patterns in _AADHAAR_MARKERS.items()
    },
    PASSPORT: {
        weight: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        for weight, patterns in _PASSPORT_MARKERS.items()
    },
    DOCUMENT: {
        weight: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        for weight, patterns in _DOCUMENT_MARKERS.items()
    },
}


def id_document_scores(text: str) -> Dict[str, float]:
    """How strongly one page reads as an Aadhaar card and as a passport.

    Reported per kind rather than as a single verdict: a scanner page can carry
    the back of an Aadhaar *and* a photocopied passport corner, and the caller
    is the one that decides which endpoint wins.
    """
    text = text or ""
    scores: Dict[str, float] = {AADHAAR: 0.0, PASSPORT: 0.0, DOCUMENT: 0.0}
    if len(text.strip()) < _MIN_PAGE_CHARS:
        return scores

    for kind, groups in _ID_MARKERS_COMPILED.items():
        score = 0.0
        for pattern in groups["strong"]:
            if pattern.search(text):
                score += 2.0
        for pattern in groups["weak"]:
            if pattern.search(text):
                score += 0.5
        scores[kind] = score

    # Actively block driving licenses from being classified as generic documents.
    if re.search(r"driving\s+licen[cs]e|license\s+type|licence\s+type", text, re.IGNORECASE):
        scores[DOCUMENT] = 0.0

    if _AADHAAR_NUMBER_RE.search(text):
        scores[AADHAAR] += 2.0

    _CNIC_NUMBER_RE = re.compile(r"\b\d{5}\s*[\-]?\s*\d{7}\s*[\-]?\s*\d\b")
    if _CNIC_NUMBER_RE.search(text):
        scores[DOCUMENT] += 3.0

    # An MRZ head ("P<INDKUMAR<<RAJESH") is conclusive on its own; a bare pair
    # of 40-character runs of capitals and chevrons is nearly so.
    if _MRZ_HEAD_RE.search(text):
        scores[PASSPORT] += 4.0
    elif len(_MRZ_LINE_RE.findall(text)) >= 2:
        scores[PASSPORT] += 3.0
    elif _MRZ_LINE_RE.search(text):
        scores[PASSPORT] += 1.5
        # The back of many modern National ID cards also contains an MRZ line.
        scores[DOCUMENT] += 1.5

    return {k: round(v, 2) for k, v in scores.items()}


@dataclass
class MultipassClassification:
    """Every page of one bundle, routed to the endpoint that can read it.

    ``resume_pages``, ``aadhaar_pages`` and ``passport_pages`` are disjoint and
    1-based. Everything else — certificates, experience letters, invoices, blank
    scanner pages — lands in ``ignored_pages`` and is never uploaded anywhere.
    """

    is_resume: bool
    confidence: float
    resume_pages: List[int]
    aadhaar_pages: List[int]
    passport_pages: List[int]
    document_pages: List[int]
    ignored_pages: List[int]
    pages: List[PageClassification]
    reason: str
    #: Passport pages held back because they were not issued by India. These
    #: *are* passports — they are simply not passports this pipeline may spend a
    #: Veris call on. Kept separate from ``ignored_pages`` so the reason a
    #: recruiter's document never produced a record is answerable.
    foreign_passport_pages: List[int] = field(default_factory=list)
    #: ``{page number: verdict}`` for every page that read as a passport,
    #: whichever way it was routed. This is what an operator inspects when a
    #: passport did not come back.
    passport_nationality: Dict[int, "pn.NationalityVerdict"] = field(default_factory=dict)

    @property
    def page_kinds(self) -> Dict[int, str]:
        return {p.page_number: p.kind for p in self.pages}

    def nationality_report(self) -> List[Dict[str, object]]:
        """Every passport page, its verdict and where it was routed."""
        return [
            {
                "page": number,
                "routed": "veris" if number in self.passport_pages else "skipped",
                **verdict.as_dict(),
            }
            for number, verdict in sorted(self.passport_nationality.items())
        ]

    def modes(self) -> Dict[str, List[int]]:
        """``{ocr mode: pages}`` for the modes this bundle has work for.

        Résumé first: it is the pass that produces the candidate record every
        other pass hangs off.
        """
        found: Dict[str, List[int]] = {}
        if self.resume_pages:
            found[RESUME] = list(self.resume_pages)
        if self.aadhaar_pages:
            found[AADHAAR] = list(self.aadhaar_pages)
        if self.passport_pages:
            found[PASSPORT] = list(self.passport_pages)
        return found


def _log_passport_decision(page: int, send: bool, why: str) -> None:
    """Say, at INFO, what happened to every passport page and why.

    Deliberately not DEBUG: "the passport did not come through" is the support
    question this feature generates, and the answer has to be in the log a
    recruiter's operator can already see.
    """
    if send:
        log.info("Passport page %d routed to the Veris passport endpoint: %s", page, why)
    else:
        log.info("Passport page %d held back: %s", page, why)


def classify_multipass(page_texts: Sequence[str]) -> MultipassClassification:
    """Split one bundle into résumé pages, Aadhaar pages and passport pages.

    The résumé half is delegated to `classify_document` unchanged — that is the
    part tuned against real mail, and the ID pass must not be able to move it.
    Only the pages the résumé pass did *not* claim are offered to the ID
    scorers, so a CV page that lists a passport number stays a CV page.
    """
    texts = list(page_texts or [])
    base = classify_document(texts)

    resume_pages = list(base.resume_pages)
    claimed = set(resume_pages)
    aadhaar_pages: List[int] = []
    passport_pages: List[int] = []
    document_pages: List[int] = []
    ignored_pages: List[int] = []
    foreign_passport_pages: List[int] = []
    passport_verdicts: Dict[int, pn.NationalityVerdict] = {}

    for page in base.pages:
        number = page.page_number
        if number in claimed:
            continue
            
        text = texts[number - 1] if number - 1 < len(texts) else ""
        log.info("Evaluating leftover page %d for ID. Text length: %d chars", number, len(text))
        # log.info("PAGE %d TESSERACT TEXT:\n%s\n---END TEXT---", number, text)
        
        if page.signals.chars < _MIN_PAGE_CHARS:
            log.info("Page %d ignored: page.signals.chars (%d) < _MIN_PAGE_CHARS (%d)", number, page.signals.chars, _MIN_PAGE_CHARS)
            ignored_pages.append(number)
            continue

        scores = id_document_scores(text)
        log.info("Page %d ID scores: %s", number, scores)

        # A page the résumé pass merely failed to reach — an unlabelled
        # continuation, say — still reads as a CV. Only conclusive document
        # evidence takes it.
        base_threshold = _ID_OVERRIDE_SCORE if page.score >= _CONTINUATION_SCORE else _ID_SEED_SCORE
        doc_threshold = 1.5 if (page.score < _CONTINUATION_SCORE) else base_threshold
        
        is_aadhaar = scores[AADHAAR] >= base_threshold
        is_passport = scores[PASSPORT] >= base_threshold
        is_document = False  # Disabled by user request: scores[DOCUMENT] >= doc_threshold
        if not (is_aadhaar or is_passport or is_document):
            log.info("Page %d ignored: all scores below threshold", number)
            ignored_pages.append(number)
            continue
            
        added = []
        if is_aadhaar:
            aadhaar_pages.append(number)
            added.append(AADHAAR)
            
        if is_passport:
            # A passport, but whose? The Veris passport endpoint is trained on the
            # Indian booklet; a Nepali or Philippine one comes back confidently
            # wrong, and a wrong passport number on a visa file is found at the
            # embassy counter. The issuing country is settled here, from the text
            # already in hand, before anything is uploaded.
            verdict = pn.detect_passport_country(text)
            passport_verdicts[number] = verdict
            send, why = pn.should_extract(
                verdict,
                india_only=settings.passport_india_only,
                allow_undetermined=settings.passport_allow_undetermined_nationality,
            )
            if send:
                passport_pages.append(number)
                added.append(PASSPORT)
            else:
                foreign_passport_pages.append(number)
                # If it's a foreign passport, we do NOT want it extracted as a generic document either.
                is_document = False
            _log_passport_decision(number, send, why)

        if is_document:
            document_pages.append(number)
            added.append(DOCUMENT)

        log.info("Page %d successfully classified as %s!", number, " + ".join(added) if added else "ignored foreign passport")
        page.kind = added[0] if added else ID_DOCUMENT

    reason = base.reason
    extra = []
    if aadhaar_pages:
        extra.append(f"aadhaar on page(s) {', '.join(str(n) for n in aadhaar_pages)}")
    if passport_pages:
        extra.append(f"passport on page(s) {', '.join(str(n) for n in passport_pages)}")
    if document_pages:
        extra.append(f"document on page(s) {', '.join(str(n) for n in document_pages)}")
    for number in sorted(foreign_passport_pages):
        held = passport_verdicts.get(number)
        extra.append(
            f"page {number} holds a {held.country if held else 'non-Indian'} passport, "
            f"not sent for extraction"
        )
    if extra:
        reason = f"{reason}; {'; '.join(extra)}"

    return MultipassClassification(
        is_resume=base.is_resume,
        confidence=base.confidence,
        resume_pages=resume_pages,
        aadhaar_pages=sorted(aadhaar_pages),
        passport_pages=sorted(passport_pages),
        document_pages=sorted(document_pages),
        ignored_pages=sorted(ignored_pages),
        pages=base.pages,
        reason=reason,
        foreign_passport_pages=sorted(foreign_passport_pages),
        passport_nationality=passport_verdicts,
    )
