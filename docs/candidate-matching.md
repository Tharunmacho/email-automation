# Candidate matching (Jev)

Matching reads the existing, already-OCR'd candidate profiles against a job order's
requirements, asks Jev (TypeSafe's System One model) a fixed set of typed questions about
each pair, and ranks the result with an explainable score. It is **preloaded**: every
open order is kept up to date in the background, so a recruiter opens an order to a
shortlist that is already there. Nothing re-runs OCR and nothing writes to a candidate
record.

The score is a **matching score** (0–100): how well the evidence on file meets the
order. It is not a probability of being hired.

## Flow

```
background sweep / order saved / panel opened / button     app/matching/autorun.py
  -> plan       only what is not current yet; nothing pending = no run          app/matching/runner.py
  -> spec       normalise the order: mandatory vs preferred, nothing invented   app/matching/spec.py
  -> prefilter  everyone not yet submitted; keyword baseline orders, never cuts app/matching/prefilter.py
  -> evidence   PII-minimised candidate state, identifiers asserted absent      app/matching/evidence.py
  -> questions  versioned decision schema (score / choice / noul)               app/matching/questions.py
  -> jev        POST api.typesafe.ai/v1/systemone, timeout, retries, cap        app/matching/jev.py
  -> scoring    weighted components + separate mandatory gate -> category       app/matching/scoring.py
  -> store      cached by order/profile/config/schema/model version             app/matching/store.py
```

Runs are background threads (`app/matching/runner.py`), polled by the screen. A run is
idempotent: re-running reuses every result whose inputs did not change, so "retry"
only re-sends what failed. Before sending anything a run *plans* (`runner.plan`): one
read of the order's current result keys, and every candidate whose key is unchanged is
skipped in memory — no Jev call and no database write.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `JEV_API_KEY` | *(required)* | TypeSafe key. Server-side only; never `NEXT_PUBLIC_`. Missing → HTTP 503, no fallback provider. |
| `JEV_BASE_URL` | `https://api.typesafe.ai` | API host |
| `JEV_MODEL` | `jev-latest` | Model or alias; the answering model is stored on each result |
| `JEV_TIMEOUT_SECONDS` | `30` | Per request |
| `JEV_MAX_RETRIES` | `3` | Attempts on 429 / 5xx / 529 / network errors (exponential backoff, honours `retry-after`) |
| `JEV_RETRY_BACKOFF_SECONDS` | `0.5` | Base backoff |
| `JEV_MAX_CONCURRENCY` | `4` | Requests in flight, process-wide |
| `JEV_MAX_CANDIDATES_PER_RUN` | `0` | Optional ceiling per run. `0` = every eligible candidate is sent to Jev. A cap is reported on the run (`skipped`) and on screen. |
| `JEV_RESULT_TTL_HOURS` | `336` | Re-ask after this long even if nothing changed (picks up a moved alias) |
| `JEV_AUTO_MATCH` | `true` | Keep every open order analysed in the background. `false` = only when a recruiter presses the button |
| `JEV_AUTO_MATCH_INTERVAL_SECONDS` | `300` | How often the background sweep looks for new or changed candidates |

## MongoDB

New collections only; created by `ensure_indexes()` at API startup. No migration, no
change to existing collections.

| Collection | Index | Why |
|---|---|---|
| `candidate_matches` | unique `(job_order_id, candidate_id, cache_key)` | one result per version of a pair — no duplicates, no double spend |
| | `(job_order_id, is_current)` | the shortlist read |
| | `(candidate_id, updated_at)` | per-candidate history |
| `candidate_match_reviews` | unique `(job_order_id, candidate_id)` | one recruiter decision per pair, kept across re-scoring |
| `candidate_match_runs` | unique `id`; `(job_order_id, created_at)` | progress |
| | unique sparse `active_lock` | at most one active run per order, enforced by the database |

`cache_key = {spec fingerprint}:{evidence fingerprint}:{scoring config version}:{schema version}:{model}`.

## Routes

All under `require_page("job-orders")` (404 otherwise), except health (admin).

| Method | Path | |
|---|---|---|
| GET | `/job-orders/matching/health` | admin: calls, failures, retries, rate limits, tokens, cost, p50/p95 latency |
| GET | `/job-orders/{id}/matching` | configured?, active?, normalised spec, latest run, counts, `coverage` (checked X of the live pool Y, not yet checked, left out by a cap) |
| POST | `/job-orders/{id}/matching/preload` | called as the panel opens: starts a run only if something is not current, sends only that → `{"pending": n, "run": run\|null}`; a run in progress is returned, not refused |
| GET | `/job-orders/{id}/matching/latest-run` | `{"run": …}` — lets a screen left open notice a background run |
| POST | `/job-orders/{id}/matching/runs` | start; body `{"mode": "match"\|"rematch", "force": false}` → 202 |
| GET | `/job-orders/{id}/matching/runs/{run_id}` | progress |
| POST | `/job-orders/{id}/matching/runs/{run_id}/cancel` | |
| POST | `/job-orders/{id}/matching/runs/{run_id}/retry` | new run; only uncached/failed candidates are sent |
| GET | `/job-orders/{id}/matching/results` | ranked; filters `category, eligibility, documents, review, skill, min_experience, page, page_size` |
| GET | `/job-orders/{id}/matching/results/{candidate_id}` | the explanation |
| POST | `/job-orders/{id}/matching/results/{candidate_id}/refresh?force=` | re-evaluate one candidate |
| PUT | `/job-orders/{id}/matching/results/{candidate_id}/review` | `{"status": "shortlisted"\|"rejected"\|"manual_review"\|"pending", "note": ""}` |

Status codes: `503` key not configured · `409` order closed/filled (use `mode: "rematch"`)
or a run already active (body carries that run) · `404` unknown order/run/match.

Start a run:

```http
POST /job-orders/JO-7/matching/runs
{"mode": "match"}

202
{"id": "MR-3f2a…", "job_order_id": "JO-7", "status": "queued", "mode": "match",
 "historical": false, "retrieved": 0, "considered": 0, "processed": 0, "scored": 0,
 "cached": 0, "skipped": 0, "failed": 0, "cost_usd": 0.0, "model": "jev-latest", "cap": null, …}
```

One result row:

```json
{"candidate_id": "c-81", "name": "…", "designation": "Pipe Fabricator",
 "experience_years": 5, "score": 66.3, "category": "manual_review",
 "matched_skills": ["TIG Welding"], "skills_without_evidence": [],
 "unverified_requirements": ["Licence: Singapore MOM welder certification"],
 "failed_requirements": [], "eligibility": "unverified",
 "documents": {"band": "partial", "value": 0.27, "label": "…"},
 "review": {"status": "pending", "note": "", "by_name": null, "at": null},
 "failure": null, "stale": {"order_changed": false, "candidate_changed": false}}
```

## Decision schema `jev-match-1`

| Question | Type | Notes |
|---|---|---|
| `role_relevance`, `skills_match`, `experience_match`, `qualifications_match`, `document_readiness`, `evidence_quality` | score, 5 levels | level 0 is always "the record does not say" and is treated as *no evidence*, not a low mark |
| `salary_fit`, `destination_fit`, `availability_fit` | choice | each has `not_stated` |
| `equivalent_title`, `conflicting_evidence` | noul | conflict ≥ 0.5 → manual review |
| `requirement__{n}` (one per mandatory requirement) | choice: `satisfied` / `not_satisfied` / `unknown` | `satisfied` below 0.6 confidence counts as `unknown` |

Never sent: name, email, phone(s), passport number, licence numbers, addresses, profile
URLs, raw OCR. Passport is sent as "on file" + expiry date only. Enforced on every request
by `assert_no_identifiers`.

## Scoring `score-1`

| Component | Default weight | From | Applicable when |
|---|---|---|---|
| skills | 30% | mean(role_relevance, skills_match) | always (skills_match only if the order lists skills) |
| experience | 25% | experience_match | always |
| qualifications | 15% | qualifications_match | order names a licence/certification |
| salary | 10% | salary_fit | order states a salary |
| mobility | 10% | mean(destination_fit, availability_fit) | order states a destination or date |
| documents | 10% | document_readiness | always |

`score = 100 × Σ(weight × value) / Σ(weight)` over applicable components. An applicable
component with no evidence scores 0 and is listed as such.

Category, in order: any mandatory **not met** → `ineligible`; any **unknown** → `manual_review`;
self-contradicting or very sparse record → `manual_review`; `≥ 75` → `strong_match`;
`≥ 50` → `potential_match`; else `low_match`. Ranking sorts by category, then score, so a
score can never lift an ineligible candidate.

Weights and thresholds are untested defaults. Override per order by storing
`matchingWeights` / `matchingThresholds` on the job-order document; the change is part of
the cache key and rescores.

## Running locally

```bash
# backend (reads .env)
uvicorn app.api.routes:app --reload --port 8000
# frontend
cd frontend && npm run dev
```

Open a job order → **AI candidate matching**. With a key set, the list fills in by
itself; the button re-checks on demand.

Tests: `python -m pytest tests/test_matching_*.py`.

Offline evaluation against recruiter decisions (shortlisted = relevant, rejected = not):

```bash
python -m app.matching.evaluation JO-1 JO-2
```

reports precision@10, recall@20, NDCG@10 for Jev and for the keyword baseline, plus an
upper bound on mandatory-eligibility false positives and the cost.

## Cost

Measured on one live call (2026-10-28, `jev-1.14.0`): 2,206 input tokens per candidate,
output free, at the documented $0.042 per million input tokens ≈ **$0.00009 per
candidate**: about **$0.09 per 1,000 candidates** on a first run. Larger profiles (long
work histories) cost proportionally more; re-runs with unchanged inputs cost nothing, so
after the first run only new or changed candidates are paid for.

## Preloading (`app/matching/autorun.py`)

Nobody has to ask for an analysis. Three things start one, each recorded on the run as
its `trigger`:

| Trigger | When | |
|---|---|---|
| `auto` | every `JEV_AUTO_MATCH_INTERVAL_SECONDS` (5 min), and as soon as a job order is created or saved | the sweep walks the open orders newest first, one at a time |
| `open` | a recruiter opens the order's panel | catches up whatever the sweep has not reached yet |
| `user` | a recruiter presses Re-run / Retry / Re-check | as before |

All three are the same run with the same cache and the same per-order lock. Each sends
only what is not current — a new candidate, a changed CV, an edited order, a result past
its TTL — and an order where nothing changed creates no run and makes no Jev call.
Closed and filled orders are never matched automatically.

An automatic run holds back a candidate Jev just failed on: for an hour after a
retryable failure (timeout, rate limit), and until the record or order changes after a
non-retryable one, so a broken record is not re-sent every five minutes. They show as
"could not be evaluated"; **Retry remaining** sends them at once.

The panel follows a background run as it goes (one small read every 30 seconds while
idle) and only announces runs a recruiter started.

**Cost.** Preloading pays for every open order, opened or not: roughly $0.09 per 1,000
candidates per order on the first pass, then only for what changes. Twenty open orders
over a pool of 3,000 is about $5.40 once. Set `JEV_AUTO_MATCH=false` to go back to
on-demand matching.

**Never in tests.** The sweep and the save hook check `_under_test()`, so the suite can
never reach the vendor.

## Who is analysed

**Everyone except candidates already submitted.** The pool is
(`app/matching/prefilter.py::retrieval_query`):

| Excluded | Why |
|---|---|
| `recruitment_status` other than `available` | already submitted — to a company or associate, interviewing, selected, on hold, offer issued or accepted — on **any** order, this one included |
| `placement_locked` | placed (implied by the above; kept as a second guard) |
| `duplicate_of` set | a second copy of a person already analysed under their original record |

Nothing else. Candidates a recruiter entered without a CV are analysed (a thin record
lands in manual review). A candidate released back to `available` (rejected, offer
declined) rejoins the pool and is analysed again.

Every candidate in the pool is sent to Jev; the keyword baseline only decides the *order*
they are evaluated in (best first, so useful rows appear early in a long run). It never
decides *whether*.

The shortlist, its category counts and its coverage show pool candidates only: someone
scored and then submitted leaves the AI list at once (the keyword section's "Submitted"
filter shows who has gone to the client), and re-checking them is refused with 409.

The panel shows "Checked by Jev: X of Y", where Y is everyone not yet submitted right now
and X counts only candidates still among them. Anyone not yet checked — newly added,
returned to the pool, or left out by a configured cap — is counted in a notice with the
reason, and "Check them now" runs only those (everyone else is a cache hit).
