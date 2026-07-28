"use client";

import React from "react";
import { Calendar, MapPin } from "lucide-react";
import { candidateTheme, type CandidateRecord } from "@/lib/api";

interface CandidateCardProps {
  candidate: CandidateRecord;
  onOpen: (candidate: CandidateRecord) => void;
}

export default function CandidateCard({ candidate, onOpen }: CandidateCardProps) {
  const profile = candidate.profile ?? {};
  const skills = profile.skills ?? [];
  const initial = (profile.full_name || "U").charAt(0).toUpperCase();
  const { cardClass, badgeClass } = candidateTheme(candidate);

  const createdAt = candidate.created_at ? new Date(candidate.created_at) : null;
  const createdLabel =
    createdAt && !Number.isNaN(createdAt.getTime()) ? createdAt.toLocaleDateString() : "Unknown";

  return (
    <div
      className={`candidate-card ${cardClass}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(candidate)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(candidate);
        }
      }}
    >
      <div className="card-top">
        <div className="candidate-avatar">{initial}</div>
        <span className={`badge ${badgeClass}`}>{candidate.status || "Active"}</span>
      </div>

      <div className="candidate-info">
        <h3 className="candidate-name">{profile.full_name || "Unnamed Candidate"}</h3>
        <p className="candidate-title">{profile.current_designation || "Candidate Title"}</p>
      </div>

      <div className="skill-tags">
        {skills.slice(0, 5).map((skill, index) => (
          <span className="skill-tag" key={`${skill}-${index}`}>
            {skill}
          </span>
        ))}
      </div>

      <div className="card-footer">
        <div className="card-footer-item">
          <MapPin size={14} />
          <span>{profile.location || "Unknown"}</span>
        </div>
        <div className="card-footer-item">
          <Calendar size={14} />
          <span>{createdLabel}</span>
        </div>
      </div>
    </div>
  );
}
