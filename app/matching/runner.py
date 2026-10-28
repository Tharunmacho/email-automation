"""The matching job: from "Find Matching Candidates" to a stored shortlist.

    start_run       validate the order, take the per-order lock, queue the job
    plan            which candidates are already current, which the free
                    check can decide, which need Jev
    execute         retrieve -> plan -> screen / evaluate each -> score -> store

Runs start three ways, recorded as the run's `trigger`: a recruiter pressing
a button (`user`), the panel opening on an order with something new to check
(`open`), and the background sweep that keeps every open order current
(`auto`, see `app.matching.autorun`). They are the same run; only an
automatic one holds back a candidate that has just failed, so a broken record
is not re-sent on every sweep.

A run is a background thread, not a request. A first run over a pool of a
few thousand is many minutes of Jev calls at the configured concurrency, and
no HTTP request should be held open for that. Progress is
written to the run document as it goes, so the screen polls one small record.

A thread rather than a Celery task because matching must work in every
deployment this CRM runs in, including the single-container one with no worker,
and because the work is I/O-bound waiting on one HTTP endpoint — exactly what a
small thread pool is for. The vendor-facing concurrency cap is process-wide
(`app.matching.jev`), so two runs in parallel share it rather than doubling it.

**Idempotent and resumable by construction.** Every evaluation is keyed on
everything that could change it (see `store.cache_key`). A run that is
cancelled, crashes, or fails half-way can simply be started again: the
candidates it already scored are cache hits that cost nothing, and only the
remainder is sent. "Retry" is therefore just another run.

**Failure is recorded, never filled in.** A candidate Jev could not evaluate
is stored as a failed attempt with the vendor's status and whether it is worth
retrying. The previous result for that candidate, if there was one, stays
current. Nothing here invents a score to take the place of a missing one.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import settings
from app.logging_config import get_logger
from app.matching import jev, prefilter, scoring, screen as screening, store
from app.matching.evidence import (
    assert_no_identifiers,
    candidate_state,
    evidence_fingerprint,
    excerpt_for,
)
from app.matching.prefilter import Baseline
from app.matching.questions import (
    DECISION_SCHEMA_VERSION,
    NOT_SATISFIED,
    build_questions,
    build_state,
    read_answers,
)
from app.matching.spec import MatchSpec, normalize_job_order, spec_fingerprint

log = get_logger(__name__)

MODE_MATCH, MODE_REMATCH, MODE_REFRESH = "match", "rematch", "refresh"
TRIGGER_USER, TRIGGER_OPEN, TRIGGER_AUTO = "user", "open", "auto"

#: An automatic run leaves a candidate alone for this long after Jev failed
#: on them with a retryable error (timeouts, rate limits), and indefinitely
#: after a non-retryable one, until their record or the order changes. A
#: recruiter's "Retry remaining" always re-sends them.
AUTO_RETRY_FAILED_AFTER = timedelta(hours=1)


class OrderNotFound(LookupError):
    pass


class OrderInactive(RuntimeError):
    """The order is closed or filled, and the caller did not ask for a rematch."""


def _spawn(work: Callable[[], None]) -> None:
    """Run `work` in the background. Replaced in tests with a direct call."""
    threading.Thread(target=work, name="candidate-matching", daemon=True).start()


def load_order(order_id: str) -> Dict[str, Any]:
    from app.db.mongo import get_db

    order = get_db()["job_orders"].find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise OrderNotFound(order_id)
    return order


def inactive_reason(spec: MatchSpec) -> str:
    return (
        f"Job order {spec.order_id} is {spec.status.lower()}. A new shortlist is only "
        f"generated for open or in-progress orders. Re-open the order, or run a "
        f"historical rematch to analyse it without treating the result as a live shortlist."
    )


def start_run(
    order: Dict[str, Any],
    user: Dict[str, Any],
    *,
    mode: str = MODE_MATCH,
    candidate_ids: Optional[List[str]] = None,
    force: bool = False,
    trigger: str = TRIGGER_USER,
    wait: bool = False,
    screen: bool = True,
) -> Dict[str, Any]:
    """Validate, lock and queue a run. Returns the run document.

    `wait` executes it on the calling thread instead of a new one — the
    background sweep, which works through the open orders one at a time.
    `screen=False` sends everybody to Jev, including candidates the free
    check would skip ("Check with Jev").

    Raises `jev.JevNotConfigured`, `OrderInactive`, or `store.RunConflict`; the
    API turns each into its own status code and explanation.
    """
    if not jev.is_configured():
        raise jev.JevNotConfigured(
            "JEV_API_KEY is not set on the server, so candidates cannot be matched. "
            "Add it to the server environment and restart the API."
        )
    spec = normalize_job_order(order)
    if not spec.is_active and mode != MODE_REMATCH:
        raise OrderInactive(inactive_reason(spec))

    config = scoring.config_for(order)
    run = store.create_run(spec.order_id, {
        "mode": mode,
        "trigger": trigger,
        "screen": bool(screen),
        "historical": not spec.is_active,
        "force": bool(force),
        "candidate_ids": list(candidate_ids or []) or None,
        "spec_fingerprint": spec_fingerprint(spec),
        "scoring_version": config.version,
        "schema_version": DECISION_SCHEMA_VERSION,
        "model": settings.jev_model,
        # None when uncapped, which is the default; see `jev_max_candidates_per_run`.
        "cap": settings.jev_max_candidates_per_run or None,
        "created_by": {"id": user.get("id"), "name": user.get("name") or user.get("email")},
    })
    run_id = run["id"]
    run.pop("active_lock", None)

    def work() -> None:
        execute(run_id, order, candidate_ids=candidate_ids, force=force,
                auto=trigger != TRIGGER_USER, screen=screen)

    if wait:
        work()
        return store.get_run(run_id) or run
    _spawn(work)
    return run


def _expired(result: Dict[str, Any]) -> bool:
    """Past `jev_result_ttl_hours`. Never, when that is 0 (the default)."""
    if settings.jev_result_ttl_hours <= 0:
        return False
    evaluated = result.get("evaluated_at")
    if not isinstance(evaluated, datetime):
        return True
    if evaluated.tzinfo is None:
        evaluated = evaluated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - evaluated > timedelta(hours=settings.jev_result_ttl_hours)


def _key(spec_fp: str, config: scoring.ScoringConfig, row: Dict[str, Any]) -> str:
    return store.cache_key(spec_fp, evidence_fingerprint(row), config.version,
                           DECISION_SCHEMA_VERSION, settings.jev_model)


@dataclass
class Plan:
    """What a run over these rows would do, decided before any Jev call."""

    #: The candidates the run covers, best keyword baseline first, after any cap.
    shortlist: List[Tuple[Dict[str, Any], Baseline]]
    #: Of those, the ones that need Jev: no current result, or one whose order,
    #: profile, weights, schema or model has changed since, or one past its TTL.
    pending: List[Tuple[Dict[str, Any], Baseline]]
    #: Already current: nothing to send.
    reused: int
    #: Left out by a configured cap.
    skipped: int
    #: Held back by an automatic run because Jev has just failed on them.
    deferred: int
    #: Not current, but decided by the free check — stored without a Jev call.
    screened: List[Tuple[Dict[str, Any], Baseline, screening.Screened]] = field(default_factory=list)

    @property
    def needs_run(self) -> bool:
        return bool(self.pending or self.screened)


def plan(
    spec: MatchSpec,
    config: scoring.ScoringConfig,
    rows: List[Dict[str, Any]],
    *,
    force: bool = False,
    auto: bool = False,
    screen: bool = True,
) -> Plan:
    """Split the pool into what is current, what the free check decides, and
    what needs Jev.

    Two reads for the whole order — the current keys and the failures — and
    the rest in memory. This is what makes the background sweep cheap: an
    order where nothing changed comes back with nothing pending, and no run
    is made for it at all.

    With `screen=False` a candidate the free check skipped earlier is not
    current any more: they are what "Check with Jev" is asking about.
    """
    spec_fp = spec_fingerprint(spec)
    current = {} if force else store.current_keys(spec.order_id)
    failures = store.failed_keys(spec.order_id) if auto and not force else {}
    cutoff = datetime.now(timezone.utc) - AUTO_RETRY_FAILED_AFTER
    keys = {str(row.get("id") or ""): _key(spec_fp, config, row) for row in rows}

    def is_current(candidate_id: str) -> bool:
        mine = current.get(candidate_id)
        if not mine or mine.get("cache_key") != keys[candidate_id] or _expired(mine):
            return False
        return screen or mine.get("status") != store.SCREENED

    def held_back(candidate_id: str) -> bool:
        failure = failures.get((candidate_id, keys[candidate_id]))
        if not failure or is_current(candidate_id):
            return False
        return not failure["retryable"] or (failure["at"] is not None and failure["at"] > cutoff)

    eligible = [row for row in rows if not held_back(str(row.get("id") or ""))]
    shortlist = prefilter.shortlist_for_jev(spec, eligible, settings.jev_max_candidates_per_run)
    stale = [(row, base) for row, base in shortlist if not is_current(str(row["id"]))]
    pending, screened = screening.screen_all(spec, stale) if screen else (stale, [])
    return Plan(shortlist=shortlist, pending=pending, reused=len(shortlist) - len(stale),
                skipped=len(eligible) - len(shortlist), deferred=len(rows) - len(eligible),
                screened=screened)


def save_screened(
    spec: MatchSpec,
    config: scoring.ScoringConfig,
    row: Dict[str, Any],
    base: Baseline,
    verdict: screening.Screened,
    *,
    run_id: str,
    spec_fp: str,
) -> None:
    """Store the free check's verdict as the candidate's current result.

    Shaped like a Jev result so the shortlist, filters and counts treat it the
    same — but with no score, because no score was computed, and with the
    reason in place of an evaluation.
    """
    failed = [verdict.failed_requirement] if verdict.failed_requirement else []
    requirements = [
        {"key": item.key, "label": item.label, "status": NOT_SATISFIED, "confidence": 1.0,
         "source": item.source}
        for item in spec.mandatory_requirements if item.label in failed
    ]
    store.save_scored({
        "job_order_id": spec.order_id,
        "candidate_id": str(row["id"]),
        "cache_key": _key(spec_fp, config, row),
        "run_id": run_id,
        "spec_fingerprint": spec_fp,
        "evidence_fingerprint": evidence_fingerprint(row),
        "scoring_version": config.version,
        "schema_version": DECISION_SCHEMA_VERSION,
        "model_requested": settings.jev_model,
        "model": None,
        "historical": not spec.is_active,
        "baseline": base.as_dict(),
        "screen": verdict.as_dict(),
        "score": None,
        "category": verdict.category,
        "category_label": scoring.CATEGORY_LABELS[verdict.category],
        "components": [],
        "matched_requirements": [],
        "unverified_requirements": [],
        "failed_requirements": failed,
        "reasons": [verdict.reason, "Not sent to Jev: decided by the free check. "
                                    "\"Check with Jev\" sends this candidate anyway."],
        "evaluation": {"dimensions": {}, "requirements": requirements},
        "evidence": _supporting_evidence(spec, row),
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        "evaluated_at": datetime.now(timezone.utc),
    }, status=store.SCREENED)


def _supporting_evidence(spec: MatchSpec, row: Dict[str, Any]) -> List[Dict[str, str]]:
    """Excerpts from the record for each thing the order asks about.

    Only lines that are on file. A requirement with no supporting line is left
    out here and shows up in the explanation as unverified — the excerpt list
    is evidence, not a checklist.
    """
    out: List[Dict[str, str]] = []
    wanted = [(skill, [skill]) for skill in spec.required_skills]
    if spec.role_terms:
        wanted.insert(0, (spec.designation or spec.title, spec.role_terms))
    wanted += [(item, [item]) for item in spec.required_licences + spec.required_certifications]
    for label, terms in wanted:
        found = excerpt_for(row, terms)
        if found:
            out.append({"requirement": label, **found})
    return out[:12]


def evaluate_one(
    spec: MatchSpec,
    row: Dict[str, Any],
    base: Baseline,
    questions: Dict[str, Any],
    config: scoring.ScoringConfig,
    *,
    run_id: str,
    spec_fp: str,
    historical: bool,
    force: bool = False,
) -> str:
    """Evaluate one candidate. Returns `"cached"`, `"scored"` or `"failed"`.

    Raises only `JevNotConfigured`, which is not about this candidate and stops
    the whole run.
    """
    candidate_id = str(row["id"])
    state = candidate_state(row)
    assert_no_identifiers(state)
    evidence_fp = evidence_fingerprint(row)
    key = _key(spec_fp, config, row)

    if not force:
        cached = store.find_scored(spec.order_id, candidate_id, key)
        if cached and not _expired(cached):
            store.mark_current(spec.order_id, candidate_id, key)
            return "cached"

    common = {
        "job_order_id": spec.order_id,
        "candidate_id": candidate_id,
        "cache_key": key,
        "run_id": run_id,
        "spec_fingerprint": spec_fp,
        "evidence_fingerprint": evidence_fp,
        "scoring_version": config.version,
        "schema_version": DECISION_SCHEMA_VERSION,
        "model_requested": settings.jev_model,
        "historical": historical,
        "baseline": base.as_dict(),
    }

    try:
        response = jev.system_one(build_state(spec, state), questions)
    except jev.JevError as exc:
        store.save_failed({**common, "error": {
            "message": str(exc), "status": exc.status, "retryable": exc.retryable,
        }})
        return "failed"

    evaluation = read_answers(response.answers, spec, model=response.model)
    if not evaluation.dimensions:
        # Answers came back but none of them in the shape that was asked for.
        # Scoring that would produce a confident-looking zero; recording it as
        # a failure tells the truth.
        store.save_failed({**common, "error": {
            "message": "Jev returned no usable answers for this candidate",
            "status": 200, "retryable": True,
        }, "usage": {"input_tokens": response.input_tokens, "output_tokens": response.output_tokens}})
        return "failed"

    outcome = scoring.score(spec, evaluation, config)
    store.save_scored({
        **common,
        "model": response.model,
        **outcome.as_dict(),
        "evaluation": evaluation.as_dict(),
        "evidence": _supporting_evidence(spec, row),
        "usage": {
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": round(response.cost_usd, 8),
        },
        "evaluated_at": datetime.now(timezone.utc),
    })
    store.update_run(run_id, inc={
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": response.cost_usd,
    })
    return "scored"


def execute(
    run_id: str,
    order: Dict[str, Any],
    *,
    candidate_ids: Optional[List[str]] = None,
    force: bool = False,
    auto: bool = False,
    screen: bool = True,
) -> None:
    """Drive one run to a terminal state. Never raises — the run records why."""
    from app.api import routes  # late: routes imports this package's router

    spec = normalize_job_order(order)
    config = scoring.config_for(order)
    spec_fp = spec_fingerprint(spec)
    store.update_run(run_id, status=store.RUN_RUNNING, started_at=datetime.now(timezone.utc))

    try:
        rows = prefilter.retrieve(routes.repo(), spec, candidate_ids=candidate_ids)
        work_plan = plan(spec, config, rows, force=force, auto=auto, screen=screen)
        pool = work_plan.pending
        # The free check's verdicts cost nothing and are stored first, so the
        # shortlist is complete before the first Jev answer lands.
        for row, base, verdict in work_plan.screened:
            save_screened(spec, config, row, base, verdict, run_id=run_id, spec_fp=spec_fp)
        # `skipped` is what a configured cap left out. Zero unless an operator
        # set one, and recorded either way so the screen can say so. Results
        # that are already current count as processed from the start, so what
        # is left on the progress bar is only what is actually being sent.
        store.update_run(run_id, retrieved=len(rows), considered=len(work_plan.shortlist),
                         skipped=work_plan.skipped, deferred=work_plan.deferred,
                         screened=len(work_plan.screened), cached=work_plan.reused,
                         processed=work_plan.reused + len(work_plan.screened))
        log.info("Matching run %s on %s: %d retrieved, %d current, %d skipped by the free check, "
                 "%d to evaluate, %d left out by the cap, %d held back after a recent failure",
                 run_id, spec.order_id, len(rows), work_plan.reused, len(work_plan.screened),
                 len(pool), work_plan.skipped, work_plan.deferred)

        if not pool:
            store.finish_run(run_id, store.RUN_COMPLETED)
            return

        questions = build_questions(spec)
        outcomes = {"cached": work_plan.reused, "scored": 0, "failed": 0}
        cancelled = False

        def work(row: Dict[str, Any], base: Baseline) -> Optional[str]:
            if store.cancel_requested(run_id):
                return None
            return evaluate_one(
                spec, row, base, questions, config,
                run_id=run_id, spec_fp=spec_fp, historical=not spec.is_active, force=force,
            )

        with ThreadPoolExecutor(max_workers=max(1, settings.jev_max_concurrency),
                                thread_name_prefix="match-eval") as executor:
            futures = [executor.submit(work, row, base) for row, base in pool]
            for future in as_completed(futures):
                outcome = future.result()
                if outcome is None:
                    cancelled = True
                    continue
                outcomes[outcome] += 1
                store.update_run(run_id, inc={"processed": 1, outcome: 1})

        if cancelled:
            status = store.RUN_CANCELLED
        elif outcomes["failed"] and (outcomes["scored"] or outcomes["cached"]):
            status = store.RUN_PARTIAL
        elif outcomes["failed"]:
            status = store.RUN_FAILED
        else:
            status = store.RUN_COMPLETED
        error = (f"{outcomes['failed']} candidate evaluation(s) failed; the run can be retried "
                 f"and will only re-send those") if outcomes["failed"] else None
        store.finish_run(run_id, status, error)
        log.info("Matching run %s finished %s: %s", run_id, status, outcomes)
    except jev.JevNotConfigured as exc:
        store.finish_run(run_id, store.RUN_FAILED, str(exc))
    except Exception as exc:  # noqa: BLE001 — a run must always reach a terminal state
        log.exception("Matching run %s crashed", run_id)
        store.finish_run(run_id, store.RUN_FAILED, f"Matching stopped unexpectedly: {type(exc).__name__}")
