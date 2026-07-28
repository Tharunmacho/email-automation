"use client";

import React, { useEffect, useState } from "react";
import {
  Briefcase,
  CheckCircle2,
  Download,
  FolderGit2,
  GraduationCap,
  Save,
  Sparkles,
  User,
  X,
} from "lucide-react";
import {
  resumeDownloadUrl,
  type CandidateProfile,
  type CandidateRecord,
} from "@/lib/api";

interface CandidateModalProps {
  candidate: CandidateRecord | null;
  saving: boolean;
  verifying: boolean;
  onClose: () => void;
  onSave: (candidateId: string, profile: CandidateProfile) => void;
  onVerify: (candidateId: string) => void;
}

interface EditableFields {
  email: string;
  phone: string;
  location: string;
  experience: string;
  linkedin: string;
  github: string;
  skills: string;
}

function toFields(profile: CandidateProfile): EditableFields {
  return {
    email: profile.email ?? "",
    phone: profile.phone ?? "",
    location: profile.location ?? "",
    experience:
      profile.total_experience_years !== undefined && profile.total_experience_years !== null
        ? String(profile.total_experience_years)
        : "",
    linkedin: profile.linkedin_url ?? "",
    github: profile.github_url ?? "",
    skills: (profile.skills ?? []).join(", "),
  };
}

export default function CandidateModal(props: CandidateModalProps) {
  const { candidate } = props;

  // The overlay stays mounted (hidden) so its CSS transition has something to
  // animate; the editable body remounts per candidate, which re-seeds the form
  // without an effect.
  if (!candidate) {
    return null;
  }
  return <CandidateModalBody {...props} key={candidate.id} candidate={candidate} />;
}

