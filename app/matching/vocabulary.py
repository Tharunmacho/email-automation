"""The words of recruitment: how CVs and job orders say the same thing differently.

Matching without a language model stands or falls on vocabulary. A CV says
"argon arc", the order says "TIG"; a WhatsApp intake says "fabricator", the
order says "welder"; OCR turns "welding" into "weldng". This module is where
those differences are made to meet, in three layers, each applied identically
to the order and to the candidate so a crude rule still matches itself:

    stem         a deliberately light stemmer — plurals, then -ing / -er —
                 so "welders", "welding" and "welder" are one word
    EQUIVALENTS  different words for the *same* thing, rewritten to one
                 canonical word: gtaw / argon arc / tungsten inert gas -> tig
    TRADES       occupations, how titles and CVs name them, and which other
                 trades are close enough to count for something

Typos are handled where the vocabulary is known — in `matcher`, against the
words that actually occur in the candidate pool.

All of it is plain data. A trade or synonym that is missing costs recall on
that one term and nothing else; adding it is a one-line change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, FrozenSet, Iterable, List, Tuple

_WORD = re.compile(r"[a-z0-9+#]+")

#: Words that say nothing about a skill or a trade.
STOP_WORDS = frozenset({
    "and", "the", "for", "with", "of", "in", "a", "an", "to", "at", "on", "or", "by", "as",
    "senior", "junior", "sr", "jr", "cum", "lead", "chief", "head", "trainee", "experienced",
    "skilled", "semi", "general", "staff", "worker", "work", "job", "post", "position",
    "required", "urgent", "urgently", "need", "needed", "vacancy", "candidate", "year", "years",
    "experience", "good", "knowledge", "ability", "able", "must", "should", "will", "can",
})


@lru_cache(maxsize=200_000)
def stem(token: str) -> str:
    """Plurals, then -ing / -er. Applied to both sides, so it only has to be consistent."""
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


def _stemmed(text: str) -> str:
    return " ".join(stem(word) for word in _WORD.findall((text or "").lower()))


#: Different words for the same thing. The first entry is what every other
#: entry is rewritten to. Single words cover what the stemmer misses
#: ("fabrication" / "fabricator"); phrases cover the trade's own synonyms.
EQUIVALENTS: Tuple[Tuple[str, ...], ...] = (
    # Welding processes
    ("tig", "gtaw", "argon arc", "argon welding", "argon", "heliarc", "tungsten inert gas",
     "tungsten arc"),
    ("mig", "mag", "gmaw", "co2 welding", "metal inert gas"),
    ("smaw", "stick welding", "manual metal arc", "mma", "shielded metal arc"),
    ("fcaw", "flux cored", "flux core"),
    ("saw", "submerged arc"),
    # Word forms the stemmer does not join
    ("fabricat", "fabrication", "fabricator", "fabricate", "fabricated"),
    ("machinist", "machining"),
    ("install", "installation", "installer"),
    ("maintenance", "maintain", "maintaining", "maintained"),
    ("operat", "operation", "operator", "operating", "operate", "operated"),
    ("inspect", "inspection", "inspector"),
    ("supervis", "supervisor", "supervision", "supervising", "supervise"),
    ("account", "accountant", "accountancy"),
    ("nurse", "nursing"),
    ("mason", "masonry"),
    ("carpent", "carpentry", "carpenter"),
    ("electric", "electrical", "electrician"),
    ("plumb", "plumbing", "plumber"),
    ("pipe fitt", "pipefitter", "pipe fitter", "pipe fitting", "pipefitting"),
    ("bricklay", "bricklayer", "brick layer", "bricklaying"),
    ("scaffold", "scaffolder", "scaffolding"),
    ("forklift", "fork lift"),
    # Vehicles and licences
    ("lmv", "light motor vehicle", "light vehicle", "light duty vehicle"),
    ("hmv", "heavy motor vehicle", "heavy vehicle", "heavy duty vehicle"),
    # Building services
    ("hvac", "heating ventilation air conditioning", "heating ventilation and air conditioning"),
    ("aircon", "air conditioning", "air conditioner", "air condition"),
    # Quality and safety
    ("qaqc", "qa qc", "qa/qc", "quality assurance quality control"),
    ("ndt", "non destructive testing", "nondestructive testing"),
    ("hse", "health safety environment", "health and safety"),
    # Office tools
    ("autocad", "auto cad"),
    ("msoffice", "ms office", "microsoft office"),
    ("excel", "ms excel", "microsoft excel"),
    ("tally", "tally erp"),
)


def _build_equivalents() -> Tuple[re.Pattern, Dict[str, str]]:
    canonical: Dict[str, str] = {}
    for group in EQUIVALENTS:
        target = _stemmed(group[0])
        for phrase in group:
            stemmed = _stemmed(phrase)
            if stemmed and stemmed != target:
                canonical.setdefault(stemmed, target)
    phrases = sorted(canonical, key=len, reverse=True)
    pattern = re.compile(r"(?<![a-z0-9+#])(?:" + "|".join(re.escape(p) for p in phrases) + r")(?![a-z0-9+#])")
    return pattern, canonical


_EQUIV_PATTERN, _CANONICAL = _build_equivalents()


def normalise(text: str) -> str:
    """Lower-cased, stemmed, with every equivalent rewritten to its canonical word."""
    stemmed = _stemmed(text)
    if not stemmed:
        return ""
    return _EQUIV_PATTERN.sub(lambda match: _CANONICAL[match.group(0)], stemmed)


def tokens(text: str) -> List[str]:
    """`normalise`, split into words."""
    return normalise(text).split()


def terms(text: str) -> List[str]:
    """The words of `text` that carry meaning: no stop words, nothing shorter than two letters."""
    return terms_of_normalised(normalise(text))


def terms_of_normalised(text: str) -> List[str]:
    """`terms`, for text that has already been through `normalise`."""
    stops = _STEMMED_STOPS
    return [word for word in text.split() if len(word) > 1 and word not in stops]


_STEMMED_STOPS = frozenset(stem(word) for word in STOP_WORDS)


def raw_phrases_for(text: str) -> List[str]:
    """`text` plus the everyday spellings of anything in it that has equivalents.

    For quoting evidence: the record says "argon arc", the order says "TIG",
    and the excerpt search has to look for the record's words.
    """
    found = set(tokens(text))
    out = [text]
    for group in EQUIVALENTS:
        if _stemmed(group[0]) in found:
            out.extend(group)
    return out


@dataclass(frozen=True)
class Trade:
    label: str
    #: How a title or a CV names this trade.
    names: Tuple[str, ...]
    #: Trades close enough to count for something. Symmetric: listing B under
    #: A is enough.
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


def _build_trades() -> Tuple[re.Pattern, Dict[str, str]]:
    owner: Dict[str, str] = {}
    for key, trade in TRADES.items():
        for name in trade.names:
            phrase = normalise(name)
            if phrase:
                owner.setdefault(phrase, key)
    phrases = sorted(owner, key=len, reverse=True)
    pattern = re.compile(r"(?<![a-z0-9+#])(?:" + "|".join(re.escape(p) for p in phrases) + r")(?![a-z0-9+#])")
    return pattern, owner


_TRADE_PATTERN, _TRADE_OWNER = _build_trades()


def _related_map() -> Dict[str, FrozenSet[str]]:
    out: Dict[str, set] = {key: set() for key in TRADES}
    for key, trade in TRADES.items():
        for other in trade.related:
            out[key].add(other)
            out[other].add(key)
    return {key: frozenset(value) for key, value in out.items()}


RELATED: Dict[str, FrozenSet[str]] = _related_map()


def trades_in_normalised(text: str) -> FrozenSet[str]:
    """Trades named in text that has already been through `normalise`."""
    return frozenset(_TRADE_OWNER[name] for name in set(_TRADE_PATTERN.findall(text)))


def trades_in(text: str) -> FrozenSet[str]:
    """Every trade named anywhere in `text`."""
    return trades_in_normalised(normalise(text))


def related_to(keys: Iterable[str]) -> FrozenSet[str]:
    """The trades related to any of `keys`, not counting `keys` themselves."""
    keys = set(keys)
    out: set = set()
    for key in keys:
        out |= RELATED.get(key, frozenset())
    return frozenset(out - keys)


def labels(keys: Iterable[str]) -> str:
    return ", ".join(sorted(TRADES[key].label for key in keys if key in TRADES))
