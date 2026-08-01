"use client";

import React from "react";
import { Calendar, MapPin } from "lucide-react";
import { candidateTheme, type CandidateRecord } from "@/lib/api";
import { formatDateFull, initialsOf } from "@/lib/format";

interface CandidateCardProps {
  candidate: CandidateRecord;
  onOpen: (candidate: CandidateRecord) => void;
}

export default function CandidateCard({ candidate, onOpen }: CandidateCardProps) {
  const profile = candidate.profile ?? {};
  const skills = profile.skills ?? [];
  const experiences = profile.work_experience ?? [];

  // Smart Name Resolution
  let displayName = profile.full_name;
  if (!displayName || displayName.toLowerCase() === "candidate profile" || displayName.toLowerCase() === "unnamed") {
    if (candidate.source_email?.from_name) {
      displayName = candidate.source_email.from_name;
    } else if (profile.email || candidate.source_email?.from_addr) {
      const addr = profile.email || candidate.source_email?.from_addr || "";
      const userPart = addr.split("@")[0].replace(/\d+/g, "");
      displayName = userPart.replace(/[._-]/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
    } else {
      displayName = "Candidate Profile";
    }
  }

  // Smart Title Resolution
  const displayTitle =
    profile.current_designation ||
    (experiences.length > 0 && experiences[0].designation
      ? experiences[0].designation
      : experiences.length > 0 && experiences[0].company
      ? experiences[0].company
      : "Software Professional");

  const initial = initialsOf(displayName);
  const { cardClass, badgeClass } = candidateTheme(candidate);

  const createdAt = candidate.created_at ? new Date(candidate.created_at) : null;
  const createdLabel =
    createdAt && !Number.isNaN(createdAt.getTime()) ? formatDateFull(createdAt) : "Recent";

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
        <span className={`badge ${badgeClass}`}>{candidate.status || "Ingested"}</span>
      </div>

      <div className="candidate-info">
        <h3 className="candidate-name">{displayName}</h3>
        <p className="candidate-title">{displayTitle}</p>
      </div>

      <dl className="cc-meta">
        <div className="cc-meta-item">
          <dd className="cc-meta-value">
            {typeof profile.total_experience_years === "number"
              ? `${profile.total_experience_years} yr${profile.total_experience_years === 1 ? "" : "s"}`
              : "Fresher"}
          </dd>
          <dt className="cc-meta-label">Experience</dt>
        </div>
        <div className="cc-meta-item">
          <dd className="cc-meta-value">{skills.length}</dd>
          <dt className="cc-meta-label">Skills parsed</dt>
        </div>
      </dl>

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
          <span>{profile.location || (experiences.length > 0 && experiences[0].company) || "Verified"}</span>
        </div>
        <div className="card-footer-item">
          <Calendar size={14} />
          <span>{createdLabel}</span>
        </div>
      </div>
    </div>
  );
}
