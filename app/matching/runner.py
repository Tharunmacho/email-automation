"""The matching job: from "Find Matching Candidates" to a stored shortlist.

    start_run       validate the order, take the per-order lock, queue the job
    execute         retrieve -> prefilter -> evaluate each -> score -> store

A run is a background thread, not a request. A pool of a few thousand with a
cap of 150 evaluations is a minute or two of Jev calls at the configured
concurrency, and no HTTP request should be held open for that. Progress is
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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from app.config import settings
from app.logging_config import get_logger
from app.matching import jev, prefilter, scoring, store
from app.matching.evidence import (
    assert_no_identifiers,
    candidate_state,
    evidence_fingerprint,
    excerpt_for,
)
from app.matching.prefilter import Baseline
from app.matching.questions import DECISION_SCHEMA_VERSION, build_questions, build_state, read_answers
from app.matching.spec import MatchSpec, normalize_job_order, spec_fingerprint

log = get_logger(__name__)

MODE_MATCH, MODE_REMATCH, MODE_REFRESH = "match", "rematch", "refresh"


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
) -> Dict[str, Any]:
    """Validate, lock and queue a run. Returns the run document.

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
    _spawn(lambda: execute(run_id, order, candidate_ids=candidate_ids, force=force))
    run.pop("active_lock", None)
    return run


def _expired(result: Dict[str, Any]) -> bool:
    evaluated = result.get("evaluated_at")
    if not isinstance(evaluated, datetime):
        return True
    if evaluated.tzinfo is None:
        evaluated = evaluated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - evaluated > timedelta(hours=settings.jev_result_ttl_hours)


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
    key = store.cache_key(spec_fp, evidence_fp, config.version, DECISION_SCHEMA_VERSION, settings.jev_model)

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
) -> None:
    """Drive one run to a terminal state. Never raises — the run records why."""
    from app.api import routes  # late: routes imports this package's router

    spec = normalize_job_order(order)
    config = scoring.config_for(order)
    spec_fp = spec_fingerprint(spec)
    store.update_run(run_id, status=store.RUN_RUNNING, started_at=datetime.now(timezone.utc))

    try:
        rows = prefilter.retrieve(routes.repo(), spec)
        if candidate_ids:
            wanted = set(candidate_ids)
            rows = [row for row in rows if str(row.get("id")) in wanted]
        pool = prefilter.shortlist_for_jev(spec, rows, settings.jev_max_candidates_per_run)
        # `skipped` is what a configured cap left out. Zero unless an operator
        # set one, and recorded either way so the screen can say so.
        store.update_run(run_id, retrieved=len(rows), considered=len(pool),
                         skipped=len(rows) - len(pool))
        log.info("Matching run %s on %s: %d retrieved, %d to evaluate, %d left out by the cap",
                 run_id, spec.order_id, len(rows), len(pool), len(rows) - len(pool))

        if not pool:
            store.finish_run(run_id, store.RUN_COMPLETED)
            return

        questions = build_questions(spec)
        outcomes = {"cached": 0, "scored": 0, "failed": 0}
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
