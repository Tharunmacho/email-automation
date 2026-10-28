"""Ranking a job order's candidate pool — locally, instantly, and explainably.

No model and no network call. Every candidate not yet submitted is scored
against the order on seven components, and every number traces back to a line
of the record:

    role            the order's trade in the candidate's titles (1.0), in their
                    skills or history (0.75), a related trade (0.6 / 0.4), or —
                    for a role the trade table does not know — the role's words,
                    weighted by how rare they are in the pool
    skills          each of the order's skills: all its words on record (1.0,
                    synonyms and typos included), half of them or its trade
                    (0.5), nothing (0)
    experience      years against the minimum; a band is a range, a blank is
                    unknown, and years in an unrelated trade do not count
    relevance       BM25 — the search-engine ranking function — of the order's
                    words over the whole record, as a 0-1 coverage
    qualifications  licences and certifications the order names
    destination     the job's country among the candidate's preferences
    documents       passport on file, valid, nothing outstanding

then `scoring.combine` applies the mandatory gate and the category rules.

Why this rather than a language model: it is free, it ranks thousands of
candidates in well under a second, and it can be wrong only in ways a
recruiter can see — a missing synonym is visible as "skill not found", and is
fixed by adding the word to `vocabulary`. Typos are absorbed by matching the
order's words against the words that actually occur in the pool.

**Speed.** The pool is read and indexed once (`pool_index`), kept for
`INDEX_TTL_SECONDS`, and shared by every order; a ranking is kept until the
index or the order changes. Opening an order is one in-memory pass.
"""
from __future__ import annotations

import difflib
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from app.matching import evidence, scoring, vocabulary
from app.matching.scoring import NOT_SATISFIED, SATISFIED, UNKNOWN, Component, Outcome, Verdict
from app.matching.spec import MatchSpec, normalize_job_order, spec_fingerprint

#: How long a read of the pool is reused. New and edited candidates show up
#: within this; "Refresh" on the screen skips the wait.
INDEX_TTL_SECONDS = 60

#: BM25 term saturation and length normalisation. Lower than the textbook
#: 1.2 / 0.75: a CV that names a skill once has evidenced it, and a long CV
#: should not be penalised much for having more to say.
BM25_K1 = 0.6
BM25_B = 0.5

#: How close a word must be to count as a misspelling of another (difflib
#: ratio), and how long it must be before that is attempted at all — below
#: five letters every word is "close" to several others.
TYPO_CUTOFF = 0.85
TYPO_MIN_LENGTH = 5

#: Gulf preferences, and the spellings of the countries recruiters place in.
_GULF = frozenset({"uae", "saudi", "qatar", "kuwait", "oman", "bahrain"})
_COUNTRY = {
    "united arab emirates": "uae", "u a e": "uae", "dubai": "uae", "abu dhabi": "uae", "sharjah": "uae",
    "ksa": "saudi", "saudi arabia": "saudi", "kingdom of saudi arabia": "saudi",
    "uk": "uk", "united kingdom": "uk", "england": "uk", "great britain": "uk",
    "usa": "usa", "us": "usa", "united states": "usa", "united states of america": "usa", "america": "usa",
}
_OPEN_PREFERENCE = frozenset({"any", "anywhere", "any country", "open", "abroad", "overseas", "all"})


def _country(value: Any) -> str:
    text = " ".join(str(value or "").lower().replace(".", " ").split())
    return _COUNTRY.get(text, text)


# --------------------------------------------------------------------------- #
#  The pool, indexed
# --------------------------------------------------------------------------- #
@dataclass
class CandidateDoc:
    """One candidate, read once per index build."""

    id: str
    row: Dict[str, Any]
    title_terms: Set[str]
    body_counts: Counter
    body_len: int
    qual_terms: Set[str]
    title_trades: FrozenSet[str]
    body_trades: FrozenSet[str]
    has_titles: bool
    sparse: bool
    experience: Optional[Tuple[float, float, str]]
    destinations: List[str]

    @property
    def body_terms(self) -> Set[str]:
        return set(self.body_counts)


