"use client";

import React, { useMemo } from "react";
import { Sparkles } from "lucide-react";
import type { CandidateRecord } from "@/lib/api";

interface PopularSkillsProps {
  candidates: CandidateRecord[];
}

export default function PopularSkills({ candidates }: PopularSkillsProps) {
  const topSkills = useMemo(() => {
    const counts = new Map<string, number>();
    for (const candidate of candidates) {
      for (const skill of candidate.profile?.skills ?? []) {
        counts.set(skill, (counts.get(skill) ?? 0) + 1);
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15);
  }, [candidates]);

  return (
    <div className="glass-card">
      <h2 className="section-title">
        <Sparkles size={20} /> Popular Candidate Skills
      </h2>
      <div className="skill-tags" style={{ maxHeight: "none", gap: "0.75rem" }}>
        {topSkills.length === 0 ? (
          <span style={{ color: "var(--text-muted)", fontSize: "0.9rem" }}>
            No candidate skills parsed yet.
          </span>
        ) : (
          topSkills.map(([skill, count]) => (
            <span
              key={skill}
              className="skill-tag"
              style={{
                padding: "0.35rem 0.85rem",
                fontSize: "0.85rem",
                background: "rgba(99, 102, 241, 0.08)",
                borderColor: "rgba(99, 102, 241, 0.15)",
              }}
            >
              {skill}{" "}
              <span style={{ color: "var(--secondary)", marginLeft: "0.25rem", fontWeight: 600 }}>
                {count}
              </span>
            </span>
          ))
        )}
      </div>
    </div>
  );
}
