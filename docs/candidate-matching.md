# Candidate matching

A job order's **Candidate matching** panel ranks everyone not yet submitted against the
order's requirements, explains every score, and keeps the recruiter's shortlist / reject
decisions. It runs entirely on the CRM server: no AI model, no external API, no cost per
candidate, nothing to start and nothing to wait for. Nothing re-runs OCR and nothing writes
to a candidate record.

The score is a **matching score** (0–100): how well the evidence on file meets the order.
It is not a probability of being hired.

## How a candidate is scored

`app/matching/matcher.py`. Seven components, each 0–1, weighted and averaged over the ones
the order actually asks about:

| Component | Default weight | How it is read | Applies when |
|---|---|---|---|
| role | 30% | the order's trade in the candidate's titles (1.0), in their skills/history (0.75), a related trade in titles (0.6) or history (0.4); otherwise the role's words, weighted by how rare they are in the pool | always |
| skills | 30% | per skill: all its words on record — synonyms and typos included (1.0), half of them or the skill's trade (0.5), nothing (0) | the order lists skills |
| experience | 15% | years against the minimum; a band is a range; years in an unrelated trade are not counted | the order sets a minimum |
| relevance | 10% | **BM25** (the search-engine ranking function) of the order's words over the whole record, as a 0–1 coverage | always |
| qualifications | 10% | licences / certifications the order names, found on record and not expired | the order names any |
| destination | 5% | the job's country among the candidate's preferred destinations | the order states one |
| documents | 5% | passport on file and valid > 6 months, nothing outstanding | always |

`score = 100 × Σ(weight × value) / Σ(weight)` over applicable components. An applicable
component the record cannot answer scores 0 and is shown as "Not on record".

**Category**, in order (`app/matching/scoring.py`):

1. any mandatory requirement **not met** (years below the minimum, an expired licence) → `ineligible`
2. a mandatory requirement **not on record**, for a plausible candidate (score ≥ 50 or a title that fits) → `manual_review`
3. a title that fits on a record too thin to check skills (typical WhatsApp intake) → `manual_review`
4. score ≥ 75 → `strong_match`; ≥ 50 → `potential_match`; else `low_match`

The list sorts by category, then score, so no score lifts an ineligible candidate above an
eligible one. Mandatory requirements are only what the order states: `minExperience`,
`mandatorySkills`, `requiredLicences`, `requiredCertifications`, `workEligibility`, and an
age band (always "not on record" — no date of birth is stored). Skills in `skills` are
scored, never a gate.

Weights and thresholds are starting defaults. A job order can override them with
`matchingWeights` / `matchingThresholds`.

## Vocabulary: how different wording is made to meet

`app/matching/vocabulary.py`, applied identically to the order and the candidate:

| Layer | Example |
|---|---|
| light stemming | welders / welding / welder → `weld` |
| `EQUIVALENTS` — same thing, different words | GTAW, argon arc, tungsten inert gas → `tig`; light motor vehicle → `lmv`; fabrication / fabricator → one word |
| `TRADES` — ~45 trades, their names and related trades | Fabricator, Boilermaker, Plater are the *fabricator* trade, related to *welder*, so a fabricator scores 0.6 on a welder order instead of 0 |
| typo tolerance (`matcher.PoolIndex.variants`) | "shutering" counts for "shuttering": words of 5+ letters match close spellings that occur in the pool (difflib ratio ≥ 0.85) |

A missing synonym or trade costs recall on that one term and shows up as "Not on record";
adding it is one line in `vocabulary.py`.

## Who is ranked

**Everyone except candidates already submitted** (`app/matching/pool.py::retrieval_query`):

| Excluded | Why |
|---|---|
| `recruitment_status` other than `available` | already submitted — on **any** order, this one included |
| `placement_locked` | placed |
| `duplicate_of` set | a second copy of a person already ranked under their original record |

Candidates entered without a CV are ranked (a fitting title on a thin record lands in
manual review). A candidate released back to `available` rejoins at the next pool read.

## Speed

The pool is read once and indexed (`matcher.pool_index`) — about 1.3 s per 5,000
candidates — then shared by every order for `INDEX_TTL_SECONDS` (60 s). Ranking one order
is one in-memory pass, about half a second for 5,000 candidates, and is cached until the
pool is re-read or the order changes. **Refresh** on the panel re-reads the pool at once.

## Routes

All under `require_page("job-orders")` (404 otherwise).

| Method | Path | |
|---|---|---|
| GET | `/job-orders/{id}/matching?fresh=` | active?, normalised spec, pool size, category counts; `fresh=true` re-reads the pool |
| GET | `/job-orders/{id}/matching/results` | ranked; filters `category, eligibility, documents, review, skill, min_experience, page, page_size` |
| GET | `/job-orders/{id}/matching/results/{candidate_id}` | the explanation: components with details, mandatory checks, reasons, quoted evidence |
| PUT | `/job-orders/{id}/matching/results/{candidate_id}/review` | `{"status": "shortlisted"\|"rejected"\|"manual_review"\|"pending", "note": ""}` |

Closed and filled orders are still ranked; the panel says the ranking is for reference.

## Storage

Only recruiter decisions are stored: `candidate_match_reviews`, unique
`(job_order_id, candidate_id)`, with the last 20 changes as history. They survive the order
being edited and the candidate being re-ranked.

## Measuring it

```bash
python -m app.matching.evaluation JO-1 JO-2
```

scores the ranking against recruiter decisions (shortlisted = relevant, rejected = not):
precision@10, recall@20, NDCG@10, and an upper bound on eligibility false positives. This
is what weights and thresholds should be tuned against once there are decisions to learn
from.

Tests: `python -m pytest tests/test_matching_*.py`.

## History

Matching was first built on Jev (TypeSafe System One), a paid external model; that version
is in git at commits `41a9d76`, `67e37c1` and `be5e6ee`. It was replaced by this local
matcher. The `JEV_*` settings are no longer read, and its collections
(`candidate_matches`, `candidate_match_runs`, `candidate_match_activity`) are left in the
database untouched.
