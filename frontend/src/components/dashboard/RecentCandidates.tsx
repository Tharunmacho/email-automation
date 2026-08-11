"use client";

import { UsersRound } from "lucide-react";

import { recentCandidates } from "@/lib/dashboardMetrics";
import { candidateNameOf, initialsOf, timeAgo } from "@/lib/format";
import { REVIEW_CONFIDENCE_THRESHOLD, type CandidateRecord } from "@/lib/api";

interface RecentCandidatesProps {
  candidates: CandidateRecord[];
  onOpenCandidate?: (candidate: CandidateRecord) => void;
}

function statusOf(candidate: CandidateRecord): { label: string; tone: string } {
  if (candidate.status === "verified") return { label: "Verified", tone: "is-verified" };
  if ((candidate.profile?.confidence ?? 1) < REVIEW_CONFIDENCE_THRESHOLD) {
    return { label: "Review", tone: "is-pending" };
  }
  return { label: "Active", tone: "is-info" };
}

/** The last handful of profiles to arrive, newest first. */
export default function RecentCandidates({ candidates, onOpenCandidate }: RecentCandidatesProps) {
  const recent = recentCandidates(candidates, 6);

  return (
    <section className="db-card">
      <header className="db-card-head">
        <div>
          <h3 className="db-card-title">Recent candidates</h3>
          <p className="db-card-sub">The newest profiles the parser has produced.</p>
        </div>
      </header>

      <div className="db-card-body" style={{ paddingTop: "0.5rem", paddingBottom: "0.5rem" }}>
        {recent.length === 0 ? (
          <div className="db-empty" style={{ border: "none", padding: "1.75rem 0" }}>
            <UsersRound size={24} strokeWidth={1.5} />
            <span className="db-empty-title">No candidates yet</span>
            <span className="db-empty-sub">Run a sync and the newest arrivals show up here.</span>
          </div>
        ) : (
          recent.map((candidate) => {
            const name = candidateNameOf(candidate);
            const status = statusOf(candidate);
            const role =
              candidate.profile?.current_designation ||
              candidate.profile?.work_experience?.[0]?.designation ||
              "—";
            return (
              <button
                key={candidate.id}
                type="button"
                className="rc-row"
                onClick={() => onOpenCandidate?.(candidate)}
              >
                <span className="rc-avatar" aria-hidden="true">
                  {initialsOf(name)}
                </span>
                <span style={{ minWidth: 0 }}>
                  <span className="rc-name">{name}</span>
                  <span className="rc-sub">{timeAgo(candidate.created_at)}</span>
                </span>
                <span className="rc-role" title={role}>
                  {role}
                </span>
                <span className={`db-pill ${status.tone}`}>{status.label}</span>
              </button>
            );
          })
        )}
      </div>
    </section>
  );
}
