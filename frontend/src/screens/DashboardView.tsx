"use client";

import React, { useMemo } from "react";
import StatsGrid from "@/components/StatsGrid";
import LogsConsole, { type LogEntry } from "@/components/LogsConsole";
import PopularSkills from "@/components/PopularSkills";
import { REVIEW_CONFIDENCE_THRESHOLD, type CandidateRecord } from "@/lib/api";

interface DashboardViewProps {
  total: number;
  candidates: CandidateRecord[];
  logs: LogEntry[];
}

export default function DashboardView({ total, candidates, logs }: DashboardViewProps) {
  const verified = candidates.filter((c) => c.status === "verified").length;
  const review = candidates.filter(
    (c) => (c.profile?.confidence ?? 1) < REVIEW_CONFIDENCE_THRESHOLD,
  ).length;

  return (
    <div className="tab-content active" style={{ display: "flex", flexDirection: "column", gap: "2rem" }}>
      {/* Dynamic Statistics Grid */}
      <StatsGrid total={total} verified={verified} review={review} />

      {/* Logs Console */}
      <LogsConsole logs={logs} />

      {/* Popular Candidate Skills */}
      <PopularSkills candidates={candidates} />
    </div>
  );
}