function CandidateModalBody({
  candidate,
  saving,
  verifying,
  onClose,
  onSave,
  onVerify,
}: CandidateModalProps & { candidate: CandidateRecord }) {
  const [fields, setFields] = useState<EditableFields>(() =>
    toFields(candidate.profile ?? ({} as CandidateProfile)),
  );

  // Close on Escape while the modal is open.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const profile = candidate.profile ?? ({} as CandidateProfile);
  const initial = (profile.full_name || "U").charAt(0).toUpperCase();
  const experiences = profile.work_experience ?? [];
  const educations = profile.education ?? [];
  const projects = profile.projects ?? [];

  const update = (key: keyof EditableFields) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setFields((prev) => ({ ...prev, [key]: event.target.value }));

  const handleSave = () => {
    // Merge onto the existing profile: the API replaces the whole `profile`
    // sub-document, so sending only the edited fields would drop the parsed
    // experience/education/project history.
    const merged: CandidateProfile = {
      ...profile,
      email: fields.email.trim() || null,
      phone: fields.phone.trim() || null,
      location: fields.location.trim() || null,
      total_experience_years: fields.experience.trim() === "" ? null : Number(fields.experience),
      linkedin_url: fields.linkedin.trim() || null,
      github_url: fields.github.trim() || null,
      skills: fields.skills
        .split(",")
        .map((skill) => skill.trim())
        .filter(Boolean),
    };
    onSave(candidate.id, merged);
  };

  const emptyNote = (text: string) => (
    <p style={{ color: "var(--text-muted)", fontSize: "0.9rem" }}>{text}</p>
  );

  return (
    <div
      className="modal-overlay active"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal-container" role="dialog" aria-modal="true">
        <div className="modal-header">
          <h2 className="page-title" style={{ fontSize: "1.35rem" }}>
            {profile.full_name || "Candidate Profile"}
          </h2>
          <button className="modal-close" onClick={onClose} aria-label="Close profile">
            <X size={22} />
          </button>
        </div>

        <div className="modal-body">
          <div className="profile-header-block">
            <div className="candidate-avatar">{initial}</div>
            <div>
              <h3 className="page-title" style={{ fontSize: "1.25rem", fontWeight: 600 }}>
                {profile.full_name || "Unnamed"}
              </h3>
              <p style={{ color: "var(--text-muted)", fontSize: "0.9rem" }}>
                {profile.current_designation || "Not Provided"}
              </p>
            </div>
          </div>

          <div className="profile-summary">
            {profile.resume_summary || "No professional summary found on the resume."}
          </div>

          <h3 className="section-title">
            <User size={20} /> Contact &amp; General Info
          </h3>
          <div className="details-grid">
            <div className="field-group">
              <label className="field-label" htmlFor="edit-email">
                Email
              </label>
              <input
                id="edit-email"
                type="text"
                className="field-value"
                value={fields.email}
                onChange={update("email")}
              />
            </div>
            <div className="field-group">
              <label className="field-label" htmlFor="edit-phone">
                Phone
              </label>
              <input
                id="edit-phone"
                type="text"
                className="field-value"
                value={fields.phone}
                onChange={update("phone")}
              />
            </div>
            <div className="field-group">
              <label className="field-label" htmlFor="edit-location">
                Location
              </label>
              <input
                id="edit-location"
                type="text"
                className="field-value"
                value={fields.location}
                onChange={update("location")}
              />
            </div>
            <div className="field-group">
              <label className="field-label" htmlFor="edit-exp">
                Total Exp (Years)
              </label>
              <input
                id="edit-exp"
                type="number"
                step="0.1"
                className="field-value"
                value={fields.experience}
                onChange={update("experience")}
              />
            </div>
            <div className="field-group">
              <label className="field-label" htmlFor="edit-linkedin">
                LinkedIn
              </label>
              <input
                id="edit-linkedin"
                type="text"
                className="field-value"
                value={fields.linkedin}
                onChange={update("linkedin")}
              />
            </div>
            <div className="field-group">
              <label className="field-label" htmlFor="edit-github">
                GitHub
              </label>
              <input
                id="edit-github"
                type="text"
                className="field-value"
                value={fields.github}
                onChange={update("github")}
              />
            </div>
          </div>

          <h3 className="section-title">
            <Sparkles size={20} /> Technical &amp; Core Skills
          </h3>
          <div className="field-group" style={{ marginBottom: "2rem" }}>
            <input
              type="text"
              className="field-value"
              placeholder="Skills (comma separated)"
              value={fields.skills}
              onChange={update("skills")}
            />
          </div>

          <h3 className="section-title">
            <Briefcase size={20} /> Work Experience
          </h3>
          <div className="timeline">
            {experiences.length === 0
              ? emptyNote("No professional experience parsed.")
              : experiences.map((exp, index) => (
                  <div className="timeline-item" key={index}>
                    <div className="timeline-date">
                      {(exp.start_date || "") + (exp.end_date ? ` — ${exp.end_date}` : " — Present")}
                    </div>
                    <div className="timeline-title">{exp.designation || "Designation"}</div>
                    <div className="timeline-subtitle">
                      {exp.company || "Company"}
                      {exp.location ? ` | ${exp.location}` : ""}
                    </div>
                    <p className="timeline-desc">
                      {exp.description || "No job description extracted."}
                    </p>
                  </div>
                ))}
          </div>

          <h3 className="section-title">
            <GraduationCap size={20} /> Education History
          </h3>
          <div className="timeline">
            {educations.length === 0
              ? emptyNote("No education history parsed.")
              : educations.map((edu, index) => (
                  <div className="timeline-item" key={index}>
                    <div className="timeline-date">
                      {(edu.start_date || "") + (edu.end_date ? ` — ${edu.end_date}` : "")}
                    </div>
                    <div className="timeline-title">{edu.degree || "Degree"}</div>
                    <div className="timeline-subtitle">
                      {edu.institution || "Institution"}
                      {edu.grade ? ` | Grade: ${edu.grade}` : ""}
                    </div>
                  </div>
                ))}
          </div>

          <h3 className="section-title">
            <FolderGit2 size={20} /> Projects
          </h3>
          <div className="timeline">
            {projects.length === 0
              ? emptyNote("No projects listed.")
              : projects.map((project, index) => (
                  <div className="timeline-item" key={index}>
                    <div className="timeline-title">{project.name || "Project Name"}</div>
                    <p className="timeline-desc" style={{ marginTop: "0.25rem" }}>
                      {project.description || "No description listed."}
                    </p>
                    <div
                      style={{
                        marginTop: "0.5rem",
                        display: "flex",
                        gap: "0.35rem",
                        flexWrap: "wrap",
                      }}
                    >
                      {(project.technologies ?? []).map((tech, techIndex) => (
                        <span
                          className="skill-tag"
                          key={techIndex}
                          style={{ fontSize: "0.7rem", background: "rgba(20,184,166,0.05)" }}
                        >
                          {tech}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
          </div>
        </div>

        <div className="modal-footer">
          <a
            className="btn btn-secondary"
            href={resumeDownloadUrl(candidate.id)}
            download={candidate.resume?.original_filename ?? "resume.pdf"}
          >
            <Download size={18} /> Download Resume
          </a>

          {candidate.status !== "verified" && (
            <button
              className="btn btn-secondary"
              style={{ borderColor: "var(--success)", color: "var(--success)" }}
              onClick={() => onVerify(candidate.id)}
              disabled={verifying}
            >
              <CheckCircle2 size={18} /> {verifying ? "Verifying..." : "Verify Profile"}
            </button>
          )}

          <button className="btn" onClick={handleSave} disabled={saving}>
            <Save size={18} /> {saving ? "Saving..." : "Save Changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