def _candidate_text(row: Dict[str, Any]) -> Tuple[str, str, str, bool]:
    """(titles, everything, qualifications, has any skills/history/summary)."""
    profile = evidence.profile_of(row)
    titles = [str(profile.get(key) or "") for key in (
        "current_designation", "job_title", "job_preference", "job_category", "course_or_trade")]
    roles = evidence.roles(profile)
    titles += [role.get("designation", "") for role in roles]
    skills = (evidence.strings(profile.get("skills")) + evidence.strings(profile.get("technical_skills"))
              + evidence.strings(profile.get("trade_skills")))
    quals = (evidence.strings(profile.get("certifications"))
             + [" ".join(licence.get(key, "") for key in ("name", "issuer")) for licence in evidence.licences(profile)]
             + evidence.education(profile))
    summary = str(profile.get("resume_summary") or "")
    descriptions = [role.get("description", "") for role in roles]
    title_text = " . ".join(t for t in titles if t)
    body = " . ".join(part for part in [title_text, *skills, *quals, summary, *descriptions] if part)
    return title_text, body, " . ".join(quals), bool(skills or roles or summary.strip())


def _doc(row: Dict[str, Any]) -> CandidateDoc:
    title_text, body_text, qual_text, has_content = _candidate_text(row)
    title_norm = vocabulary.normalise(title_text)
    body_norm = vocabulary.normalise(body_text)
    body_terms = vocabulary.terms_of_normalised(body_norm)
    profile = evidence.profile_of(row)
    destinations = [_country(value) for value in
                    [profile.get("destination_country"), *evidence.strings(profile.get("destination_countries"))]
                    if str(value or "").strip()]
    return CandidateDoc(
        id=str(row.get("id")),
        row=row,
        title_terms=set(vocabulary.terms_of_normalised(title_norm)),
        body_counts=Counter(body_terms),
        body_len=len(body_terms),
        qual_terms=set(vocabulary.terms(qual_text)),
        title_trades=vocabulary.trades_in_normalised(title_norm),
        body_trades=vocabulary.trades_in_normalised(body_norm),
        has_titles=bool(title_text.strip()),
        sparse=not has_content,
        experience=evidence.experience_range(row),
        destinations=destinations,
    )


