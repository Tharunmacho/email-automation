"use client";

/**
 * AI matching for one job order: the ranked shortlist, the explanation behind
 * each score, and the controls to re-run it.
 *
 * Preloaded, not requested. The server keeps every open order current in the
 * background (`app/matching/autorun.py`), and opening this panel catches up
 * whatever the sweep has not reached yet — so the list is simply there, and
 * the button is for a re-check, not the way in.
 *
 * Sits on the job-order detail screen above the keyword matches, which stay
 * exactly as they were — this is a second, explainable reading of the same
 * pool, not a replacement for it.
 *
 * The API owns every decision: who is in the pool, what is eligible, how the
 * score is weighted, which category a candidate lands in. This panel draws it,
 * and is careful about two things only — never presenting the score as a
 * probability of hire, and never letting an unverified requirement read as a
 * met one.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  BadgeCheck,
  CircleHelp,
  CircleX,
  FileCheck2,
  History,
  Loader2,
  RefreshCw,
  Sparkles,
  Square,
  X,
} from "lucide-react";

import Select from "@/components/ui/Select";
import StatTile from "@/components/ui/StatTile";
import { useModalFocus } from "@/components/ui/useModalFocus";
import { formatInt, initialsOf } from "@/lib/format";
import {
  cancelMatchRun,
  fetchMatchExplanation,
  fetchLatestMatchRun,
  fetchMatchingState,
  fetchMatchResults,
  fetchMatchRun,
  preloadMatching,
  refreshMatch,
  retryMatchRun,
  reviewMatch,
  startMatching,
  type MatchCategory,
  type MatchDocumentsBand,
  type MatchEligibility,
  type MatchExplanation,
  type MatchingState,
  type MatchResultFilters,
  type MatchResultRow,
  type MatchResultsPage,
  type MatchReviewStatus,
  type MatchRun,
} from "@/lib/api";
import type { JobOrderRecord } from "@/types";

type Tone = "good" | "warn" | "bad" | "info" | "quiet";

const CATEGORY_TONE: Record<MatchCategory, Tone> = {
  strong_match: "good",
  potential_match: "info",
  manual_review: "warn",
  low_match: "quiet",
  ineligible: "bad",
};

const CATEGORY_SHORT: Record<MatchCategory, string> = {
  strong_match: "Strong match",
  potential_match: "Potential match",
  manual_review: "Manual review",
  low_match: "Low match",
  ineligible: "Ineligible",
};

const ELIGIBILITY: Record<MatchEligibility, { label: string; tone: Tone }> = {
  met: { label: "Mandatory met", tone: "good" },
  no_mandatory: { label: "No mandatory items", tone: "quiet" },
  unverified: { label: "Unverified", tone: "warn" },
  not_met: { label: "Not met", tone: "bad" },
  not_evaluated: { label: "Not evaluated", tone: "quiet" },
};

const DOCUMENTS: Record<MatchDocumentsBand, { label: string; tone: Tone }> = {
  ready: { label: "Ready", tone: "good" },
  partial: { label: "Partial", tone: "warn" },
  not_ready: { label: "Not ready", tone: "bad" },
  unknown: { label: "Unknown", tone: "quiet" },
};

const REVIEW_LABEL: Record<MatchReviewStatus, string> = {
  pending: "Not reviewed",
  shortlisted: "Shortlisted",
  rejected: "Rejected",
  manual_review: "Manual review",
};

const COMPONENT_LABEL: Record<string, string> = {
  skills: "Skills & equipment",
  experience: "Relevant experience",
  qualifications: "Qualifications & licences",
  salary: "Salary compatibility",
  mobility: "Destination & availability",
  documents: "Document readiness",
};

const ACTIVE_RUN = new Set(["queued", "running"]);
const POLL_MS = 2000;
/** How often an idle panel looks for a run the background sweep started. */
const IDLE_CHECK_MS = 30000;
/** How often the list is re-read while a run is adding to it. */
const LIVE_RELOAD_MS = 15000;
const PAGE_SIZE = 15;

const SCORE_HINT =
  "Matching score (0–100): how well the evidence on file meets this order. Not a probability of being hired.";

