"use client";

/**
 * Candidate matching for one job order: the ranked shortlist, and the
 * explanation behind each score.
 *
 * The server ranks everyone not yet submitted against the order, locally and
 * on request (`app/matching/matcher.py`) — there is nothing to start and no
 * progress to wait for. The panel loads the ranking like any other list;
 * "Refresh" re-reads the candidate pool for anyone added in the last minute.
 *
 * Sits on the job-order detail screen above the keyword matches, which stay
 * exactly as they were. The API owns every decision — who is in the pool,
 * what is eligible, how the score is weighted, which category a candidate
 * lands in. This panel draws it, and is careful about two things only: never
 * presenting the score as a probability of hire, and never letting an
 * unverified requirement read as a met one.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BadgeCheck, CircleHelp, CircleX, History, Loader2, RefreshCw, Target, Users, X } from "lucide-react";

import Select from "@/components/ui/Select";
import StatTile from "@/components/ui/StatTile";
import { useModalFocus } from "@/components/ui/useModalFocus";
import { formatInt, initialsOf } from "@/lib/format";
import {
  fetchMatchExplanation,
  fetchMatchingState,
  fetchMatchResults,
  reviewMatch,
  type MatchCategory,
  type MatchDocumentsBand,
  type MatchEligibility,
  type MatchExplanation,
  type MatchingState,
  type MatchResultFilters,
  type MatchResultRow,
  type MatchResultsPage,
  type MatchReviewStatus,
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
};

const DOCUMENTS: Record<MatchDocumentsBand, { label: string; tone: Tone }> = {
  ready: { label: "Ready", tone: "good" },
  partial: { label: "Partial", tone: "warn" },
  not_ready: { label: "Not ready", tone: "bad" },
};

const REVIEW_LABEL: Record<MatchReviewStatus, string> = {
  pending: "Not reviewed",
  shortlisted: "Shortlisted",
  rejected: "Rejected",
  manual_review: "Manual review",
};

const COMPONENT_LABEL: Record<string, string> = {
  role: "Job title / trade",
  skills: "Skills",
  experience: "Experience",
  relevance: "CV relevance",
  qualifications: "Licences & certifications",
  destination: "Destination",
  documents: "Documents",
};

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

export default function CandidateMatchPanel({ order, onActivity, onOpenCandidate }: Props) {
  const [state, setState] = useState<MatchingState | null>(null);
  const [page, setPage] = useState<MatchResultsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState<MatchResultFilters>({ page: 1, page_size: PAGE_SIZE });
  const [openId, setOpenId] = useState<string | null>(null);

  const orderId = order.id;
  const say = useCallback(
    (message: string, type: "info" | "success" | "warn" | "error" = "info") => onActivity?.(message, type),
    [onActivity],
  );

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

  const loadResults = useCallback(async () => {
    setPage(await fetchMatchResults(orderId, filters));
  }, [filters, orderId]);

  // The pool is re-read at most once a minute on its own; this skips the wait.
  const refresh = async () => {
    setRefreshing(true);
    try {
      const nextState = await fetchMatchingState(orderId, { fresh: true });
      setState(nextState);
      setPage(await fetchMatchResults(orderId, filters));
      setError("");
      say(`Re-ranked ${formatInt(nextState.pool)} candidates for ${order.title}.`, "success");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Matching could not be refreshed.");
    } finally {
      setRefreshing(false);
    }
  };

  const setFilter = <K extends keyof MatchResultFilters>(key: K, value: MatchResultFilters[K] | "") =>
    setFilters((prev) => ({ ...prev, [key]: value === "" ? undefined : value, page: 1 }));

  const counts = state?.counts;
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
          <Target size={19} />
          Candidate matching
        </h2>
        <div className="aim-head-actions">
          <button type="button" className="ds-ghost-btn" onClick={refresh} disabled={refreshing || loading}
            title="Re-read the candidate pool now">
            <RefreshCw size={14} className={refreshing ? "icon-spin" : undefined} /> Refresh
          </button>
        </div>
      </div>

      {state && !state.active && state.inactive_reason && (
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

      {counts && state && state.pool > 0 && (
        <div className="stat-tiles">
          <StatTile tone="green" icon={BadgeCheck} label="Strong matches" value={formatInt(counts.strong_match)}
            note={`${formatInt(counts.potential_match)} potential`} />
          <StatTile tone="yellow" icon={CircleHelp} label="Need manual review" value={formatInt(counts.manual_review)}
            note="A mandatory item is not on record" />
          <StatTile tone="red" icon={CircleX} label="Ineligible" value={formatInt(counts.ineligible)}
            note="Mandatory requirement not met" />
          <StatTile tone="slate" icon={Users} label="Ranked" value={formatInt(state.pool)}
            note="Everyone not yet submitted" />
        </div>
      )}

      {page && page.unfiltered_total > 0 && (
        <div className="ds-filterbar" aria-label="Match filters">
          <span className="ds-filterbar-label">Filters{activeFilters ? ` (${activeFilters})` : ""}</span>
          <Select size="sm" ariaLabel="Match category" value={filters.category ?? ""}
            onChange={(value) => setFilter("category", value as MatchCategory)}
            options={[{ value: "", label: "All categories" },
              ...(Object.keys(CATEGORY_SHORT) as MatchCategory[]).map((key) => ({ value: key, label: CATEGORY_SHORT[key] }))]} />
          <Select size="sm" ariaLabel="Mandatory eligibility" value={filters.eligibility ?? ""}
            onChange={(value) => setFilter("eligibility", value as MatchEligibility)}
            options={[{ value: "", label: "Any eligibility" },
              ...(Object.keys(ELIGIBILITY) as MatchEligibility[]).map((key) => ({ value: key, label: ELIGIBILITY[key].label }))]} />
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
        <div className="aim-skeleton" role="status" aria-label="Loading matches">
          {[0, 1, 2].map((item) => <div key={item} className="dash-skel" />)}
        </div>
      ) : !page || page.unfiltered_total === 0 ? (
        <div className="ds-empty-state">
          <Users size={28} />
          <h3>No candidates to rank</h3>
          <p>Everyone in the candidate pool has already been submitted, or no candidates have been added yet.</p>
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
                    ...row.skills_without_evidence.map((skill) => `${skill} (not on record)`)];
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
                        <span className={`aim-score ${row.category === "ineligible" ? "is-struck" : ""}`}>
                          {row.score.toFixed(0)}
                        </span>
                      </td>
                      <td data-label="Category">
                        <Pill tone={CATEGORY_TONE[row.category]}>{CATEGORY_SHORT[row.category]}</Pill>
                      </td>
                      <td data-label="Matched skills">
                        {row.matched_skills.length || row.related_skills.length ? (
                          <span className="aim-chips">
                            {row.matched_skills.slice(0, 3).map((skill) => <span key={skill} className="ds-chip">{skill}</span>)}
                            {row.matched_skills.length === 0 && row.related_skills.slice(0, 2).map((skill) => (
                              <span key={skill} className="ds-chip" title="Related evidence, half credit">{skill} ~</span>
                            ))}
                            {row.matched_skills.length > 3 && <span className="aim-quiet">+{row.matched_skills.length - 3}</span>}
                          </span>
                        ) : <span className="aim-quiet">None on record</span>}
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
              {formatInt(page.total)} of {formatInt(page.unfiltered_total)} candidates · ranked by category, then score
            </span>
            <div className="ds-pagination" aria-label="Match pages">
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
  onOpenCandidate?: (candidateId: string) => void;
  say: (message: string, type?: "info" | "success" | "warn" | "error") => void;
}

function MatchExplanationDialog({ orderId, candidateId, onClose, onReviewed, onOpenCandidate, say }: DialogProps) {
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
          {!detail && !error && (
            <div className="aim-skeleton"><Loader2 size={16} className="icon-spin" /><div className="dash-skel" /></div>
          )}

          {detail && (
            <>
              <section className="aim-verdict">
                <div className="aim-verdict-score">
                  <span className={`aim-score is-large ${detail.category === "ineligible" ? "is-struck" : ""}`}>
                    {detail.score.toFixed(0)}
                  </span>
                  <span className="aim-quiet">/ 100</span>
                </div>
                <div>
                  <Pill tone={CATEGORY_TONE[detail.category]}>{detail.category_label}</Pill>
                  <p className="aim-quiet aim-score-label">{detail.score_label}</p>
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
                          {item.status === "satisfied" ? "Met" : item.status === "not_satisfied" ? "Not met" : "Unverified"}
                        </Pill>
                        <span>{item.label}</span>
                        <small className="aim-quiet">{item.detail} · from {item.source}</small>
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
                        <th scope="row">
                          {COMPONENT_LABEL[component.key] ?? component.key}
                          <small className="aim-quiet aim-component-detail">{component.detail}</small>
                        </th>
                        <td className="aim-quiet">{component.applicable ? `${Math.round(component.weight * 100)}%` : "—"}</td>
                        <td>
                          {!component.applicable ? (
                            <span className="aim-quiet">Not asked by this order</span>
                          ) : component.unknown ? (
                            <Pill tone="warn">Not on record</Pill>
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
                  <p className="aim-quiet">Not on record: {detail.skills_without_evidence.join(", ")}</p>
                )}
              </section>

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

              <p className="aim-quiet aim-meta">Scoring {detail.scoring_version}</p>
            </>
          )}
        </div>

        <div className="modal-footer">
          {onOpenCandidate && (
            <button type="button" className="modal-cancel-btn" onClick={() => onOpenCandidate(candidateId)}>
              Open profile
            </button>
          )}
          <button type="button" className="modal-cancel-btn" onClick={onClose} disabled={busy}>Close</button>
        </div>
      </div>
    </div>
  );
}