class PoolIndex:
    """Every candidate in the pool, tokenised once, with document frequencies."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self.built_at = time.monotonic()
        self.docs = [_doc(row) for row in rows if row.get("id")]
        self.by_id = {doc.id: doc for doc in self.docs}
        self.df: Counter = Counter()
        for doc in self.docs:
            self.df.update(doc.body_counts.keys())
        self.n = len(self.docs)
        self.avg_len = (sum(doc.body_len for doc in self.docs) / self.n) if self.n else 1.0
        self._vocab = sorted(self.df)
        self._variants: Dict[str, FrozenSet[str]] = {}
        self._lock = threading.Lock()

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def variants(self, term: str) -> FrozenSet[str]:
        """The term, and the words in the pool close enough to be a misspelling of it."""
        found = self._variants.get(term)
        if found is not None:
            return found
        out = {term}
        if len(term) >= TYPO_MIN_LENGTH and term.isalpha():
            out.update(difflib.get_close_matches(term, self._vocab, n=4, cutoff=TYPO_CUTOFF))
        found = frozenset(out)
        with self._lock:
            self._variants[term] = found
        return found

    def known(self, term: str) -> bool:
        return any(self.df.get(variant) for variant in self.variants(term))


_index: Optional[PoolIndex] = None
_index_lock = threading.Lock()


def pool_index(*, fresh: bool = False) -> PoolIndex:
    """The current pool index, rebuilt when it is older than `INDEX_TTL_SECONDS`."""
    global _index
    from app.api import routes  # late: routes includes this package's router
    from app.matching import pool

    with _index_lock:
        if fresh or _index is None or time.monotonic() - _index.built_at > INDEX_TTL_SECONDS:
            _index = PoolIndex(pool.retrieve(routes.repo()))
            _rankings.clear()
        return _index


def reset() -> None:
    """Forget the cached pool and rankings (tests, and "Refresh")."""
    global _index
    with _index_lock:
        _index = None
        _rankings.clear()


# --------------------------------------------------------------------------- #
#  The order, as a query
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Skill:
    label: str
    terms: Tuple[str, ...]
    trades: FrozenSet[str]


@dataclass(frozen=True)
class Query:
    spec: MatchSpec
    role_terms: Tuple[str, ...]
    role_trades: FrozenSet[str]
    related_trades: FrozenSet[str]
    skills: Tuple[Skill, ...]
    weighted_terms: Dict[str, float]


def _unique(items: List[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def build_query(spec: MatchSpec) -> Query:
    role_text = " ".join(term for term in (spec.designation, spec.title) if term)
    role_trades = vocabulary.trades_in(role_text)
    skills = tuple(
        Skill(label=skill, terms=_unique(vocabulary.terms(skill)), trades=vocabulary.trades_in(skill))
        for skill in spec.required_skills if vocabulary.terms(skill)
    )
    weighted: Dict[str, float] = {}
    for term in vocabulary.terms(role_text):
        weighted[term] = 1.0
    for skill in skills:
        for term in skill.terms:
            weighted[term] = 1.0
    for text, weight in ((spec.industry, 0.5), (spec.description, 0.5)):
        for term in _unique(vocabulary.terms(text))[:30]:
            weighted.setdefault(term, weight)
    return Query(
        spec=spec,
        role_terms=_unique(vocabulary.terms(role_text)),
        role_trades=role_trades,
        related_trades=vocabulary.related_to(role_trades),
        skills=skills,
        weighted_terms=weighted,
    )


# --------------------------------------------------------------------------- #
#  Components
# --------------------------------------------------------------------------- #
def _has(index: PoolIndex, terms: Set[str], term: str) -> bool:
    return bool(index.variants(term) & terms)


def _idf_share(index: PoolIndex, wanted: Tuple[str, ...], present: Set[str]) -> Tuple[float, List[str]]:
    """How much of `wanted` is in `present`, weighted by rarity. Words nobody has are left out."""
    total = matched = 0.0
    hits: List[str] = []
    for term in wanted:
        if not index.known(term):
            continue
        weight = index.idf(term)
        total += weight
        if _has(index, present, term):
            matched += weight
            hits.append(term)
    return (matched / total if total else 0.0), hits


def _role(query: Query, doc: CandidateDoc, index: PoolIndex) -> Tuple[float, bool, str]:
    if not doc.has_titles and not doc.body_counts:
        return 0.0, True, "No job title or work history on record"
    options: List[Tuple[float, str]] = [(0.0, "Nothing on record matches the role")]
    wanted = query.role_trades
    if wanted & doc.title_trades:
        options.append((1.0, f"Job title is {vocabulary.labels(wanted & doc.title_trades)}"))
    elif wanted & doc.body_trades:
        options.append((0.75, f"{vocabulary.labels(wanted & doc.body_trades)} work appears in skills or history"))
    if query.related_trades & doc.title_trades:
        options.append((0.6, f"Related trade: {vocabulary.labels(query.related_trades & doc.title_trades)}"))
    elif query.related_trades & doc.body_trades:
        options.append((0.4, f"Related trade in skills or history: "
                             f"{vocabulary.labels(query.related_trades & doc.body_trades)}"))
    share, hits = _idf_share(index, query.role_terms, doc.title_terms)
    if share:
        options.append((0.9 * share, f"Job title shares: {', '.join(hits)}"))
    share, hits = _idf_share(index, query.role_terms, doc.body_terms)
    if share:
        options.append((0.5 * share, f"Role words in the record: {', '.join(hits)}"))
    value, detail = max(options, key=lambda option: option[0])
    return value, False, detail


def _skills(query: Query, doc: CandidateDoc, index: PoolIndex
            ) -> Tuple[Optional[float], bool, str, List[str], List[str], List[str]]:
    if not query.skills:
        return None, False, "The order lists no skills", [], [], []
    full: List[str] = []
    partial: List[str] = []
    missing: List[str] = []
    body = doc.body_terms
    for skill in query.skills:
        hits = [term for term in skill.terms if _has(index, body, term)]
        if len(hits) == len(skill.terms):
            full.append(skill.label)
        elif (skill.trades & doc.body_trades) or len(hits) * 2 >= len(skill.terms) > 1:
            partial.append(skill.label)
        else:
            missing.append(skill.label)
    value = (len(full) + 0.5 * len(partial)) / len(query.skills)
    if doc.sparse:
        return value, True, "No skills or work history on record", full, partial, missing
    detail = f"{len(full)} of {len(query.skills)} skills on record"
    if partial:
        detail += f"; related: {', '.join(partial)}"
    return value, False, detail, full, partial, missing


def _experience(spec: MatchSpec, doc: CandidateDoc, relevant: bool
                ) -> Tuple[Optional[float], bool, str, Optional[Verdict]]:
    floor = spec.min_experience_years
    if floor is None:
        return None, False, "The order sets no minimum", None
    requirement = next(item for item in spec.mandatory_requirements if item.key == "min-experience")

    def verdict(status: str, detail: str) -> Verdict:
        return Verdict(requirement.key, requirement.label, status, requirement.source, detail)

    if doc.experience is None:
        return 0.0, True, "No experience figure on record", verdict(UNKNOWN, "Not on record")
    low, high, stated = doc.experience
    if low >= floor:
        value, status, detail = 1.0, SATISFIED, f"{stated} — meets {floor:g}"
    elif high < floor:
        value, status, detail = 0.7 * high / floor, NOT_SATISFIED, f"{stated} — below {floor:g}"
    else:
        value, status, detail = 0.5, UNKNOWN, f"{stated} — may or may not reach {floor:g}"
    if not relevant:
        value, detail = 0.0, f"{detail}; not counted: the role does not match"
    return value, False, detail, verdict(status, detail)


def _relevance(query: Query, doc: CandidateDoc, index: PoolIndex) -> Tuple[float, str]:
    """BM25 over the whole record, as the share of the order's (weighted) words it covers."""
    total = covered = 0.0
    norm = 1.0 - BM25_B + BM25_B * (doc.body_len / index.avg_len if index.avg_len else 1.0)
    for term, weight in query.weighted_terms.items():
        if not index.known(term):
            continue
        idf = index.idf(term) * weight
        total += idf
        tf = sum(doc.body_counts.get(variant, 0) for variant in index.variants(term))
        if tf:
            covered += idf * (tf * (BM25_K1 + 1) / (tf + BM25_K1 * norm)) / (BM25_K1 + 1)
    value = covered / total if total else 0.0
    return value, f"The record covers {round(value * 100)}% of the order's vocabulary"


