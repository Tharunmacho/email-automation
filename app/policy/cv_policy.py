"""Whether a candidate must supply a CV.

Why this lives in the CRM
-------------------------
The WhatsApp bot has to know the answer *during* the conversation — it decides
whether the next thing it says is "please send your CV" or the question after
that. So the bot needs the answer, and the obvious shortcut is to let the bot
decide and tell us what it decided.

That shortcut does not work, and the reason is worth stating plainly: a payload
that carries both `cv_required` and the resume it is meant to justify is a
payload that certifies itself. Anything sending `cv_required: false` skips the
check. The rule would exist in the schema and not in reality.

So the rule lives here, the CRM computes it from `(destination_country,
job_category)`, and what the bot sends is a *claim* — recorded, compared, never
trusted. The bot asks `GET /policy/cv-required` so it usually agrees with us,
and `POST /candidates` re-derives the answer anyway so that agreement is never
load-bearing.

Why the job category and not the job title
------------------------------------------
Candidates type "General Worker", "general labour", "helper", "GW". A table
keyed on what people type matches almost nothing and silently falls through to
the default, which looks like a working policy and is not one. The bot offers a
fixed list of categories, sends the id, and their own words travel alongside in
`job_preference` for a human to read.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: Matches any country, or any job category, in a rule.
ANY = "*"

#: The categories the built-in table knows about.
#:
#: No longer the list of what may be *sent*: jobs are rows now, an admin adds
#: them, and `known_job_ids()` below is what the API validates against. This
#: tuple survives as the fallback vocabulary for a database that has not been
#: seeded yet — a fresh install answering its first request before startup has
#: finished — and as the seed list's own source of truth.
JOB_CATEGORIES = (
    "general_worker",
    "factory_warehouse",
    "packing",
    "cleaning_housekeeping",
    "hospitality",
    "construction",
    "driver_operator",
    "fabrication_welding",
    "electrical_mechanical",
    "sales_retail",
    "technician",
    "other",
)


def known_job_ids() -> tuple[str, ...]:
    """Every job id the CRM will accept on a submission.

    Read from the table, so a job an admin created five minutes ago is accepted
    the moment it exists — the whole point of making these rows. Falls back to
    the built-in tuple when the database cannot be reached, which keeps intake
    working on the ids that have always existed rather than refusing everything.
    """
    try:
        from app.db.taxonomy import list_jobs

        ids = tuple(str(job["id"]) for job in list_jobs() if job.get("id"))
        return ids or JOB_CATEGORIES
    except Exception as exc:  # noqa: BLE001 — a lookup failure must not close intake
        log.warning("job list unavailable (%s); accepting the built-in categories", exc)
        return JOB_CATEGORIES


@dataclass(frozen=True)
class CvRule:
    destination_country: str
    job_category: str
    cv_required: bool

    @property
    def specificity(self) -> int:
        """How exact this rule is; higher wins. See `_match`."""
        return (self.destination_country != ANY) + (self.job_category != ANY)


@dataclass(frozen=True)
class CvPolicy:
    version: str
    default_cv_required: bool
    rules: List[CvRule]

    def is_cv_required(
        self,
        destination_country: Optional[str],
        job_category: Optional[str],
    ) -> bool:
        """The authoritative answer for one destination and job category.

        An unknown country or category falls back to `default_cv_required`,
        which ships as True. That direction matters: the failure mode of
        defaulting to "required" is a candidate asked for a CV they did not
        strictly need, and the failure mode of defaulting to "not required" is a
        candidate reaching a client without one. The first is an inconvenience
        and the second loses a placement.
        """
        country = _norm_country(destination_country)
        category = _norm_category(job_category)

        matched = _match(self.rules, country, category)
        if matched is None:
            log.debug(
                "cv policy: no rule for (%s, %s); using default %s",
                country or "?",
                category or "?",
                self.default_cv_required,
            )
            return self.default_cv_required
        return matched.cv_required


def _norm_country(value: Optional[str]) -> str:
    """Country names arrive with different casing and spacing from every source."""
    return (value or "").strip().casefold()


def _norm_category(value: Optional[str]) -> str:
    return (value or "").strip().casefold().replace(" ", "_").replace("-", "_")


def _match(rules: List[CvRule], country: str, category: str) -> Optional[CvRule]:
    """The most specific rule that applies, or None.

    Specificity rather than file order, so a table can carry a broad rule
    ("anything going to Malaysia needs no CV") and an exception to it ("except
    CNC operators") without the exception's position in the list deciding
    whether it works. Order-dependent tables are the reason policy files rot.
    """
    candidates = [
        rule
        for rule in rules
        if rule.destination_country in (ANY, country)
        and rule.job_category in (ANY, category)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.specificity)


# --------------------------------------------------------------------------- #
#  Loading
# --------------------------------------------------------------------------- #

#: Shipped so the system has a coherent answer before anyone writes a policy
#: file. These are a starting point and are expected to be replaced — the real
#: rules belong in the JSON at `settings.cv_policy_path`, where changing them
#: does not require a deploy.
DEFAULT_POLICY: Dict[str, Any] = {
    "version": "builtin-1",
    "default_cv_required": True,
    "rules": [
        # Low-skill roles in the South-East Asian corridor are placed off a
        # profile and an interview; a CV adds nothing the bot has not collected.
        {"destination_country": "Malaysia", "job_category": "general_worker", "cv_required": False},
        {"destination_country": "Malaysia", "job_category": "factory_warehouse", "cv_required": False},
        {"destination_country": "Malaysia", "job_category": "packing", "cv_required": False},
        {"destination_country": "Malaysia", "job_category": "cleaning_housekeeping", "cv_required": False},
        {"destination_country": "Malaysia", "job_category": "construction", "cv_required": False},
        {"destination_country": "Singapore", "job_category": "general_worker", "cv_required": False},
        {"destination_country": "Singapore", "job_category": "factory_warehouse", "cv_required": False},
        {"destination_country": "Singapore", "job_category": "packing", "cv_required": False},
        {"destination_country": "Singapore", "job_category": "cleaning_housekeeping", "cv_required": False},
        {"destination_country": "Singapore", "job_category": "construction", "cv_required": False},
        # Skilled and certificated roles are placed against a client
        # specification, and the CV is what the client reads.
        {"destination_country": ANY, "job_category": "technician", "cv_required": True},
        {"destination_country": ANY, "job_category": "electrical_mechanical", "cv_required": True},
        {"destination_country": ANY, "job_category": "fabrication_welding", "cv_required": True},
        {"destination_country": ANY, "job_category": "driver_operator", "cv_required": True},
    ],
}


def _parse(raw: Dict[str, Any]) -> CvPolicy:
    rules = [
        CvRule(
            destination_country=_norm_country(r.get("destination_country", ANY)) or ANY,
            job_category=_norm_category(r.get("job_category", ANY)) or ANY,
            cv_required=bool(r.get("cv_required", True)),
        )
        for r in raw.get("rules", [])
    ]
    return CvPolicy(
        version=str(raw.get("version", "unversioned")),
        default_cv_required=bool(raw.get("default_cv_required", True)),
        rules=rules,
    )


@lru_cache(maxsize=1)
def get_policy() -> CvPolicy:
    """The active policy, read once per process.

    A bad or missing file falls back to the built-in table and says so loudly
    rather than failing the process. An unreadable policy file must not take
    candidate intake down with it — every rule it would have supplied defaults
    to "CV required", which is the safe direction.
    """
    path = (settings.cv_policy_path or "").strip()
    if not path:
        return _parse(DEFAULT_POLICY)

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any failure means "use the default"
        log.error("cv policy at %s could not be read (%s); using the built-in table", path, exc)
        return _parse(DEFAULT_POLICY)

    policy = _parse(raw)
    log.info("cv policy %s loaded from %s (%d rules)", policy.version, path, len(policy.rules))
    return policy


def reset_policy_cache() -> None:
    """Drop the cached policy. For tests, and for a future reload endpoint."""
    get_policy.cache_clear()


# --------------------------------------------------------------------------- #
#  Resolution
#
#  The rule an admin writes on a job, and the rule a file used to carry, are two
#  answers to the same question. This is the order they are asked in.
# --------------------------------------------------------------------------- #
def resolve_cv_requirement(
    destination_country: Optional[str], job_id: Optional[str]
) -> tuple[bool, str]:
    """Whether this candidate needs a CV, and what decided it.

    In order, first hit wins:

    1. **The job's country override.** "General Worker needs a CV, except in
       Malaysia and Singapore." The most specific thing anyone said, and the
       shape the admin form is built around.
    2. **The job's default.** What the admin set when they created it, applying
       to every destination without an exception of its own.
    3. **The built-in table**, for a job id that predates the rows or arrives
       from somewhere unexpected — this is what keeps a system that has not been
       seeded yet answering the way it always did.
    4. **Required.** An unknown job going to an unknown country is asked for a
       CV, because the cost of an unnecessary CV is one question and the cost of
       a missing one is a placement.

    The second element of the tuple is the reason, and it exists because "why
    was this candidate not asked for a CV?" is a question someone asks months
    later, in front of a client, about a record whose rules have since changed.
    """
    country = _norm_country(destination_country)
    job = _norm_category(job_id)

    if job:
        try:
            from app.db.taxonomy import get_job, normalise_country

            row = get_job(job)
            if row:
                overrides = row.get("cv_overrides") or {}
                if country and normalise_country(country) in overrides:
                    return bool(overrides[normalise_country(country)]), (
                        f"{row.get('title', job)} in {str(destination_country).strip()}"
                    )
                return bool(row.get("cv_required_default", True)), (
                    f"{row.get('title', job)} default"
                )
        except Exception as exc:  # noqa: BLE001 — never fail a registration on a lookup
            log.warning("job lookup failed for %r (%s); falling back to the built-in table", job, exc)

    policy = get_policy()
    matched = _match(policy.rules, country, job)
    if matched is not None:
        return matched.cv_required, f"built-in rule ({policy.version})"
    return policy.default_cv_required, f"built-in default ({policy.version})"


def is_cv_required(destination_country: Optional[str], job_category: Optional[str]) -> bool:
    """Whether a CV is required. The CRM's answer, and the only one that counts."""
    required, _reason = resolve_cv_requirement(destination_country, job_category)
    return required


def cv_requirement_reason(
    destination_country: Optional[str], job_category: Optional[str]
) -> str:
    """Which rule answered, in words. For the admin screen and the audit trail."""
    _required, reason = resolve_cv_requirement(destination_country, job_category)
    return reason


def policy_version() -> str:
    """The stamp recorded on candidates as `cv_policy_version`.

    Derived from the table when there is one, so an admin editing a rule is
    visible in the record of every candidate registered afterwards — and, just
    as importantly, in the *absence* of a change on every candidate registered
    before. Historical values are never recomputed.
    """
    try:
        from app.db.taxonomy import taxonomy_version

        return taxonomy_version()
    except Exception:  # noqa: BLE001
        return get_policy().version