function Pill({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={`aim-pill is-${tone}`}>{children}</span>;
}

function formatWhen(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function experienceOf(row: MatchResultRow): string {
  if (row.experience_years !== null && row.experience_years !== undefined) {
    return `${row.experience_years} yr${row.experience_years === 1 ? "" : "s"}`;
  }
  return row.experience_band || "Not stated";
}

interface Props {
  order: JobOrderRecord;
  onActivity?: (message: string, type?: "info" | "success" | "warn" | "error") => void;
  /** Opens the candidate's profile, when the host screen can. */
  onOpenCandidate?: (candidateId: string) => void;
}

export default function AiMatchPanel({ order, onActivity, onOpenCandidate }: Props) {
  const [state, setState] = useState<MatchingState | null>(null);
  const [page, setPage] = useState<MatchResultsPage | null>(null);
  const [run, setRun] = useState<MatchRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState<MatchResultFilters>({ page: 1, page_size: PAGE_SIZE });
  const [openId, setOpenId] = useState<string | null>(null);

  const orderId = order.id;
  const say = useCallback(
    (message: string, type: "info" | "success" | "warn" | "error" = "info") => onActivity?.(message, type),
    [onActivity],
  );

  const loadResults = useCallback(async () => {
    const next = await fetchMatchResults(orderId, filters);
    setPage(next);
  }, [filters, orderId]);

  const loadAll = useCallback(async () => {
    try {
      const [nextState, nextPage] = await Promise.all([fetchMatchingState(orderId), fetchMatchResults(orderId, filters)]);
      setState(nextState);
      setPage(nextPage);
      setRun(nextState.latest_run);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Matching could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [filters, orderId]);

  // `loading` starts true for the first read; a filter change re-reads in
  // place rather than flashing the skeleton over rows that are still valid.
  // The `active` guard drops a response that lands after the filters moved on.
  useEffect(() => {
    let active = true;
    Promise.all([fetchMatchingState(orderId), fetchMatchResults(orderId, filters)])
      .then(([nextState, nextPage]) => {
        if (!active) return;
        setState(nextState);
        setPage(nextPage);
        setRun(nextState.latest_run);
        setError("");
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Matching could not be loaded.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [filters, orderId]);

  // Poll the run while it is working: one small document every two seconds.
  // The shortlist is re-read every quarter minute while new scores are
  // landing — a first analysis of a large pool takes a while, and the best
  // keyword matches are scored first — and once more when the run ends.
  const runId = run?.id;
  const runActive = run ? ACTIVE_RUN.has(run.status) : false;
  useEffect(() => {
    if (!runId || !runActive) return;
    let alive = true;
    let shownDone = 0;
    let shownAt = Date.now();
    const timer = window.setInterval(async () => {
      try {
        const next = await fetchMatchRun(orderId, runId);
        if (!alive) return;
        setRun(next);
        const done = next.scored + next.failed;
        if (ACTIVE_RUN.has(next.status) && done > shownDone && Date.now() - shownAt >= LIVE_RELOAD_MS) {
          shownDone = done;
          shownAt = Date.now();
          await loadAll();
        }
        if (!ACTIVE_RUN.has(next.status)) {
          window.clearInterval(timer);
          await loadAll();
          // Announced only when somebody asked for it. A run that started on
          // its own just updates the list in place.
          if (!next.trigger || next.trigger === "user") {
            const failed = next.failed ? ` · ${formatInt(next.failed)} could not be evaluated` : "";
            say(`Matching ${next.status} for ${order.title}: ${formatInt(next.scored + next.cached)} evaluated${failed}.`,
              next.status === "completed" ? "success" : "warn");
          }
        }
      } catch {
        // A missed poll is not a failed run; the next tick tries again.
      }
    }, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [loadAll, order.title, orderId, runActive, runId, say]);

  // Opening the panel brings the order up to date: anything the background
  // sweep has not reached yet — a candidate added a minute ago, an order just
  // edited — is sent now, and only that. Once per order, and never while a
  // run is already going.
  const preloaded = useRef<string | null>(null);
  useEffect(() => {
    if (!state || state.spec.order_id !== orderId || preloaded.current === orderId) return;
    preloaded.current = orderId;
    if (!state.configured || !state.active) return;
    if (state.latest_run && ACTIVE_RUN.has(state.latest_run.status)) return;
    preloadMatching(orderId)
      .then(({ run: started }) => {
        if (started && preloaded.current === orderId) setRun(started);
      })
      .catch(() => {
        // Not fatal: the stored list is still on screen, and the coverage
        // notice offers to check whatever is missing.
      });
  }, [orderId, state]);

  // A panel left open notices a run the sweep started, follows it, and
  // re-reads the list when it ends. One small read every half minute.
  const runStatus = run?.status;
  const configured = Boolean(state?.configured);
  useEffect(() => {
    if (runActive || !configured) return;
    let alive = true;
    const timer = window.setInterval(async () => {
      try {
        const latest = await fetchLatestMatchRun(orderId);
        if (!alive || !latest || (latest.id === runId && latest.status === runStatus)) return;
        setRun(latest);
        if (!ACTIVE_RUN.has(latest.status)) await loadAll();
      } catch {
        // A missed check is not an error; the next tick tries again.
      }
    }, IDLE_CHECK_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [configured, loadAll, orderId, runActive, runId, runStatus]);

  const begin = async (mode: "match" | "rematch" = "match", force = false) => {
    setStarting(true);
    setError("");
    try {
      const next = await startMatching(orderId, { mode, force });
      setRun(next);
      say(`Started ${mode === "rematch" ? "a historical rematch" : "AI matching"} for ${order.title}.`, "info");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Matching could not be started.";
      setError(message);
      // A run already in progress is not an error to the person who pressed
      // the button twice — pick it up and show its progress instead.
      if (/already in progress/i.test(message)) void loadAll();
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    if (!run) return;
    try {
      setRun(await cancelMatchRun(orderId, run.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "The run could not be cancelled.");
    }
  };

  const retry = async () => {
    if (!run) return;
    setStarting(true);
    try {
      setRun(await retryMatchRun(orderId, run.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "The run could not be retried.");
    } finally {
      setStarting(false);
    }
  };

  const setFilter = <K extends keyof MatchResultFilters>(key: K, value: MatchResultFilters[K] | "") =>
    setFilters((prev) => ({ ...prev, [key]: value === "" ? undefined : value, page: 1 }));

  const counts = state?.counts;
  const coverage = state?.coverage;
  const evaluated = counts ? Object.values(counts).reduce((sum, value) => sum + value, 0) : 0;
  // Measured over what is actually being sent: results that were already
  // current are counted as cached from the start and are not "progress".
  const toCheck = run ? Math.max(0, run.considered - run.cached) : 0;
  const checked = run ? run.scored + run.failed : 0;
  const progress = toCheck ? Math.min(100, Math.round((checked / toCheck) * 100)) : 0;
  const pageCount = page ? Math.max(1, Math.ceil(page.total / (page.page_size || PAGE_SIZE))) : 1;
  const activeFilters = ["category", "eligibility", "documents", "review", "skill", "min_experience"]
    .filter((key) => filters[key as keyof MatchResultFilters] !== undefined).length;

  const skillOptions = useMemo(
    () => [{ value: "", label: "Any matched skill" }, ...(order.skills || []).map((skill) => ({ value: skill, label: skill }))],
    [order.skills],
  );

  const onReviewed = useCallback(() => {
    void loadResults();
  }, [loadResults]);

  return (
    <section className="aim-panel" aria-labelledby="aim-title">
      <div className="jod-section-head">
        <h2 className="jod-section-title" id="aim-title">
          <Sparkles size={19} />
          AI candidate matching
        </h2>
        <div className="aim-head-actions">
          {run && ACTIVE_RUN.has(run.status) ? (
            <button type="button" className="ds-ghost-btn" onClick={cancel} disabled={run.cancel_requested}>
              <Square size={14} /> {run.cancel_requested ? "Stopping…" : "Stop"}
            </button>
          ) : (
            <>
              {run && (run.status === "partial" || run.status === "failed" || run.status === "cancelled") && (
                <button type="button" className="ds-ghost-btn" onClick={retry} disabled={starting}>
                  <RefreshCw size={14} /> Retry remaining
                </button>
              )}
              <button
                type="button"
                className="ds-primary-btn"
                onClick={() => begin(state && !state.active ? "rematch" : "match")}
                disabled={starting || loading || !state?.configured}
                title={!state?.configured ? "JEV_API_KEY is not configured on the server" : undefined}
              >
                {starting ? <Loader2 size={15} className="icon-spin" /> : <Sparkles size={15} />}
                {state && !state.active ? "Run historical rematch" : evaluated ? "Re-run matching" : "Find Matching Candidates"}
              </button>
            </>
          )}
        </div>
      </div>

      {state && !state.configured && (
        <div className="aim-notice is-bad" role="alert">
          <AlertTriangle size={16} />
          <span>
            AI matching is not configured: <code>JEV_API_KEY</code> is not set on the server. No other provider is
            used in its place.
          </span>
        </div>
      )}
      {state && state.configured && !state.active && (
        <div className="aim-notice is-warn">
          <History size={16} />
          <span>{state.inactive_reason}</span>
        </div>
      )}
      {error && (
        <div className="aim-notice is-bad" role="alert">
          <CircleX size={16} />
          <span>{error}</span>
        </div>
      )}

      {run && ACTIVE_RUN.has(run.status) && (
        <div className="aim-progress" role="status" aria-live="polite">
          <div className="aim-progress-top">
            <span>
              <Loader2 size={14} className="icon-spin" />
              {run.status === "queued"
                ? "Queued…"
                : run.considered
                  ? `Analysing ${formatInt(checked)} of ${formatInt(toCheck)} new or changed candidate${toCheck === 1 ? "" : "s"}`
                  : "Retrieving candidates…"}
            </span>
            <span className="aim-quiet">
              {`${formatInt(run.retrieved)} not yet submitted · ${formatInt(run.cached)} already up to date`}
              {run.skipped > 0 && ` · ${formatInt(run.skipped)} left out by the configured cap`}
              {run.trigger && run.trigger !== "user" && " · started automatically"}
            </span>
          </div>
          <div className="ds-meter-track">
            <div className="ds-meter-fill" style={{ width: `${progress}%` }} />
          </div>
        </div>
      )}
      {run && !ACTIVE_RUN.has(run.status) && run.error && (
        <div className="aim-notice is-warn">
          <AlertTriangle size={16} />
          <span>
            Last run {run.status}: {run.error}
          </span>
        </div>
      )}

      {/* Nobody in the pool is skipped silently: anyone Jev has not read is
          counted here, with the reason and a way to check them. */}
      {coverage && coverage.not_evaluated > 0 && !runActive && evaluated > 0 && (
        <div className="aim-notice is-warn">
          <CircleHelp size={16} />
          <span>
            {formatInt(coverage.not_evaluated)} of {formatInt(coverage.pool)} candidate
            {coverage.pool === 1 ? "" : "s"} not yet submitted {coverage.not_evaluated === 1 ? "has" : "have"} not
            been checked by Jev
            {coverage.skipped_by_cap > 0
              ? ` — the last run was capped at ${formatInt(coverage.cap ?? 0)} by JEV_MAX_CANDIDATES_PER_RUN.`
              : " — they were added, or returned to the pool, after the last run."}
            {coverage.failed > 0 && ` ${formatInt(coverage.failed)} more could not be evaluated.`}
          </span>
          {state?.configured && coverage.skipped_by_cap === 0 && (
            <button type="button" className="ds-ghost-btn aim-notice-action" disabled={starting}
              onClick={() => begin(state.active ? "match" : "rematch")}>
              <Sparkles size={14} /> Check them now
            </button>
          )}
        </div>
      )}

      {counts && evaluated > 0 && (
        <div className="stat-tiles">
          <StatTile tone="green" icon={BadgeCheck} label="Strong matches" value={formatInt(counts.strong_match)}
            note={`${formatInt(counts.potential_match)} potential`} />
          <StatTile tone="yellow" icon={CircleHelp} label="Need manual review" value={formatInt(counts.manual_review)}
            note="A mandatory item could not be verified" />
          <StatTile tone="red" icon={CircleX} label="Ineligible" value={formatInt(counts.ineligible)}
            note="Verified mandatory requirement not met" />
          <StatTile tone={coverage && coverage.not_evaluated > 0 ? "yellow" : "slate"} icon={FileCheck2}
            label="Checked by Jev"
            value={coverage ? `${formatInt(coverage.evaluated)} of ${formatInt(coverage.pool)}` : formatInt(evaluated)}
            note={coverage && coverage.not_evaluated === 0
              ? "Everyone not yet submitted"
              : run ? `Last run ${formatWhen(run.finished_at || run.created_at)} · $${(run.cost_usd || 0).toFixed(4)}` : "—"} />
        </div>
      )}

      {page && page.unfiltered_total > 0 && (
        <div className="ds-filterbar" aria-label="AI match filters">
          <span className="ds-filterbar-label">Filters{activeFilters ? ` (${activeFilters})` : ""}</span>
          <Select size="sm" ariaLabel="Match category" value={filters.category ?? ""}
            onChange={(value) => setFilter("category", value as MatchCategory)}
            options={[{ value: "", label: "All categories" },
              ...(Object.keys(CATEGORY_SHORT) as MatchCategory[]).map((key) => ({ value: key, label: CATEGORY_SHORT[key] }))]} />
          <Select size="sm" ariaLabel="Mandatory eligibility" value={filters.eligibility ?? ""}
            onChange={(value) => setFilter("eligibility", value as MatchEligibility)}
            options={[{ value: "", label: "Any eligibility" },
              ...(["met", "unverified", "not_met", "no_mandatory"] as MatchEligibility[]).map((key) => ({ value: key, label: ELIGIBILITY[key].label }))]} />
          <Select size="sm" ariaLabel="Document readiness" value={filters.documents ?? ""}
            onChange={(value) => setFilter("documents", value as MatchDocumentsBand)}
            options={[{ value: "", label: "Any documents" },
              ...(Object.keys(DOCUMENTS) as MatchDocumentsBand[]).map((key) => ({ value: key, label: `Documents: ${DOCUMENTS[key].label}` }))]} />
          <Select size="sm" ariaLabel="Matched skill" value={filters.skill ?? ""}
            onChange={(value) => setFilter("skill", value)} options={skillOptions} />
          <Select size="sm" ariaLabel="Minimum experience" value={filters.min_experience !== undefined ? String(filters.min_experience) : ""}
            onChange={(value) => setFilter("min_experience", value === "" ? "" : Number(value))}
            options={[{ value: "", label: "Any experience" }, { value: "1", label: "1+ years" },
              { value: "3", label: "3+ years" }, { value: "5", label: "5+ years" }, { value: "8", label: "8+ years" }]} />
          <Select size="sm" ariaLabel="Recruiter review" value={filters.review ?? ""}
            onChange={(value) => setFilter("review", value as MatchReviewStatus)}
            options={[{ value: "", label: "Any review status" },
              ...(Object.keys(REVIEW_LABEL) as MatchReviewStatus[]).map((key) => ({ value: key, label: REVIEW_LABEL[key] }))]} />
          {activeFilters > 0 && (
            <button type="button" className="ds-filter-clear" onClick={() => setFilters({ page: 1, page_size: PAGE_SIZE })}>
              <X size={13} /> Clear
            </button>
          )}
        </div>
      )}

      {loading ? (
        <div className="aim-skeleton" role="status" aria-label="Loading AI matches">
          {[0, 1, 2].map((item) => <div key={item} className="dash-skel" />)}
        </div>
      ) : !page || page.unfiltered_total === 0 ? (
        <div className="ds-empty-state">
          <Sparkles size={28} />
          <h3>{run && ACTIVE_RUN.has(run.status) ? "Analysing candidates…" : "No AI matches yet"}</h3>
          <p>
            {!state?.configured
              ? "Configure JEV_API_KEY on the server to enable AI matching."
              : !state.active
                ? "Closed and filled orders are not analysed automatically. Run a historical rematch to analyse this one."
                : "Open job orders are analysed automatically: every candidate not yet submitted is read against this order's requirements and ranked, with the reasons for every score. The list appears here as it is ready."}
          </p>
        </div>
      ) : page.items.length === 0 ? (
        <div className="ds-empty-state">
          <CircleHelp size={28} />
          <h3>No matches for these filters</h3>
          <button type="button" className="ds-ghost-btn" onClick={() => setFilters({ page: 1, page_size: PAGE_SIZE })}>Clear filters</button>
        </div>
      ) : (
        <>
          <div className="ds-table-wrap is-ruled">
            <table className="ds-table is-ruled aim-table">
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>Experience</th>
                  <th title={SCORE_HINT}>Matching score</th>
                  <th>Category</th>
                  <th>Matched skills</th>
                  <th>Missing / unverified</th>
                  <th>Mandatory</th>
                  <th>Documents</th>
                  <th>Review</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((row) => {
                  const gaps = [...row.failed_requirements, ...row.unverified_requirements,
                    ...row.skills_without_evidence.map((skill) => `${skill} (no evidence)`)];
                  return (
                    <tr key={row.candidate_id} className="is-clickable" tabIndex={0}
                      onClick={() => setOpenId(row.candidate_id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setOpenId(row.candidate_id);
                        }
                      }}
                      aria-label={`Explain the match for ${row.name}`}>
                      <td data-label="Candidate">
                        <span className="ds-who">
                          <span className="ds-avatar" aria-hidden="true">{initialsOf(row.name)}</span>
                          <span className="ds-who-text">
                            <strong title={row.name}>{row.name}</strong>
                            <small>{row.designation || "—"}</small>
                            <small className="crm-record-id">{row.candidate_code || row.candidate_id}</small>
                          </span>
                        </span>
                      </td>
                      <td data-label="Experience">{experienceOf(row)}</td>
                      <td data-label="Matching score" title={SCORE_HINT}>
                        {row.status === "failed" ? (
                          <Pill tone="bad">Not evaluated</Pill>
                        ) : (
                          <span className={`aim-score ${row.category === "ineligible" ? "is-struck" : ""}`}>
                            {row.score?.toFixed(0)}
                          </span>
                        )}
                        {(row.stale.order_changed || row.stale.candidate_changed) && (
                          <small className="aim-quiet aim-stale">
                            {row.stale.order_changed ? "Order changed since" : "Profile changed since"}
                          </small>
                        )}
                      </td>
                      <td data-label="Category">
                        {row.category ? <Pill tone={CATEGORY_TONE[row.category]}>{CATEGORY_SHORT[row.category]}</Pill> : "—"}
                        {row.failure && row.status === "scored" && (
                          <small className="aim-quiet">Last re-check failed</small>
                        )}
                      </td>
                      <td data-label="Matched skills">
                        {row.matched_skills.length ? (
                          <span className="aim-chips">
                            {row.matched_skills.slice(0, 3).map((skill) => <span key={skill} className="ds-chip">{skill}</span>)}
                            {row.matched_skills.length > 3 && <span className="aim-quiet">+{row.matched_skills.length - 3}</span>}
                          </span>
                        ) : <span className="aim-quiet">None evidenced</span>}
                      </td>
                      <td data-label="Missing / unverified">
                        {gaps.length ? (
                          <span className="aim-gaps" title={gaps.join("\n")}>
                            {gaps[0]}{gaps.length > 1 && <span className="aim-quiet"> +{gaps.length - 1}</span>}
                          </span>
                        ) : <span className="aim-quiet">—</span>}
                      </td>
                      <td data-label="Mandatory"><Pill tone={ELIGIBILITY[row.eligibility].tone}>{ELIGIBILITY[row.eligibility].label}</Pill></td>
                      <td data-label="Documents"><Pill tone={DOCUMENTS[row.documents.band].tone}>{DOCUMENTS[row.documents.band].label}</Pill></td>
                      <td data-label="Review">
                        <span className={`aim-review is-${row.review.status}`}>{REVIEW_LABEL[row.review.status]}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="ds-panel-foot">
            <span>
              {formatInt(page.total)} of {formatInt(page.unfiltered_total)} evaluated · ranked by category, then score
            </span>
            <div className="ds-pagination" aria-label="AI match pages">
              <button type="button" disabled={(filters.page ?? 1) <= 1}
                onClick={() => setFilters((prev) => ({ ...prev, page: Math.max(1, (prev.page ?? 1) - 1) }))}
                aria-label="Previous page">‹</button>
              <span>Page {filters.page ?? 1} of {pageCount}</span>
              <button type="button" disabled={(filters.page ?? 1) >= pageCount}
                onClick={() => setFilters((prev) => ({ ...prev, page: Math.min(pageCount, (prev.page ?? 1) + 1) }))}
                aria-label="Next page">›</button>
            </div>
          </div>
        </>
      )}

      {openId && (
        <MatchExplanationDialog
          orderId={orderId}
          candidateId={openId}
          onClose={() => setOpenId(null)}
          onReviewed={onReviewed}
          onRefreshStarted={(next) => { setRun(next); setOpenId(null); }}
          onOpenCandidate={onOpenCandidate}
          say={say}
        />
      )}
    </section>
  );
}

// --------------------------------------------------------------------------- //
//  Why this score
// --------------------------------------------------------------------------- //
interface DialogProps {
  orderId: string;
  candidateId: string;
  onClose: () => void;
  onReviewed: () => void;
  onRefreshStarted: (run: MatchRun) => void;
  onOpenCandidate?: (candidateId: string) => void;
  say: (message: string, type?: "info" | "success" | "warn" | "error") => void;
}

function MatchExplanationDialog({
  orderId, candidateId, onClose, onReviewed, onRefreshStarted, onOpenCandidate, say,
}: DialogProps) {
  const dialogRef = useModalFocus<HTMLDivElement>(true, onClose);
  const [detail, setDetail] = useState<MatchExplanation | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const loaded = useRef(false);

  useEffect(() => {
    let alive = true;
    fetchMatchExplanation(orderId, candidateId)
      .then((next) => {
        if (!alive) return;
        setDetail(next);
        if (!loaded.current) {
          setNote(next.review.note || "");
          loaded.current = true;
        }
      })
      .catch((err) => alive && setError(err instanceof Error ? err.message : "The explanation could not be loaded."));
    return () => {
      alive = false;
    };
  }, [candidateId, orderId]);

  const decide = async (status: MatchReviewStatus) => {
    setBusy(true);
    try {
      const review = await reviewMatch(orderId, candidateId, status, note.trim());
      setDetail((prev) => (prev ? { ...prev, review: { ...prev.review, ...review } } : prev));
      say(`${detail?.name ?? "Candidate"} marked ${REVIEW_LABEL[status].toLowerCase()}.`, "success");
      onReviewed();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The decision could not be saved.");
    } finally {
      setBusy(false);
    }
  };

  const recheck = async () => {
    setBusy(true);
    try {
      onRefreshStarted(await refreshMatch(orderId, candidateId, true));
    } catch (err) {
      setError(err instanceof Error ? err.message : "The candidate could not be re-checked.");
      setBusy(false);
    }
  };

  return (
    <div className="cm-overlay active" onClick={() => !busy && onClose()}>
      <div ref={dialogRef} className="cm-dialog sh-modal aim-dialog" role="dialog" aria-modal="true"
        aria-labelledby="aim-dialog-title" tabIndex={-1} onClick={(event) => event.stopPropagation()}>
        <div className="sh-modal-head">
          <div className="aim-dialog-who">
            <span className="ds-avatar" aria-hidden="true">{initialsOf(detail?.name)}</span>
            <span>
              <h2 id="aim-dialog-title" className="sh-modal-title">{detail?.name ?? "Loading…"}</h2>
              {detail && (
                <span className="crm-record-id">
                  {detail.candidate_code || detail.candidate_id} · {detail.designation || "No designation on file"}
                </span>
              )}
            </span>
          </div>
          <button type="button" className="sh-modal-close" onClick={onClose} disabled={busy} aria-label="Close explanation">
            <X size={18} />
          </button>
        </div>

        <div className="aim-dialog-body">
          {error && <div className="aim-notice is-bad" role="alert"><CircleX size={16} /><span>{error}</span></div>}
          {!detail && !error && <div className="aim-skeleton"><div className="dash-skel" /><div className="dash-skel" /></div>}

          {detail && (
            <>
              <section className="aim-verdict">
                <div className="aim-verdict-score">
                  <span className={`aim-score is-large ${detail.category === "ineligible" ? "is-struck" : ""}`}>
                    {detail.score !== null ? detail.score.toFixed(0) : "—"}
                  </span>
                  <span className="aim-quiet">/ 100</span>
                </div>
                <div>
                  {detail.category && <Pill tone={CATEGORY_TONE[detail.category]}>{detail.category_label}</Pill>}
                  <p className="aim-quiet aim-score-label">{detail.score_label}</p>
                  {detail.failure && (
                    <p className="aim-inline-warn">
                      <AlertTriangle size={13} /> The last evaluation attempt failed ({detail.failure.message}).
                      {detail.status === "scored" ? " The score shown is from the previous successful evaluation." : ""}
                    </p>
                  )}
                  {(detail.stale.order_changed || detail.stale.candidate_changed) && (
                    <p className="aim-inline-warn">
                      <History size={13} /> {detail.stale.order_changed ? "The job order" : "This candidate's profile"} changed
                      after this evaluation. Re-check to update it.
                    </p>
                  )}
                </div>
              </section>

              {detail.reasons.length > 0 && (
                <ul className="aim-reasons">
                  {detail.reasons.map((reason) => <li key={reason}>{reason}</li>)}
                </ul>
              )}

              <section>
                <h3 className="aim-h3">Mandatory requirements</h3>
                {detail.requirements.length === 0 ? (
                  <p className="aim-quiet">This order states no mandatory requirement.</p>
                ) : (
                  <ul className="aim-list">
                    {detail.requirements.map((item) => (
                      <li key={item.key}>
                        <Pill tone={item.status === "satisfied" ? "good" : item.status === "not_satisfied" ? "bad" : "warn"}>
                          {item.status === "satisfied" ? "Verified" : item.status === "not_satisfied" ? "Not met" : "Unverified"}
                        </Pill>
                        <span>{item.label}</span>
                        <small className="aim-quiet">from {item.source}</small>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section>
                <h3 className="aim-h3">How the score was built</h3>
                <table className="aim-components">
                  <tbody>
                    {detail.components.map((component) => (
                      <tr key={component.key}>
                        <th scope="row">{COMPONENT_LABEL[component.key] ?? component.key}</th>
                        <td className="aim-quiet">{Math.round(component.weight * 100)}%</td>
                        <td>
                          {!component.applicable ? (
                            <span className="aim-quiet">Not asked by this order</span>
                          ) : component.unknown ? (
                            <Pill tone="warn">No evidence on file</Pill>
                          ) : (
                            <span className="aim-bar" aria-label={`${Math.round((component.value ?? 0) * 100)} of 100`}>
                              <span style={{ width: `${Math.round((component.value ?? 0) * 100)}%` }} />
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>

              <section>
                <h3 className="aim-h3">Evidence from the candidate record</h3>
                {detail.evidence.length === 0 ? (
                  <p className="aim-quiet">No line of the record could be matched to this order&apos;s requirements.</p>
                ) : (
                  <ul className="aim-evidence">
                    {detail.evidence.map((item) => (
                      <li key={`${item.requirement}-${item.field}`}>
                        <strong>{item.requirement}</strong>
                        <q>{item.excerpt}</q>
                        <small className="aim-quiet">{item.field}</small>
                      </li>
                    ))}
                  </ul>
                )}
                {detail.skills_without_evidence.length > 0 && (
                  <p className="aim-quiet">No evidence on file for: {detail.skills_without_evidence.join(", ")}</p>
                )}
              </section>

              {detail.baseline && (
                <p className="aim-quiet aim-baseline">
                  Keyword baseline for comparison: {detail.baseline.score.toFixed(0)}/100
                  {detail.baseline.experience_met === false ? " · below the stated experience floor" : ""}
                </p>
              )}

              <section className="aim-decision">
                <h3 className="aim-h3">Recruiter decision</h3>
                <textarea className="aim-note" value={note} maxLength={1000} placeholder="Note (optional)"
                  onChange={(event) => setNote(event.target.value)} aria-label="Decision note" />
                <div className="aim-decision-actions">
                  <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => decide("shortlisted")}>
                    <BadgeCheck size={15} /> Shortlist
                  </button>
                  <button type="button" className="ds-ghost-btn" disabled={busy} onClick={() => decide("manual_review")}>
                    <CircleHelp size={15} /> Manual review
                  </button>
                  <button type="button" className="ds-ghost-btn aim-danger" disabled={busy} onClick={() => decide("rejected")}>
                    <CircleX size={15} /> Reject
                  </button>
                  <span className="aim-quiet">
                    Current: {REVIEW_LABEL[detail.review.status]}
                    {detail.review.by_name ? ` · ${detail.review.by_name}, ${formatWhen(detail.review.at)}` : ""}
                  </span>
                </div>
              </section>

              <p className="aim-quiet aim-meta">
                Evaluated {formatWhen(detail.evaluated_at)} · model {detail.versions.model ?? "—"} · schema{" "}
                {detail.versions.schema ?? "—"} · scoring {detail.versions.scoring ?? "—"}
              </p>
            </>
          )}
        </div>

        <div className="modal-footer">
          {onOpenCandidate && (
            <button type="button" className="modal-cancel-btn" onClick={() => onOpenCandidate(candidateId)}>
              Open profile
            </button>
          )}
          <button type="button" className="modal-cancel-btn" onClick={recheck} disabled={busy}>
            <RefreshCw size={14} /> Re-check with Jev
          </button>
          <button type="button" className="modal-cancel-btn" onClick={onClose} disabled={busy}>Close</button>
        </div>
      </div>
    </div>
  );
}