def _found(index: PoolIndex, terms: Set[str], text: str) -> bool:
    wanted = vocabulary.terms(text)
    return bool(wanted) and all(_has(index, terms, term) for term in wanted)


def _qualifications(spec: MatchSpec, doc: CandidateDoc, index: PoolIndex
                    ) -> Tuple[Optional[float], str, List[Verdict]]:
    wanted = [(item, item.value) for item in spec.mandatory_requirements
              if item.key.startswith(("licence:", "certification:"))]
    if not wanted:
        return None, "The order names no licence or certification", []
    verdicts: List[Verdict] = []
    today = date.today()
    profile = evidence.profile_of(doc.row)
    for requirement, name in wanted:
        if _found(index, doc.qual_terms | doc.body_terms, name):
            expired = None
            for licence in evidence.licences(profile):
                expiry = evidence._parse_date(licence.get("expiry_date"))
                if expiry and expiry < today and _found(index, set(vocabulary.terms(licence.get("name", ""))), name):
                    expired = expiry
            if expired:
                verdicts.append(Verdict(requirement.key, requirement.label, NOT_SATISFIED, requirement.source,
                                        f"On record but expired {expired.isoformat()}"))
            else:
                verdicts.append(Verdict(requirement.key, requirement.label, SATISFIED, requirement.source,
                                        "On record"))
        else:
            verdicts.append(Verdict(requirement.key, requirement.label, UNKNOWN, requirement.source,
                                    "Not on record"))
    held = sum(1 for verdict in verdicts if verdict.status == SATISFIED)
    return held / len(verdicts), f"{held} of {len(verdicts)} on record", verdicts


