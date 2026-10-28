"""A free check before Jev: who cannot match, decided without paying for a call.

Two rules, each written so that it only skips somebody Jev could not have put
on the shortlist either:

    below_experience   the order sets a minimum, the record states fewer
                       years — as a figure, or as a band whose top is below
                       it — and the dated work history does not show more.
                       That is a verified fail of a mandatory requirement:
                       the same `ineligible` Jev would return, bought for
                       nothing.
    different_trade    the record names a trade, it is neither the order's
                       trade nor a related one, and nothing anywhere in the
                       record — titles, skills, summary, work history —
                       mentions the order's trade, a related trade, a word of
                       the role or one of its skills.

Everybody else goes to Jev. In particular a thin record with no recognisable
title, a generic one ("helper"), an order whose trade is not in the table, and
anyone with any overlap at all are always sent. The failure this feature was
built to avoid — a fabricator whose CV never says "welder" being ranked out —
cannot happen here: fabricator is a *related* trade and is read by Jev like
anyone else.

Skipped candidates are not hidden: they are stored with the reason, sit on the
shortlist as low match or ineligible, are counted on screen, and "Check with
Jev" sends them anyway.

The trade table is plain data. A trade recruiters place that is missing from it
simply never screens anybody out — the safe direction to be wrong in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from app.matching import scoring
from app.matching.evidence import experience_years
from app.matching.prefilter import Baseline, _evidence_text
from app.matching.spec import MatchSpec

#: Part of the stored result, so a screened candidate can be traced to the
#: rules that skipped them.
SCREEN_VERSION = "screen-1"

BELOW_EXPERIENCE = "below_experience"
DIFFERENT_TRADE = "different_trade"


@dataclass(frozen=True)
class Trade:
    label: str
    #: How a title or a CV says this trade. Matched as whole words after the
    #: light stemming below, so "welders" and "welding" both find "welder".
    names: Tuple[str, ...]
    #: Trades close enough that only Jev should decide. Symmetric: listing B
    #: under A is enough.
    related: Tuple[str, ...] = ()


TRADES: Dict[str, Trade] = {
    # Metal work
    "welder": Trade("Welder", (
        "welder", "welding", "tig", "mig", "gtaw", "smaw", "gmaw", "fcaw", "arc welding",
        "argon", "argon arc", "stick welding", "6g", "6g welder", "3g welder", "co2 welding",
    ), ("fabricator", "pipe_fitter", "fitter", "qaqc")),
    "fabricator": Trade("Fabricator", (
        "fabricator", "fabrication", "steel fabricator", "structural fabricator", "sheet metal",
        "plater", "boilermaker", "boiler maker", "aluminium fabricator", "aluminum fabricator",
    ), ("fitter", "pipe_fitter")),
    "pipe_fitter": Trade("Pipe fitter", (
        "pipe fitter", "pipefitter", "pipe fitting", "piping", "piping fitter",
    ), ("plumber", "fitter")),
    "fitter": Trade("Fitter", (
        "fitter", "mechanical fitter", "steel fitter", "structural fitter", "millwright",
        "machine fitter",
    ), ("mechanic", "machinist")),
    "machinist": Trade("Machinist", (
        "machinist", "cnc", "cnc operator", "cnc machinist", "lathe", "lathe operator",
        "turner", "milling", "vmc operator",
    )),
    # Electrical
    "electrician": Trade("Electrician", (
        "electrician", "electrical technician", "electrical fitter", "wireman", "electrical",
        "industrial electrician", "panel wiring", "cable jointer", "cable pulling",
        "electrical maintenance", "auto electrician",
    ), ("instrument_tech", "electrical_engineer", "hvac", "mechanic")),
    "instrument_tech": Trade("Instrument technician", (
        "instrument technician", "instrumentation", "instrument fitter", "calibration technician",
    ), ("electrical_engineer",)),
    "electrical_engineer": Trade("Electrical engineer", (
        "electrical engineer", "electrical supervisor", "electrical foreman",
    )),
    # Mechanical and building services
    "mechanic": Trade("Mechanic", (
        "mechanic", "auto mechanic", "diesel mechanic", "motor mechanic",
        "heavy equipment mechanic", "automobile technician", "vehicle technician",
    ), ("equipment_operator", "mechanical_engineer")),
    "hvac": Trade("HVAC technician", (
        "hvac", "hvac technician", "ac technician", "ac mechanic", "air conditioning",
        "refrigeration", "chiller", "duct fabricator", "ductman",
    ), ("plumber", "mechanical_engineer")),
    "plumber": Trade("Plumber", ("plumber", "plumbing", "sanitary", "drainage")),
    "mechanical_engineer": Trade("Mechanical engineer", (
        "mechanical engineer", "mechanical supervisor",
    ), ("fitter",)),
    # Civil and finishing
    "mason": Trade("Mason", (
        "mason", "masonry", "bricklayer", "brick layer", "block mason", "blockwork",
        "block work", "stone mason", "plasterer", "plastering",
    ), ("tiler", "steel_fixer")),
    "tiler": Trade("Tiler", (
        "tiler", "tiling", "tile mason", "tile fixer", "marble fixer", "granite fixer",
    )),
    "carpenter": Trade("Carpenter", (
        "carpenter", "carpentry", "joiner", "joinery", "shuttering", "shuttering carpenter",
        "formwork", "form work",
    ), ("gypsum", "steel_fixer")),
    "gypsum": Trade("Gypsum / ceiling fixer", (
        "gypsum", "false ceiling", "ceiling fixer", "drywall", "dry wall", "partition fixer",
    ), ("painter",)),
    "painter": Trade("Painter", (
        "painter", "painting", "spray painter", "industrial painter", "blaster",
        "sandblaster", "sand blaster", "blasting", "coating applicator",
    ), ("insulator",)),
    "steel_fixer": Trade("Steel fixer", ("steel fixer", "rebar", "bar bender", "steel binder")),
    "scaffolder": Trade("Scaffolder", ("scaffolder", "scaffolding"), ("rigger",)),
    "rigger": Trade("Rigger", ("rigger", "rigging", "signalman", "banksman"), ("crane_operator",)),
    "insulator": Trade("Insulator", ("insulator", "insulation", "lagging")),
    "civil_engineer": Trade("Civil engineer / site supervisor", (
        "civil engineer", "site engineer", "civil supervisor", "site supervisor",
        "construction supervisor", "quantity surveyor",
    ), ("surveyor",)),
    "surveyor": Trade("Surveyor", ("surveyor", "land surveyor", "total station")),
    # Operators and drivers
    "crane_operator": Trade("Crane operator", (
        "crane operator", "mobile crane", "tower crane", "crawler crane", "overhead crane",
    ), ("equipment_operator",)),
    "equipment_operator": Trade("Heavy equipment operator", (
        "equipment operator", "heavy equipment operator", "excavator", "excavator operator",
        "jcb", "backhoe", "wheel loader", "loader operator", "bulldozer", "dozer", "grader",
        "roller operator", "poclain",
    ), ("forklift_operator", "driver")),
    "forklift_operator": Trade("Forklift operator", (
        "forklift", "fork lift", "forklift operator", "reach truck",
    ), ("driver", "storekeeper")),
    "driver": Trade("Driver", (
        "driver", "heavy driver", "light driver", "lmv", "hmv", "trailer driver", "truck driver",
        "bus driver", "car driver", "delivery driver", "chauffeur", "tipper driver",
        "heavy vehicle driver", "light vehicle driver",
    )),
    # Safety and quality
    "safety_officer": Trade("Safety officer", (
        "safety officer", "hse officer", "hse", "safety supervisor", "safety steward",
        "nebosh", "iosh", "fire watcher", "fire watchman",
    )),
    "qaqc": Trade("QA/QC inspector", (
        "qa qc", "qaqc", "qc inspector", "quality inspector", "quality control",
        "welding inspector", "ndt", "ndt technician", "cswip",
    )),
    # Hospitality, care and services
    "cook": Trade("Cook / chef", (
        "cook", "chef", "commis", "commis chef", "tandoor", "tandoori", "cooking", "kitchen",
        "baker", "bakery", "pastry",
    ), ("waiter",)),
    "waiter": Trade("Waiter", (
        "waiter", "waitress", "barista", "food service", "restaurant service",
    ), ("housekeeping",)),
    "housekeeping": Trade("Housekeeping / cleaner", (
        "housekeeping", "housekeeper", "room attendant", "cleaner", "cleaning", "janitor",
        "laundry", "housemaid", "maid", "domestic worker",
    ), ("caregiver",)),
    "security_guard": Trade("Security guard", (
        "security guard", "security officer", "watchman", "security supervisor",
    )),
    "nurse": Trade("Nurse", (
        "nurse", "nursing", "staff nurse", "registered nurse", "gnm", "anm", "icu nurse",
    ), ("caregiver",)),
    "caregiver": Trade("Caregiver", (
        "caregiver", "care giver", "caretaker", "patient care", "home nurse", "nanny",
    )),
    "tailor": Trade("Tailor", ("tailor", "sewing", "sewing machine operator", "stitching")),
    "beautician": Trade("Beautician / barber", ("beautician", "hairdresser", "barber", "salon")),
    # Office
    "accountant": Trade("Accountant", (
        "accountant", "accounting", "accounts", "bookkeeper", "bookkeeping", "tally",
        "finance executive", "auditor", "accounts assistant",
    ), ("admin",)),
    "sales": Trade("Sales", (
        "sales", "salesman", "sales executive", "sales representative", "business development",
        "marketing executive", "sales associate",
    ), ("admin",)),
    "storekeeper": Trade("Storekeeper", (
        "storekeeper", "store keeper", "warehouse", "warehouse assistant", "inventory",
        "stores assistant", "store assistant", "logistics assistant",
    ), ("admin",)),
    "admin": Trade("Office / admin", (
        "office assistant", "admin assistant", "administrative assistant", "data entry",
        "clerk", "receptionist", "front office", "office admin",
    )),
    "software": Trade("Software / IT", (
        "software engineer", "software developer", "developer", "programmer", "web developer",
        "full stack", "frontend developer", "backend developer", "it support",
        "system administrator",
    )),
    # Generic: says nothing about which trade, so never grounds a skip.
    "helper": Trade("Helper / general worker", (
        "helper", "labour", "labourer", "laborer", "general worker", "construction worker",
        "unskilled worker", "general labour",
    )),
}

#: Trades that do not say which trade somebody is in.
GENERIC: FrozenSet[str] = frozenset({"helper"})


# --------------------------------------------------------------------------- #
#  Text
# --------------------------------------------------------------------------- #
_WORD = re.compile(r"[a-z0-9+#]+")


@lru_cache(maxsize=100_000)
def stem(token: str) -> str:
    """A deliberately light stemmer: plurals, then -ing / -er.

    Enough that "welders", "welding" and "welder" meet, and simple enough to
    reason about. It is applied identically to the trade table and to the
    record, so a crude stem still matches itself.
    """
    if len(token) <= 3 or not token.isalpha():
        return token
    if token.endswith("ies") and len(token) > 4:
        token = token[:-3] + "y"
    elif token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    if token.endswith("ing") and len(token) >= 6:
        token = token[:-3]
    elif token.endswith("er") and len(token) >= 5:
        token = token[:-2]
    return token


def normalise(text: str) -> str:
    """Lower-cased, punctuation-free, stemmed words joined by single spaces."""
    return " ".join(stem(word) for word in _WORD.findall((text or "").lower()))


def _build_matcher() -> Tuple[re.Pattern, Dict[str, str]]:
    owner: Dict[str, str] = {}
    for key, trade in TRADES.items():
        for name in trade.names:
            owner.setdefault(normalise(name), key)
    phrases = sorted(owner, key=len, reverse=True)
    pattern = re.compile(r"(?<![a-z0-9+#])(?:" + "|".join(re.escape(p) for p in phrases) + r")(?![a-z0-9+#])")
    return pattern, owner


_PATTERN, _OWNER = _build_matcher()


def _related_map() -> Dict[str, FrozenSet[str]]:
    out: Dict[str, set] = {key: set() for key in TRADES}
    for key, trade in TRADES.items():
        for other in trade.related:
            out[key].add(other)
            out[other].add(key)
    return {key: frozenset(value) for key, value in out.items()}


RELATED = _related_map()


def trades_in(text: str) -> FrozenSet[str]:
    """Every trade named anywhere in `text`."""
    return frozenset(_OWNER[match.group(0)] for match in _PATTERN.finditer(normalise(text)))


# --------------------------------------------------------------------------- #
#  Experience
# --------------------------------------------------------------------------- #
_YEAR = re.compile(r"\b(19[5-9]\d|20\d\d)\b")
_ONGOING = re.compile(r"present|current|till date|to date|now|ongoing", re.I)
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:years?|yrs?)", re.I)


def band_ceiling(band: Any) -> Optional[float]:
    """The most experience a band like "2-5 years" or "3_5" allows; None if open or unreadable."""
    text = str(band or "").strip().lower()
    if not text:
        return None
    if "fresher" in text or text in {"0", "none", "no experience"}:
        return 0.0
    if "+" in text or "above" in text or "more" in text or "over" in text:
        return None
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    return max(numbers) if numbers else None


def history_years(row: Dict[str, Any]) -> float:
    """The most experience the dated work history could support.

    The wider of: first start year to last end year, and the stated
    durations added up. Generous on purpose — it only ever stops a skip.
    """
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    starts: List[int] = []
    ends: List[int] = []
    durations = 0.0
    this_year = date.today().year
    for role in profile.get("work_experience") or []:
        if not isinstance(role, dict):
            continue
        start = " ".join(str(role.get(key) or "") for key in ("start_date", "from"))
        end = " ".join(str(role.get(key) or "") for key in ("end_date", "to"))
        span = " ".join(str(role.get(key) or "") for key in ("duration", "period"))
        start_years = [int(y) for y in _YEAR.findall(start)] or [int(y) for y in _YEAR.findall(span)][:1]
        end_years = [int(y) for y in _YEAR.findall(end)] or [int(y) for y in _YEAR.findall(span)][1:]
        if _ONGOING.search(end) or _ONGOING.search(span):
            end_years.append(this_year)
        starts.extend(start_years)
        ends.extend(end_years or start_years)
        match = _DURATION.search(span)
        if match:
            durations += float(match.group(1))
    by_dates = float(max(ends) - min(starts)) if starts and ends else 0.0
    return max(by_dates, durations)


# --------------------------------------------------------------------------- #
#  The check
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Screened:
    """Why a candidate was not sent to Jev."""

    rule: str
    reason: str
    category: str
    failed_requirement: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "reason": self.reason, "version": SCREEN_VERSION}


def _titles(row: Dict[str, Any]) -> str:
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    parts = [str(profile.get(key) or "") for key in (
        "current_designation", "job_title", "job_preference", "job_category", "course_or_trade")]
    for role in profile.get("work_experience") or []:
        if isinstance(role, dict):
            parts.extend(str(role.get(key) or "") for key in ("designation", "title"))
    return " ".join(part for part in parts if part)


def order_trades(spec: MatchSpec) -> FrozenSet[str]:
    return trades_in(" ".join(term for term in (spec.designation, spec.title) if term))


def _below_experience(spec: MatchSpec, row: Dict[str, Any]) -> Optional[Screened]:
    floor = spec.min_experience_years
    if floor is None:
        return None
    years = experience_years(row)
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    if years is not None:
        stated, ceiling = f"{years:g} years on record", years
    else:
        ceiling = band_ceiling(profile.get("total_experience_band"))
        if ceiling is None:
            return None
        stated = f"experience band \"{profile.get('total_experience_band')}\""
    if ceiling >= floor or history_years(row) >= floor:
        return None
    requirement = next((item.label for item in spec.mandatory_requirements if item.key == "min-experience"),
                       f"At least {floor:g} years of relevant experience")
    return Screened(
        rule=BELOW_EXPERIENCE,
        reason=f"{stated[0].upper()}{stated[1:]}; this order needs at least {floor:g}",
        category=scoring.INELIGIBLE,
        failed_requirement=requirement,
    )


def _different_trade(spec: MatchSpec, row: Dict[str, Any], base: Baseline,
                     wanted: FrozenSet[str]) -> Optional[Screened]:
    if not wanted or wanted & GENERIC:
        return None  # an order the table cannot place screens nobody out
    if base.role_hit or base.skills_found:
        return None  # any overlap at all is for Jev to weigh
    own = trades_in(_titles(row)) - GENERIC
    if not own:
        return None  # thin, unusual or generic titles: Jev reads them
    close = set(wanted)
    for key in wanted:
        close |= RELATED.get(key, frozenset())
    if trades_in(_evidence_text(row)) & close:
        return None
    theirs = ", ".join(sorted(TRADES[key].label for key in own))
    ours = ", ".join(sorted(TRADES[key].label for key in wanted))
    return Screened(
        rule=DIFFERENT_TRADE,
        reason=f"{theirs} — a different trade from {ours}, with nothing related on record",
        category=scoring.LOW,
    )


def screen(spec: MatchSpec, row: Dict[str, Any], base: Baseline,
           wanted: Optional[FrozenSet[str]] = None) -> Optional[Screened]:
    """Why this candidate need not be sent to Jev, or None to send them.

    `wanted` is `order_trades(spec)`, passed in so a run over thousands of
    candidates works it out once.
    """
    return _below_experience(spec, row) or _different_trade(
        spec, row, base, order_trades(spec) if wanted is None else wanted)


def screen_all(spec: MatchSpec, pairs: Iterable[Tuple[Dict[str, Any], Baseline]]
               ) -> Tuple[List[Tuple[Dict[str, Any], Baseline]], List[Tuple[Dict[str, Any], Baseline, Screened]]]:
    """Split candidates into (send to Jev, skipped with a reason)."""
    wanted = order_trades(spec)
    send: List[Tuple[Dict[str, Any], Baseline]] = []
    skipped: List[Tuple[Dict[str, Any], Baseline, Screened]] = []
    for row, base in pairs:
        verdict = screen(spec, row, base, wanted)
        if verdict:
            skipped.append((row, base, verdict))
        else:
            send.append((row, base))
    return send, skipped
