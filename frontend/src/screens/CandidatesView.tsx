"use client";

import React, { useMemo, useState } from "react";
import { Search, UsersRound } from "lucide-react";
import CandidateCard from "@/components/CandidateCard";
import { formatInt } from "@/lib/format";
import type { CandidateRecord } from "@/lib/api";

interface CandidatesViewProps {
  candidates: CandidateRecord[];
  onOpenCandidate: (candidate: CandidateRecord) => void;
}

export default function CandidatesView({ candidates, onOpenCandidate }: CandidatesViewProps) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return candidates;

    return candidates.filter((candidate) => {
      const profile = candidate.profile ?? {};
      const haystack = [
        profile.full_name ?? "",
        profile.current_designation ?? "",
        profile.location ?? "",
        (profile.skills ?? []).join(" "),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [candidates, query]);

  return (
    <div className="tab-content active">
      <div className="filter-bar">
        <div className="search-input-wrapper">
          <Search size={18} />
          <input
            type="text"
            className="search-input"
            placeholder="Search by name, designation, location or skills..."
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <span className="result-count">
          {formatInt(filtered.length)}
          {filtered.length === candidates.length ? "" : ` of ${formatInt(candidates.length)}`}{" "}
          candidate{filtered.length === 1 ? "" : "s"}
        </span>
      </div>

      <div className="candidate-grid">
        {filtered.length === 0 ? (
          <div className="candidate-empty">
            <UsersRound size={40} strokeWidth={1.5} />
            <p>
              {candidates.length === 0
                ? "No candidates in the database yet."
                : `No candidates match “${query}”.`}
            </p>
            {candidates.length === 0 && (
              <span>Run a Gmail sync to ingest resumes from your inbox.</span>
            )}
          </div>
        ) : (
          filtered.map((candidate) => (
            <CandidateCard key={candidate.id} candidate={candidate} onOpen={onOpenCandidate} />
          ))
        )}
      </div>
    </div>
  );
}