def _other_mandatory(spec: MatchSpec, doc: CandidateDoc, index: PoolIndex) -> List[Verdict]:
    verdicts: List[Verdict] = []
    for item in spec.mandatory_requirements:
        if item.key.startswith("mandatory-skill:"):
            ok = _found(index, doc.body_terms, item.value)
            verdicts.append(Verdict(item.key, item.label, SATISFIED if ok else UNKNOWN, item.source,
                                    "On record" if ok else "Not on record"))
        elif item.key == "work-eligibility":
            ok = _found(index, doc.body_terms, item.value)
            verdicts.append(Verdict(item.key, item.label, SATISFIED if ok else UNKNOWN, item.source,
                                    "On record" if ok else "Not on record"))
        elif item.key == "age-band":
            verdicts.append(Verdict(item.key, item.label, UNKNOWN, item.source,
                                    "No date of birth on record"))
    return verdicts


def _destination(spec: MatchSpec, doc: CandidateDoc) -> Tuple[Optional[float], bool, str]:
    if not spec.destination_country:
        return None, False, "The order states no destination"
    target = _country(spec.destination_country)
    if not doc.destinations:
        return 0.0, True, "No preferred destination on record"
    if target in doc.destinations or (target in _GULF and "gulf" in doc.destinations):
        return 1.0, False, f"Wants to work in {spec.destination_country}"
    if any(place in _OPEN_PREFERENCE for place in doc.destinations):
        return 0.8, False, "Open to any destination"
    return 0.2, False, f"Prefers {', '.join(doc.destinations)}"


def _documents(doc: CandidateDoc) -> Tuple[float, str, str]:
    """(value, band, label)."""
    info = evidence.documents(doc.row)
    today = date.today()
    if not info["passport_on_file"]:
        value, label = 0.0, "No passport on file"
    elif info["passport_expiry"] is None:
        value, label = 0.6, "Passport on file, expiry not recorded"
    elif info["passport_expiry"] < today:
        value, label = 0.0, f"Passport expired {info['passport_expiry'].isoformat()}"
    elif info["passport_expiry"] < today + timedelta(days=180):
        value, label = 0.3, f"Passport expires {info['passport_expiry'].isoformat()}"
    else:
        value, label = 0.7, f"Passport valid to {info['passport_expiry'].isoformat()}"
    if info["outstanding_documents"]:
        label += f"; outstanding: {', '.join(info['outstanding_documents'])}"
    elif info["registration_tracked"] and value > 0:
        value += 0.3
        label += "; nothing outstanding"
    band = "ready" if value >= 0.75 else "partial" if value >= 0.4 else "not_ready"
    return min(1.0, value), band, label


# --------------------------------------------------------------------------- #
#  One candidate, one order
# --------------------------------------------------------------------------- #
@dataclass
class Match:
    candidate_id: str
    outcome: Outcome
    matched_skills: List[str]
    related_skills: List[str]
    missing_skills: List[str]
    documents: Dict[str, Any]
    row: Dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def score(self) -> float:
        return self.outcome.score

    @property
    def category(self) -> str:
        return self.outcome.category


