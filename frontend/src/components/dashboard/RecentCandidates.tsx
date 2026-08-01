"use client";

import React, { useMemo } from "react";
import { UserRound } from "lucide-react";
import {
  initialsOf,
  isVerified,
  needsReview,
  recentCandidates,
  timeAgo,
} from "@/lib/dashboardMetrics";
import type { CandidateRecord } from "@/lib/api";

interface RecentCandidatesProps {
  candidates: CandidateRecord[];
  onOpenCandidate?: (candidate: CandidateRecord) => void;
  limit?: number;
}

function statusOf(candidate: CandidateRecord): { label: string; tone: string } {
  if (isVerified(candidate)) return { label: "Verified", tone: "good" };
  if (needsReview(candidate)) return { label: "Review", tone: "warn" };
  return { label: "Parsed", tone: "neutral" };
}

export default function RecentCandidates({
  candidates,
  onOpenCandidate,
  limit = 6,
}: RecentCandidatesProps) {
  const recent = useMemo(() => recentCandidates(candidates, limit), [candidates, limit]);

  return (
    <section className="dash-card">
      <header className="dash-card-head">
        <div>
          <h3 className="dash-card-title">
            <UserRound size={17} strokeWidth={2.2} /> Latest arrivals
          </h3>
          <p className="dash-card-sub">Most recently ingested profiles</p>
        </div>
      </header>

      {recent.length === 0 ? (
        <p className="dash-empty">Nothing ingested yet.</p>
      ) : (
        <ul className="dash-people">
          {recent.map((candidate) => {
            const name = candidate.profile?.full_name?.trim() || "Unnamed candidate";
            const role =
              candidate.profile?.current_designation?.trim() ||
              candidate.profile?.current_company?.trim() ||
              candidate.source_email?.from_addr ||
              "Role not parsed";
            const status = statusOf(candidate);

            return (
              <li key={candidate.id}>
                <button
                  type="button"
                  className="dash-person"
                  onClick={() => onOpenCandidate?.(candidate)}
                  disabled={!onOpenCandidate}
                >
                  <span className="dash-avatar">{initialsOf(name)}</span>
                  <span className="dash-person-text">
                    <span className="dash-person-name">{name}</span>
                    <span className="dash-person-role">{role}</span>
                  </span>
                  <span className={`dash-pill dash-pill-${status.tone}`}>{status.label}</span>
                  <span className="dash-person-time">{timeAgo(candidate.created_at)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
