"use client";

import React, { useMemo, useState } from "react";
import { Search, UsersRound } from "lucide-react";
import CandidateCard from "@/components/CandidateCard";
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
      </div>

      <div className="candidate-grid">
        {filtered.length === 0 ? (
          <div
            style={{
              gridColumn: "1 / -1",
              textAlign: "center",
              padding: "3rem",
              color: "var(--text-muted)",
            }}
          >
            <UsersRound size={48} style={{ marginBottom: "1rem", opacity: 0.5 }} />
            <p>
              {candidates.length === 0
                ? "No candidates found in database."
                : `No candidates match "${query}".`}
            </p>
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