def match_one(query: Query, doc: CandidateDoc, index: PoolIndex, config: scoring.ScoringConfig) -> Match:
    spec = query.spec
    weights = config.weights
    role, role_unknown, role_detail = _role(query, doc, index)
    skills, skills_unknown, skills_detail, full, partial, missing = _skills(query, doc, index)
    relevant = role >= 0.4 or (skills is not None and skills >= 0.5)
    experience, exp_unknown, exp_detail, exp_verdict = _experience(spec, doc, relevant)
    relevance, relevance_detail = _relevance(query, doc, index)
    quals, quals_detail, qual_verdicts = _qualifications(spec, doc, index)
    destination, dest_unknown, dest_detail = _destination(spec, doc)
    documents, band, documents_label = _documents(doc)

    components = [
        Component("role", weights["role"], role, role_unknown, role_detail),
        Component("skills", weights["skills"], skills, skills_unknown, skills_detail),
        Component("experience", weights["experience"], experience, exp_unknown, exp_detail),
        Component("relevance", weights["relevance"], relevance, False, relevance_detail),
        Component("qualifications", weights["qualifications"], quals, False, quals_detail),
        Component("destination", weights["destination"], destination, dest_unknown, dest_detail),
        Component("documents", weights["documents"], documents, False, documents_label),
    ]
    verdicts = ([exp_verdict] if exp_verdict else []) + qual_verdicts + _other_mandatory(spec, doc, index)
    reasons = [role_detail]
    if skills is not None:
        reasons.append(skills_detail)
    if experience is not None:
        reasons.append(f"Experience: {exp_detail}")
    outcome = scoring.combine(components, verdicts, role=role, sparse=doc.sparse, config=config, reasons=reasons)
    return Match(
        candidate_id=doc.id, outcome=outcome, matched_skills=full, related_skills=partial,
        missing_skills=missing, documents={"band": band, "value": round(documents, 4), "label": documents_label},
        row=doc.row,
    )


def supporting_evidence(query: Query, match: Match) -> List[Dict[str, str]]:
    """Lines of the record behind the role, each skill found, and each qualification held.

    Computed for one candidate at a time — the explanation — not for the list.
    """
    out: List[Dict[str, str]] = []
    spec = query.spec
    role = spec.designation or spec.title
    if role:
        terms = vocabulary.raw_phrases_for(role) + [name for key in query.role_trades | query.related_trades
                                                    for name in vocabulary.TRADES[key].names]
        found = evidence.excerpt_for(match.row, terms)
        if found:
            out.append({"requirement": role, **found})
    for skill in match.matched_skills + match.related_skills:
        found = evidence.excerpt_for(match.row, vocabulary.raw_phrases_for(skill))
        if found:
            out.append({"requirement": skill, **found})
    for verdict in match.outcome.verdicts:
        if verdict.status == SATISFIED and verdict.key.startswith(("licence:", "certification:")):
            value = verdict.label.split(":", 1)[-1].strip()
            found = evidence.excerpt_for(match.row, vocabulary.raw_phrases_for(value))
            if found:
                out.append({"requirement": verdict.label, **found})
    return out[:12]


# --------------------------------------------------------------------------- #
#  The order's whole pool
# --------------------------------------------------------------------------- #
@dataclass
class Ranking:
    spec: MatchSpec
    config: scoring.ScoringConfig
    query: Query
    matches: List[Match]
    by_id: Dict[str, Match]
    pool: int


_rankings: Dict[Tuple[str, str, str, float], Ranking] = {}
_MAX_RANKINGS = 64


def rank(order: Dict[str, Any], *, fresh: bool = False) -> Ranking:
    """Every candidate in the pool, scored against `order`, best first."""
    index = pool_index(fresh=fresh)
    spec = normalize_job_order(order)
    config = scoring.config_for(order)
    key = (spec.order_id, spec_fingerprint(spec), config.version, index.built_at)
    cached = _rankings.get(key)
    if cached is not None:
        return cached
    query = build_query(spec)
    matches = [match_one(query, doc, index, config) for doc in index.docs]
    matches.sort(key=lambda m: (scoring.rank_key(m.category, m.score), m.candidate_id))
    ranking = Ranking(spec=spec, config=config, query=query, matches=matches,
                      by_id={m.candidate_id: m for m in matches}, pool=index.n)
    with _index_lock:
        if len(_rankings) >= _MAX_RANKINGS:
            _rankings.clear()
        _rankings[key] = ranking
    return ranking
