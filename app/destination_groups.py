"""The destination groups a Sourcing Hub agent recruits for.

An agent is offered five choices: Singapore, Malaysia, Mauritius, GCC
countries and Europe. Agents recruit for a region, not a list of countries they would have to
keep up to date, so choosing "Europe" sends the agent every job order for any
European country, and "GCC countries" every order for any of the six Gulf
states.

This module is the one place the groups and their member countries are
written down. The job order broadcast (`app.agent_whatsapp`) matches on them,
and `GET /sourcing-clients` sends them to the Sourcing Hub, which offers the
group names and maps an agent's older individual countries onto their group
when the agent is edited.

A job order's own `destinationCountry` stays one real country. Only the
agent's side is grouped.

Europe is meant broadly, the way overseas recruitment uses the word: the EU and
EEA, Switzerland and the UK, the Balkans, Eastern Europe including Russia,
Ukraine, Belarus and Moldova, the South Caucasus (Armenia, Georgia, Azerbaijan)
and Turkey. Russia and Armenia are in it on purpose: job orders already go out
for both, and with only these few choices an order for a country outside every
group would reach no agent at all.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

SINGAPORE = "Singapore"
MALAYSIA = "Malaysia"
MAURITIUS = "Mauritius"
GCC = "GCC countries"
EUROPE = "Europe"

#: The six Gulf Cooperation Council states.
GCC_COUNTRIES: Tuple[str, ...] = (
    "Saudi Arabia",
    "United Arab Emirates",
    "Qatar",
    "Kuwait",
    "Oman",
    "Bahrain",
)

EUROPEAN_COUNTRIES: Tuple[str, ...] = (
    "Albania",
    "Andorra",
    "Armenia",
    "Austria",
    "Azerbaijan",
    "Belarus",
    "Belgium",
    "Bosnia and Herzegovina",
    "Bulgaria",
    "Croatia",
    "Cyprus",
    "Czech Republic",
    "Denmark",
    "Estonia",
    "Finland",
    "France",
    "Georgia",
    "Germany",
    "Greece",
    "Hungary",
    "Iceland",
    "Ireland",
    "Italy",
    "Kosovo",
    "Latvia",
    "Liechtenstein",
    "Lithuania",
    "Luxembourg",
    "Malta",
    "Moldova",
    "Monaco",
    "Montenegro",
    "Netherlands",
    "North Macedonia",
    "Norway",
    "Poland",
    "Portugal",
    "Romania",
    "Russia",
    "San Marino",
    "Serbia",
    "Slovakia",
    "Slovenia",
    "Spain",
    "Sweden",
    "Switzerland",
    "Turkey",
    "Ukraine",
    "United Kingdom",
    "Vatican City",
)

#: The choices, in the order the Sourcing Hub offers them.
DESTINATION_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (SINGAPORE, (SINGAPORE,)),
    (MALAYSIA, (MALAYSIA,)),
    (MAURITIUS, (MAURITIUS,)),
    (GCC, GCC_COUNTRIES),
    (EUROPE, EUROPEAN_COUNTRIES),
)

#: Other names the same group or country is written under. Matching only; the
#: Sourcing Hub never offers these.
_ALIASES: Dict[str, Tuple[str, ...]] = {
    GCC: ("GCC", "Gulf", "Gulf countries", "Gulf states"),
    "United Arab Emirates": ("UAE", "U.A.E."),
    "Czech Republic": ("Czechia",),
    "Netherlands": ("The Netherlands", "Holland"),
    "North Macedonia": ("Macedonia",),
    "United Kingdom": ("UK", "U.K.", "Great Britain", "Britain", "England", "Scotland", "Wales",
                       "Northern Ireland"),
    "Turkey": ("Türkiye", "Turkiye"),
    "Russia": ("Russian Federation",),
    "Moldova": ("Republic of Moldova",),
    "Bosnia and Herzegovina": ("Bosnia",),
    "Vatican City": ("Vatican", "Holy See"),
}


def country_key(value: Any) -> str:
    """A country or group name for comparing: case and spacing do not matter."""
    return " ".join(str(value or "").split()).casefold()


def _names(name: str) -> Tuple[str, ...]:
    return (name, *_ALIASES.get(name, ()))


def _build_index() -> Dict[str, frozenset[str]]:
    """`{key of a group name or alias: keys of every name the group covers}`."""
    index: Dict[str, frozenset[str]] = {}
    for group, members in DESTINATION_GROUPS:
        covered = {country_key(alias) for alias in _names(group)}
        for member in members:
            covered.update(country_key(alias) for alias in _names(member))
        for alias in _names(group):
            index[country_key(alias)] = frozenset(covered)
    return index


_GROUP_INDEX = _build_index()


def expand(value: Any) -> frozenset[str]:
    """Every match key one stored or ordered value stands for.

    A group ("Europe") stands for its own name and every member country, under
    each of their aliases; a plain country ("Germany") stands for itself and its
    aliases. Blank is nothing.
    """
    key = country_key(value)
    if not key:
        return frozenset()
    if key in _GROUP_INDEX:
        return _GROUP_INDEX[key]
    for name, aliases in _ALIASES.items():
        if key in {country_key(alias) for alias in (name, *aliases)}:
            return frozenset(country_key(alias) for alias in (name, *aliases))
    return frozenset({key})


def public_groups() -> List[Dict[str, Any]]:
    """The groups as the Sourcing Hub reads them: name and member countries."""
    return [{"name": group, "countries": list(members)} for group, members in DESTINATION_GROUPS]
