"use client";

import React, { useMemo, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  Download,
  FolderGit2,
  Link as LinkIcon,
  Mail,
  MapPin,
  Phone,
} from "lucide-react";

import {
  flattenExtras,
  highestQualificationOf,
  industryOf,
  isBlankValue,
  toBullets,
  toEditableState,
} from "@/lib/candidateProfile";
import { getToken, resumeDownloadUrl, type CandidateRecord } from "@/lib/api";
import { formatDateFull, initialsOf } from "@/lib/format";

interface CandidateProfileScreenProps {
  candidate: CandidateRecord;
  verifying?: boolean;
  onBack?: () => void;
  onVerify?: (candidateId: string) => void;
  hideTopbar?: boolean;
}

export default function CandidateProfileScreen({
  candidate,
  verifying = false,
  onBack,
  onVerify,
  hideTopbar = false,
}: CandidateProfileScreenProps) {
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const profile = candidate.profile ?? {};
  const view = useMemo(() => toEditableState(candidate.profile ?? {}, candidate), [candidate]);

  const skills = useMemo(
    () =>
      view.skills
        .split(",")
        .map((skill) => skill.trim().replace(/\.$/, ""))
        .filter(Boolean),
    [view.skills],
  );

  const additionalEntries = useMemo(
    () =>
      flattenExtras((profile.additional_info ?? {}) as Record<string, unknown>, [
        "industry",
        "highest_qualification",
      ]),
    [profile.additional_info],
  );

  const industry = industryOf(profile);
  const highestQualification = highestQualificationOf(profile);
  const isVerified = candidate.status === "verified";
  const ingestedOn = candidate.created_at ? formatDateFull(new Date(candidate.created_at)) : "—";

  /** Only sections that actually carry something are offered or drawn. */
  const sections = [
    { id: "details", label: "Details", present: true },
    { id: "summary", label: "Summary", present: Boolean(view.summary.trim()) },
    { id: "experience", label: "Experience", present: view.work_experience.length > 0 },
    { id: "skills", label: "Skills", present: skills.length > 0 },
    { id: "projects", label: "Projects", present: view.projects.length > 0 },
    { id: "education", label: "Education", present: view.education.length > 0 },
    { id: "achievements", label: "Achievements", present: view.achievements.length > 0 },
    { id: "certifications", label: "Certifications", present: view.certifications.length > 0 },
    { id: "additional", label: "Additional", present: additionalEntries.length > 0 },
  ].filter((section) => section.present);

  /** Only one profile screen is ever mounted, so a fixed prefix is unique. */
  const sectionId = (id: string) => `cprof-section-${id}`;

  const jumpTo = (id: string) => {
    document.getElementById(sectionId(id))?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const handleDownload = async () => {
    setDownloadError(null);
    setDownloading(true);
    try {
      const token = getToken();
      const response = await fetch(resumeDownloadUrl(candidate.id), {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!response.ok) throw new Error(`Server replied ${response.status}`);
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = candidate.resume?.original_filename || "resume.pdf";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : "Could not download the resume file.");
    } finally {
      setDownloading(false);
    }
  };

  const contactChips = [
    { icon: <Mail size={14} />, value: view.email },
    { icon: <Phone size={14} />, value: view.phone },
    { icon: <MapPin size={14} />, value: view.location },
    { icon: <LinkIcon size={14} />, value: view.linkedin },
    { icon: <FolderGit2 size={14} />, value: view.github },
  ].filter((chip) => !isBlankValue(chip.value));

  return (
    <div className="cscreen" style={{ animation: "fadeIn 0.3s ease" }}>
      {!hideTopbar && (
        <div className="cscreen-topbar">
          {onBack && (
            <button type="button" className="jod-back" onClick={onBack} title="Back to the candidates list">
              <ArrowLeft size={15} /> Back to candidates
            </button>
          )}

          <div className="cscreen-topbar-actions">
            <button
              type="button"
              className="cscreen-btn"
              onClick={handleDownload}
              disabled={downloading}
            >
              <Download size={15} /> {downloading ? "Downloading…" : "Download resume"}
            </button>
          </div>
        </div>
      )}

      {downloadError && <div className="cscreen-error">Resume download failed — {downloadError}</div>}

      <header className="cprof-hero">
        <span className="cprof-monogram" aria-hidden="true">
          {initialsOf(view.full_name)}
        </span>

        <div className="cprof-hero-text">
          <div className="cprof-hero-line">
            <h2 className="cprof-name">{view.full_name}</h2>
            <span className={`db-pill ${isVerified ? "is-verified" : "is-info"}`}>
              {isVerified ? "Verified" : "Active"}
            </span>
            <span className="cprof-readonly-tag">Read-only view</span>
          </div>

          {!isBlankValue(view.designation) && <p className="cprof-role">{view.designation}</p>}

          <p className="cprof-meta">
            {candidate.resume?.original_filename || "resume.pdf"} · Ingested {ingestedOn}
          </p>

          {contactChips.length > 0 && (
            <div className="cprof-chips">
              {contactChips.map((chip) => (
                <span key={chip.value} className="cprof-chip">
                  {chip.icon}
                  {chip.value}
                </span>
              ))}
            </div>
          )}
        </div>
      </header>

      {sections.length > 1 && (
        <nav className="cprof-nav" aria-label="Jump to a section">
          {sections.map((section) => (
            <button key={section.id} type="button" className="cprof-nav-btn" onClick={() => jumpTo(section.id)}>
              {section.label}
            </button>
          ))}
        </nav>
      )}

      <div className="cprof-sections">
        <section className="cprof-card" id={sectionId("details")}>
          <h3 className="cprof-card-title">Candidate details</h3>
          <div className="cprof-facts">
            <Fact label="Name" value={view.full_name} />
            <Fact label="Designation" value={view.designation} />
            <Fact label="Industry" value={industry} />
            <Fact label="Highest qualification" value={highestQualification} />
            <Fact label="Total experience" value={view.experience ? `${view.experience} year(s)` : ""} />
            <Fact label="Languages" value={view.languages} />
            <Fact label="Email" value={view.email} />
            <Fact label="Phone" value={view.phone} />
            <Fact label="LinkedIn" value={view.linkedin} />
            <Fact label="GitHub" value={view.github} />
            <Fact label="Address" value={view.location} />
          </div>
        </section>

        {view.summary.trim() && (
          <section className="cprof-card" id={sectionId("summary")}>
            <h3 className="cprof-card-title">Summary</h3>
            <BulletText text={view.summary} />
          </section>
        )}

        {view.work_experience.length > 0 && (
          <section className="cprof-card" id={sectionId("experience")}>
            <h3 className="cprof-card-title">Experience</h3>
            <div className="cprof-entries">
              {view.work_experience.map((exp, index) => (
                <article key={index} className="cprof-entry">
                  <h4 className="cprof-entry-title">
                    {exp.designation || "Role not stated"}
                    {exp.company ? <span className="cprof-entry-at"> · {exp.company}</span> : null}
                  </h4>
                  {/* No dates means the extractor found none. "0 months" reads
                      as a measured duration; a blank row is honest. */}
                  {(exp.start_date || exp.end_date || exp.location) && (
                    <p className="cprof-entry-meta">
                      {[[exp.start_date, exp.end_date].filter(Boolean).join(" – "), exp.location]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                  )}
                  {exp.description && <BulletText text={exp.description} />}
                </article>
              ))}
            </div>
          </section>
        )}

        {skills.length > 0 && (
          <section className="cprof-card" id={sectionId("skills")}>
            <h3 className="cprof-card-title">Skills</h3>
            <div className="cprof-skills">
              {skills.map((skill, index) => (
                <span key={`${skill}-${index}`} className="cprof-skill">
                  {skill}
                </span>
              ))}
            </div>
          </section>
        )}

        {view.projects.length > 0 && (
          <section className="cprof-card" id={sectionId("projects")}>
            <h3 className="cprof-card-title">Projects</h3>
            <div className="cprof-entries">
              {view.projects.map((project, index) => (
                <article key={index} className="cprof-entry">
                  <h4 className="cprof-entry-title">{project.name || `Project ${index + 1}`}</h4>
                  {project.technologies && <p className="cprof-entry-meta">{project.technologies}</p>}
                  {project.description && <BulletText text={project.description} />}
                </article>
              ))}
            </div>
          </section>
        )}

        {view.education.length > 0 && (
          <section className="cprof-card" id={sectionId("education")}>
            <h3 className="cprof-card-title">Education</h3>
            <div className="cprof-entries">
              {view.education.map((edu, index) => (
                <article key={index} className="cprof-entry">
                  <h4 className="cprof-entry-title">{edu.degree || "Qualification not stated"}</h4>
                  <p className="cprof-entry-meta">
                    {[
                      edu.institution,
                      [edu.start_date, edu.end_date].filter(Boolean).join(" – "),
                      edu.grade,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                </article>
              ))}
            </div>
          </section>
        )}

        {view.achievements.length > 0 && (
          <section className="cprof-card" id={sectionId("achievements")}>
            <h3 className="cprof-card-title">Achievements</h3>
            <ul className="cprof-bullets">
              {view.achievements.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </section>
        )}

        {view.certifications.length > 0 && (
          <section className="cprof-card" id={sectionId("certifications")}>
            <h3 className="cprof-card-title">Certifications</h3>
            <ul className="cprof-bullets">
              {view.certifications.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </section>
        )}

        {additionalEntries.length > 0 && (
          <section className="cprof-card" id={sectionId("additional")}>
            <h3 className="cprof-card-title">Additional information</h3>
            <div className="cprof-facts">
              {additionalEntries.map(([label, value]) => (
                <React.Fragment key={label}>
                  <div className="cprof-fact-label">{label}</div>
                  <div className="cprof-fact-value is-multiline">{value}</div>
                </React.Fragment>
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
