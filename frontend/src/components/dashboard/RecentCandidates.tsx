"use client";

import { ArrowRight, CheckCircle2, CircleAlert, Sparkles, UsersRound } from "lucide-react";

import { recentCandidates } from "@/lib/dashboardMetrics";
import { candidateNameOf, initialsOf, timeAgo } from "@/lib/format";
import { REVIEW_CONFIDENCE_THRESHOLD, type CandidateRecord } from "@/lib/api";

interface RecentCandidatesProps {
  candidates: CandidateRecord[];
  onOpenCandidate?: (candidate: CandidateRecord) => void;
  /** "View all" in the card's header. Omitted, the header carries no control. */
  onViewAll?: () => void;
}

function statusOf(candidate: CandidateRecord): { label: string; tone: string } {
  if (candidate.status === "verified") return { label: "Verified", tone: "is-verified" };
  if ((candidate.profile?.confidence ?? 1) < REVIEW_CONFIDENCE_THRESHOLD) {
    return { label: "Review", tone: "is-pending" };
  }
  return { label: "Active", tone: "is-info" };
}

/** Years of experience, where the parser found any. */
function experienceOf(candidate: CandidateRecord): string {
  const years = candidate.profile?.total_experience_years;
  if (typeof years !== "number" || years <= 0) return "—";
  return years === 1 ? "1 yr" : `${Number.isInteger(years) ? years : years.toFixed(1)} yrs`;
}

/**
 * The last handful of profiles to arrive, newest first — a real table rather
 * than a stack of rows.
 *
 * Columns, not free-form cells: a recruiter scanning this is comparing the same
 * field down a column (who is newest, whose confidence is low), and that
 * comparison only works when the fields line up. The confidence figure carries
 * the sign of its own verdict — green where the parser was sure, rose where it
 * was not — so the column that decides whether a profile needs a human can be
 * read without cross-referencing the status pill beside it.
 */
export default function RecentCandidates({
  candidates,
  onOpenCandidate,
  onViewAll,
}: RecentCandidatesProps) {
  const recent = recentCandidates(candidates, 8);

  return (
    <section className="ov-chart-card">
      <div className="ov-chart-card-head">
        <div>
          <h2 className="ov-chart-card-title">Recent Candidates</h2>
          <p className="ov-chart-card-sub">
            <span className="ov-kpi-card-caption">The newest profiles the parser has produced</span>
          </p>
        </div>
        {onViewAll && (
          <button type="button" className="ov-card-link" onClick={onViewAll}>
            View all <ArrowRight size={13} />
          </button>
        )}
      </div>

      {recent.length === 0 ? (
        <div className="db-empty" style={{ border: "none", padding: "2rem 0 2.5rem" }}>
          <UsersRound size={24} strokeWidth={1.5} />
          <span className="db-empty-title">No candidates yet</span>
          <span className="db-empty-sub">Run a sync and the newest arrivals show up here.</span>
        </div>
      ) : (
        <div className="rc-table-wrap">
          <table className="rc-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Role</th>
                <th className="rc-col-num">Experience</th>
                <th className="rc-col-num">Confidence</th>
                <th className="rc-col-status">Status</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((candidate) => {
                const name = candidateNameOf(candidate);
                const status = statusOf(candidate);
                const role =
                  candidate.profile?.current_designation ||
                  candidate.profile?.work_experience?.[0]?.designation ||
                  "—";
                const confidence = candidate.profile?.confidence;
                const sure =
                  typeof confidence === "number" && confidence >= REVIEW_CONFIDENCE_THRESHOLD;

                return (
                  <tr
                    key={candidate.id}
                    className={onOpenCandidate ? "is-clickable" : undefined}
                    onClick={() => onOpenCandidate?.(candidate)}
                    tabIndex={onOpenCandidate ? 0 : undefined}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onOpenCandidate?.(candidate);
                      }
                    }}
                  >
                    <td>
                      <span className="rc-name-cell">
                        <span className="rc-avatar" aria-hidden="true">
                          {initialsOf(name)}
                        </span>
                        <span className="rc-name-text">
                          <span className="rc-name">{name}</span>
                          <span className="rc-sub">{timeAgo(candidate.created_at)}</span>
                        </span>
                      </span>
                    </td>
                    <td className="rc-role" title={role}>
                      {role}
                    </td>
                    <td className="rc-col-num">{experienceOf(candidate)}</td>
                    <td className="rc-col-num">
                      {typeof confidence === "number" ? (
                        <span className={`rc-score ${sure ? "is-up" : "is-down"}`}>
                          {sure ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}
                          {(confidence * 100).toFixed(0)}%
                        </span>
                      ) : (
                        <span className="rc-score is-none">
                          <Sparkles size={13} />
                          n/a
                        </span>
                      )}
                    </td>
                    <td className="rc-col-status">
                      <span className={`db-pill ${status.tone}`}>{status.label}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
