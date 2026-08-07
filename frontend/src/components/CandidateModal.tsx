"use client";

import React, { useEffect, useState } from "react";
import {
  Briefcase,
  CheckCircle2,
  Download,
  Edit3,
  ExternalLink,
  FolderGit2,
  Globe,
  GraduationCap,
  Layers,
  Link as LinkIcon,
  Mail,
  MapPin,
  Phone,
  Plus,
  Save,
  Sparkles,
  Trash2,
  User,
  X,
} from "lucide-react";
import {
  getToken,
  resumeDownloadUrl,
  type CandidateProfile,
  type CandidateRecord,
  type Education,
  type Project,
  type WorkExperience,
} from "@/lib/api";

interface CandidateModalProps {
  candidate: CandidateRecord | null;
  saving: boolean;
  verifying: boolean;
  initialEditMode?: boolean;
  onClose: () => void;
  onSave: (candidateId: string, profile: CandidateProfile) => void;
  onVerify: (candidateId: string) => void;
  onDelete?: (candidateId: string) => void;
}

interface EditableWorkExp {
  company: string;
  designation: string;
  start_date: string;
  end_date: string;
  location: string;
  description: string;
}

interface EditableEdu {
  degree: string;
  institution: string;
  start_date: string;
  end_date: string;
  grade: string;
}

interface EditableProject {
  name: string;
  description: string;
  technologies: string;
}

interface EditableState {
  full_name: string;
  designation: string;
  summary: string;
  email: string;
  phone: string;
  location: string;
  experience: string;
  linkedin: string;
  github: string;
  skills: string;
  work_experience: EditableWorkExp[];
  education: EditableEdu[];
  projects: EditableProject[];
  achievements: string[];
}

function toEditableState(profile: CandidateProfile, candidate?: CandidateRecord): EditableState {
  const experiences = profile.work_experience ?? [];
  const educations = profile.education ?? [];
  const projects = profile.projects ?? [];
  const achievements = profile.achievements ?? [];

  const inferredDesignation =
    profile.current_designation ||
    (experiences.length > 0 ? experiences[0].designation ?? "" : "");

  let inferredName = profile.full_name ?? "";
  if (!inferredName || inferredName.toLowerCase() === "candidate profile" || inferredName.toLowerCase() === "unnamed") {
    if (candidate?.source_email?.from_name) {
      inferredName = candidate.source_email.from_name;
    } else if (profile.email || candidate?.source_email?.from_addr) {
      const addr = profile.email || candidate?.source_email?.from_addr || "";
      const userPart = addr.split("@")[0].replace(/\d+/g, "");
      inferredName = userPart.replace(/[._-]/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
    } else {
      inferredName = "Candidate Profile";
    }
  }

  const email = profile.email || candidate?.email_key || candidate?.source_email?.from_addr || "";
  const rawPhone = profile.phone || candidate?.phone_key || "";
  const phone = rawPhone ? (rawPhone.startsWith("+") ? rawPhone : `+91 ${rawPhone}`) : "";

  return {
    full_name: inferredName,
    designation: inferredDesignation,
    summary: profile.resume_summary ?? "",
    email: email,
    phone: phone,
    location: profile.location ?? "",
    experience:
      profile.total_experience_years !== undefined && profile.total_experience_years !== null
        ? String(profile.total_experience_years)
        : "",
    linkedin: profile.linkedin_url ?? "",
    github: profile.github_url ?? "",
    skills: (profile.skills ?? []).join(", "),
    work_experience: experiences.map((exp) => {
      const companyVal = (exp.company ?? "").trim();
      const cleanCompany =
        companyVal.toLowerCase() === "company" || companyVal.toLowerCase() === "n/a" ? "" : companyVal;
      return {
        company: cleanCompany,
        designation: exp.designation ?? "",
        start_date: exp.start_date ?? "",
        end_date: exp.end_date ?? "",
        location: exp.location ?? "",
        description: exp.description ?? "",
      };
    }),
    education: educations.map((edu) => ({
      degree: edu.degree ?? "",
      institution: edu.institution ?? "",
      start_date: edu.start_date ?? "",
      end_date: edu.end_date ?? "",
      grade: edu.grade ?? "",
    })),
    projects: projects.map((proj) => ({
      name: proj.name ?? "",
      description: proj.description ?? "",
      technologies: (proj.technologies ?? []).join(", "),
    })),
    achievements: achievements,
  };
}

export default function CandidateModal(props: CandidateModalProps) {
  const { candidate } = props;

  if (!candidate) {
    return null;
  }
  return <CandidateModalBody {...props} key={candidate.id} candidate={candidate} />;
}

function CandidateModalBody({
  candidate,
  saving,
  verifying,
  initialEditMode,
  onClose,
  onSave,
  onVerify,
  onDelete,
}: CandidateModalProps & { candidate: CandidateRecord }) {
  const [isEditing, setIsEditing] = useState(initialEditMode ?? false);
  const [downloading, setDownloading] = useState(false);
  const [revealedFields, setRevealedFields] = useState<Record<string, boolean>>({});
  const revealField = (key: string) => setRevealedFields((prev) => ({ ...prev, [key]: true }));

  const handleDownload = async () => {
    if (!candidate) return;
    setDownloading(true);
    try {
      const token = getToken();
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;

      const res = await fetch(resumeDownloadUrl(candidate.id), { headers });
      if (!res.ok) {
        let msg = "Failed to download resume";
        try {
          const body = await res.json();
          if (body?.detail) msg = String(body.detail);
        } catch {}
        throw new Error(msg);
      }

      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = candidate.resume?.original_filename ?? `${candidate.profile?.full_name || "resume"}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err: any) {
      alert(err?.message || "Could not download resume file.");
    } finally {
      setDownloading(false);
    }
  };

  const [state, setState] = useState<EditableState>(() =>
    toEditableState(candidate.profile ?? ({} as CandidateProfile), candidate),
  );

  // Sync state whenever candidate prop changes or refreshes
  useEffect(() => {
    if (candidate?.profile) {
      setState(toEditableState(candidate.profile, candidate));
    }
  }, [candidate]);

  // Close on Escape while the modal is open.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const profile = candidate.profile ?? ({} as CandidateProfile);
  const experiences = profile.work_experience ?? [];
  const educations = profile.education ?? [];
  const projects = profile.projects ?? [];

  const displayName = state.full_name || candidate.source_email?.from_name || profile.full_name || "Candidate Profile";
  const displayDesignation = state.designation || profile.current_designation || (experiences[0]?.designation ?? "");
  const initial = displayName.charAt(0).toUpperCase();

  const updateRoot = (key: keyof EditableState) => (
    event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
  ) => setState((prev) => ({ ...prev, [key]: event.target.value }));

  // Work Exp handlers
  const updateWorkExp = (index: number, field: keyof EditableWorkExp, val: string) => {
    setState((prev) => {
      const next = [...prev.work_experience];
      next[index] = { ...next[index], [field]: val };
      return { ...prev, work_experience: next };
    });
  };
  const addWorkExp = () => {
    setState((prev) => ({
      ...prev,
      work_experience: [
        ...prev.work_experience,
        { company: "", designation: "", start_date: "", end_date: "", location: "", description: "" },
      ],
    }));
  };
  const removeWorkExp = (index: number) => {
    setState((prev) => ({
      ...prev,
      work_experience: prev.work_experience.filter((_, i) => i !== index),
    }));
  };

  // Education handlers
  const updateEdu = (index: number, field: keyof EditableEdu, val: string) => {
    setState((prev) => {
      const next = [...prev.education];
      next[index] = { ...next[index], [field]: val };
      return { ...prev, education: next };
    });
  };
  const addEdu = () => {
    setState((prev) => ({
      ...prev,
      education: [...prev.education, { degree: "", institution: "", start_date: "", end_date: "", grade: "" }],
    }));
  };
  const removeEdu = (index: number) => {
    setState((prev) => ({
      ...prev,
      education: prev.education.filter((_, i) => i !== index),
    }));
  };

  // Project handlers
  const updateProj = (index: number, field: keyof EditableProject, val: string) => {
    setState((prev) => {
      const next = [...prev.projects];
      next[index] = { ...next[index], [field]: val };
      return { ...prev, projects: next };
    });
  };
  const addProj = () => {
    setState((prev) => ({
      ...prev,
      projects: [...prev.projects, { name: "", description: "", technologies: "" }],
    }));
  };
  const removeProj = (index: number) => {
    setState((prev) => ({
      ...prev,
      projects: prev.projects.filter((_, i) => i !== index),
    }));
  };

  // Achievements handlers
  const updateAch = (index: number, val: string) => {
    setState((prev) => {
      const next = [...(prev.achievements ?? [])];
      next[index] = val;
      return { ...prev, achievements: next };
    });
  };
  const addAch = () => {
    setState((prev) => ({
      ...prev,
      achievements: [...(prev.achievements ?? []), ""],
    }));
  };
  const removeAch = (index: number) => {
    setState((prev) => ({
      ...prev,
      achievements: (prev.achievements ?? []).filter((_, i) => i !== index),
    }));
  };

  const handleSave = () => {
    const mergedWorkExperience: WorkExperience[] = state.work_experience
      .filter((w) => w.company.trim() || w.designation.trim() || w.description.trim())
      .map((w) => ({
        company: w.company.trim() || null,
        designation: w.designation.trim() || null,
        start_date: w.start_date.trim() || null,
        end_date: w.end_date.trim() || null,
        location: w.location.trim() || null,
        description: w.description.trim() || null,
      }));

    const mergedEducation: Education[] = state.education
      .filter((e) => e.degree.trim() || e.institution.trim())
      .map((e) => ({
        degree: e.degree.trim() || null,
        institution: e.institution.trim() || null,
        start_date: e.start_date.trim() || null,
        end_date: e.end_date.trim() || null,
        grade: e.grade.trim() || null,
      }));

    const mergedProjects: Project[] = state.projects
      .filter((p) => p.name.trim() || p.description.trim())
      .map((p) => ({
        name: p.name.trim() || null,
        description: p.description.trim() || null,
        technologies: p.technologies
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      }));

    const merged: CandidateProfile = {
      ...profile,
      full_name: state.full_name.trim() || null,
      current_designation: state.designation.trim() || null,
      resume_summary: state.summary.trim() || null,
      email: state.email.trim() || null,
      phone: state.phone.trim() || null,
      location: state.location.trim() || null,
      total_experience_years: state.experience.trim() === "" ? null : Number(state.experience),
      linkedin_url: state.linkedin.trim() || null,
      github_url: state.github.trim() || null,
      skills: state.skills
        .split(",")
        .map((skill) => skill.trim())
        .filter(Boolean),
      work_experience: mergedWorkExperience,
      education: mergedEducation,
      projects: mergedProjects,
      achievements: (state.achievements ?? []).map((a) => a.trim()).filter(Boolean),
    };

    onSave(candidate.id, merged);
    setIsEditing(false);
  };

  const parsedSkills = state.skills
    .split(",")
    .map((s) => s.trim().replace(/\.$/, ""))
    .filter((s) => {
      const lower = s.toLowerCase();
      if (
        !s ||
        lower.includes("degree") ||
        lower.includes("simats") ||
        lower.includes("engineering") ||
        lower.includes("2024-2028") ||
        lower.includes("0 months") ||
        lower.includes("graduation")
      ) {
        return false;
      }
      return true;
    });

  return (
    <div
      className="modal-overlay active"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal-container" role="dialog" aria-modal="true" style={{ maxWidth: "840px" }}>
        <div className="modal-header" style={{ borderBottom: "1px solid var(--border-blue)", paddingBottom: "1rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
            <h2 className="page-title" style={{ fontSize: "1.25rem", margin: 0 }}>
              {isEditing ? "Full Edit Candidate Profile" : "Candidate Executive Profile"}
            </h2>
            <span
              className={`badge ${
                candidate.status === "verified"
                  ? "badge-verified"
                  : "badge-active"
              }`}
              style={{ fontSize: "0.75rem" }}
            >
              {candidate.status || "Ingested"}
            </span>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <button
              className={`btn ${isEditing ? "btn-primary" : "btn-secondary"}`}
              onClick={() => setIsEditing((prev) => !prev)}
              style={{
                fontSize: "0.85rem",
                padding: "0.4rem 0.85rem",
                borderColor: isEditing ? "var(--primary)" : undefined,
                background: isEditing ? "var(--primary)" : undefined,
                color: isEditing ? "#fff" : undefined,
              }}
            >
              <Edit3 size={15} /> {isEditing ? "View Executive Mode" : "Edit Profile"}
            </button>

            {onDelete && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => {
                  if (confirm(`Are you sure you want to permanently delete candidate "${displayName}" from MongoDB Atlas?`)) {
                    onDelete(candidate.id);
                    onClose();
                  }
                }}
                style={{
                  fontSize: "0.85rem",
                  padding: "0.4rem 0.85rem",
                  borderColor: "rgba(239,68,68,0.4)",
                  background: "rgba(239,68,68,0.08)",
                  color: "#ef4444",
                }}
              >
                <Trash2 size={15} /> Delete Candidate
              </button>
            )}

            <button className="modal-close" onClick={onClose} aria-label="Close profile">
              <X size={20} />
            </button>
          </div>
        </div>

        <div className="modal-body" style={{ paddingTop: "1.25rem" }}>
          {/* ---- HEADER BANNER ---- */}
          <div
            className="profile-header-block"
            style={{
              display: "flex",
              alignItems: "center",
              gap: "1.25rem",
              padding: "1.25rem",
              background: "linear-gradient(135deg, rgba(11, 95, 255, 0.08) 0%, rgba(10, 36, 114, 0.05) 100%)",
              borderRadius: "0.75rem",
              border: "1px solid var(--border-blue)",
              marginBottom: "1.5rem",
            }}
          >
            <div
              className="candidate-avatar"
              style={{
                width: "64px",
                height: "64px",
                fontSize: "1.65rem",
                fontWeight: 700,
                background: "linear-gradient(140deg, var(--primary) 0%, var(--primary-deep) 100%)",
                color: "#fff",
                boxShadow: "0 4px 14px rgba(11, 95, 255, 0.3)",
              }}
            >
              {initial}
            </div>

            <div style={{ flex: 1 }}>
              {isEditing ? (
                <div className="modal-grid-2">
                  <div className="field-group">
                    <label className="field-label" style={{ fontWeight: 600, color: "var(--primary)" }}>Candidate Name</label>
                    <input
                      type="text"
                      className="field-value"
                      style={{ fontSize: "1rem", fontWeight: 600, padding: "0.5rem 0.75rem", width: "100%" }}
                      value={state.full_name}
                      onChange={updateRoot("full_name")}
                      placeholder="Full Name"
                    />
                  </div>
                  <div className="field-group">
                    <label className="field-label" style={{ fontWeight: 600, color: "var(--primary)" }}>Designation / Role</label>
                    <input
                      type="text"
                      className="field-value"
                      style={{ fontSize: "0.95rem", fontWeight: 400, padding: "0.5rem 0.75rem", width: "100%" }}
                      value={state.designation}
                      onChange={updateRoot("designation")}
                      placeholder="e.g. Founder / Senior AI Engineer"
                    />
                  </div>
                </div>
              ) : (
                <>
                  <h3 style={{ fontSize: "1.4rem", fontWeight: 700, margin: 0, color: "var(--text-main)", letterSpacing: "-0.01em" }}>
                    {displayName}
                  </h3>
                  {displayDesignation && (
                    <p style={{ color: "var(--primary)", fontSize: "0.95rem", fontWeight: 600, marginTop: "0.25rem" }}>
                      {displayDesignation}
                    </p>
                  )}
                </>
              )}
            </div>
          </div>

          {/* ---- PROFESSIONAL SUMMARY ---- */}
          {Boolean(state.summary && state.summary.trim() !== "0 months" && state.summary.trim().length > 5) && (
            <div style={{ marginBottom: "1.5rem" }}>
              <h3 className="section-title" style={{ fontSize: "1rem", marginBottom: "0.75rem" }}>
                <Sparkles size={18} /> Professional Summary
              </h3>

              {isEditing ? (
                <textarea
                  rows={4}
                  style={{
                    width: "100%",
                    padding: "0.75rem 0.85rem",
                    borderRadius: "0.5rem",
                    border: "1px solid var(--border-blue)",
                    background: "var(--bg-card)",
                    color: "var(--text-main)",
                    fontSize: "0.9rem",
                    fontWeight: 400,
                    lineHeight: "1.6",
                    maxHeight: "180px",
                    overflowY: "auto",
                    resize: "vertical",
                  }}
                  value={state.summary}
                  onChange={updateRoot("summary")}
                  placeholder="Professional summary..."
                />
              ) : (
                <div
                  style={{
                    padding: "1rem 1.25rem",
                    borderRadius: "0.6rem",
                    background: "var(--tint-1)",
                    borderLeft: "4px solid var(--primary)",
                    fontSize: "0.92rem",
                    lineHeight: "1.65",
                    color: "var(--text-main)",
                    maxHeight: "220px",
                    overflowY: "auto",
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {state.summary}
                </div>
              )}
            </div>
          )}

          {/* ---- CONTACT & GENERAL INFO ---- */}
          {(state.email || state.phone || state.linkedin || state.github || state.location || state.experience) && (
            <div style={{ marginBottom: "1.5rem" }}>
              <h3 className="section-title" style={{ fontSize: "1rem", marginBottom: "0.75rem" }}>
                <User size={18} /> Contact &amp; General Details
              </h3>

              {isEditing ? (
                <div className="details-grid">
                  {state.email && (
                    <div className="field-group">
                      <label className="field-label" style={{ fontWeight: 600 }}>Email Address</label>
                      <input
                        type="text"
                        className="field-value"
                        style={{ fontWeight: 400, fontSize: "0.9rem", padding: "0.5rem 0.75rem", width: "100%" }}
                        value={state.email}
                        onChange={updateRoot("email")}
                      />
                    </div>
                  )}
                  {state.phone && (
                    <div className="field-group">
                      <label className="field-label" style={{ fontWeight: 600 }}>Phone Number</label>
                      <input
                        type="text"
                        className="field-value"
                        style={{ fontWeight: 400, fontSize: "0.9rem", padding: "0.5rem 0.75rem", width: "100%" }}
                        value={state.phone}
                        onChange={updateRoot("phone")}
                      />
                    </div>
                  )}
                  {state.location && (
                    <div className="field-group">
                      <label className="field-label" style={{ fontWeight: 600 }}>Location</label>
                      <input
                        type="text"
                        className="field-value"
                        style={{ fontWeight: 400, fontSize: "0.9rem", padding: "0.5rem 0.75rem", width: "100%" }}
                        value={state.location}
                        onChange={updateRoot("location")}
                      />
                    </div>
                  )}
                  {Boolean(state.experience && state.experience !== "0" && state.experience !== "0.0") && (
                    <div className="field-group">
                      <label className="field-label" style={{ fontWeight: 600 }}>Total Experience (Years)</label>
                      <input
                        type="number"
                        step="0.1"
                        className="field-value"
                        style={{ fontWeight: 400, fontSize: "0.9rem", padding: "0.5rem 0.75rem", width: "100%" }}
                        value={state.experience}
                        onChange={updateRoot("experience")}
                      />
                    </div>
                  )}
                  {state.linkedin && (
                    <div className="field-group">
                      <label className="field-label" style={{ fontWeight: 600 }}>LinkedIn URL</label>
                      <input
                        type="text"
                        className="field-value"
                        style={{ fontWeight: 400, fontSize: "0.9rem", padding: "0.5rem 0.75rem", width: "100%" }}
                        value={state.linkedin}
                        onChange={updateRoot("linkedin")}
                      />
                    </div>
                  )}
                  {state.github && (
                    <div className="field-group">
                      <label className="field-label" style={{ fontWeight: 600 }}>GitHub URL</label>
                      <input
                        type="text"
                        className="field-value"
                        style={{ fontWeight: 400, fontSize: "0.9rem", padding: "0.5rem 0.75rem", width: "100%" }}
                        value={state.github}
                        onChange={updateRoot("github")}
                      />
                    </div>
                  )}
                </div>
              ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem" }}>
                {state.email && (
                  <a
                    href={`mailto:${state.email}`}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      padding: "0.5rem 0.85rem",
                      borderRadius: "0.5rem",
                      background: "rgba(11, 95, 255, 0.08)",
                      color: "var(--primary)",
                      fontSize: "0.85rem",
                      fontWeight: 500,
                      textDecoration: "none",
                    }}
                  >
                    <Mail size={15} /> {state.email}
                  </a>
                )}

                {state.phone && (
                  <a
                    href={`tel:${state.phone}`}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      padding: "0.5rem 0.85rem",
                      borderRadius: "0.5rem",
                      background: "rgba(16,185,129,0.08)",
                      color: "var(--success)",
                      fontSize: "0.85rem",
                      fontWeight: 500,
                      textDecoration: "none",
                    }}
                  >
                    <Phone size={15} /> {state.phone}
                  </a>
                )}

                {state.linkedin && (
                  <a
                    href={state.linkedin}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      padding: "0.5rem 0.85rem",
                      borderRadius: "0.5rem",
                      background: "rgba(10,102,194,0.12)",
                      color: "#0a66c2",
                      fontSize: "0.85rem",
                      fontWeight: 600,
                      textDecoration: "none",
                    }}
                  >
                    <Globe size={15} /> LinkedIn Profile <ExternalLink size={13} />
                  </a>
                )}

                {state.github && (
                  <a
                    href={state.github}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      padding: "0.5rem 0.85rem",
                      borderRadius: "0.5rem",
                      background: "var(--tint-1)",
                      color: "var(--text-main)",
                      fontSize: "0.85rem",
                      fontWeight: 500,
                      textDecoration: "none",
                    }}
                  >
                    <LinkIcon size={15} /> GitHub Profile <ExternalLink size={13} />
                  </a>
                )}

                {state.location && (
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      padding: "0.5rem 0.85rem",
                      borderRadius: "0.5rem",
                      background: "var(--tint-1)",
                      color: "var(--text-muted)",
                      fontSize: "0.85rem",
                    }}
                  >
                    <MapPin size={15} /> {state.location}
                  </span>
                )}

                {Boolean(state.experience && state.experience !== "0" && state.experience !== "0.0") && (
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      padding: "0.5rem 0.85rem",
                      borderRadius: "0.5rem",
                      background: "rgba(245,158,11,0.08)",
                      color: "#f59e0b",
                      fontSize: "0.85rem",
                      fontWeight: 500,
                    }}
                  >
                    <Briefcase size={15} /> {state.experience} Yrs Experience
                  </span>
                )}
              </div>
            )}
          </div>
          )}

          {/* ---- TECHNICAL SKILLS ---- */}
          {(isEditing || parsedSkills.length > 0) && (
            <div style={{ marginBottom: "1.5rem" }}>
              <h3 className="section-title" style={{ fontSize: "1rem", marginBottom: "0.75rem" }}>
                <Sparkles size={18} /> Technical &amp; Core Competencies
              </h3>

            {isEditing ? (
              <textarea
                rows={3}
                style={{
                  width: "100%",
                  padding: "0.75rem 0.85rem",
                  borderRadius: "0.5rem",
                  border: "1px solid var(--border-blue)",
                  background: "var(--bg-card)",
                  color: "var(--text-main)",
                  fontSize: "0.9rem",
                  fontWeight: 400,
                  lineHeight: "1.5",
                  maxHeight: "120px",
                  overflowY: "auto",
                  resize: "vertical",
                }}
                placeholder="Skills (comma separated, e.g. Python, AI, React, Docker, CAMS)"
                value={state.skills}
                onChange={updateRoot("skills")}
              />
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem" }}>
                {parsedSkills.map((skill, index) => (
                  <span
                    key={index}
                    style={{
                      padding: "0.4rem 0.85rem",
                      borderRadius: "2rem",
                      background: "linear-gradient(135deg, rgba(11, 95, 255, 0.12) 0%, rgba(0, 194, 255,0.12) 100%)",
                      border: "1px solid rgba(11, 95, 255, 0.25)",
                      color: "var(--text-main)",
                      fontSize: "0.82rem",
                      fontWeight: 600,
                      boxShadow: "0 2px 6px rgba(0,0,0,0.1)",
                    }}
                  >
                    {skill}
                  </span>
                ))}
              </div>
            )}
          </div>
          )}

          {/* ---- ACHIEVEMENTS & CERTIFICATIONS ---- */}
          {(isEditing ? true : (profile.achievements ?? []).length > 0 || (profile.certifications ?? []).length > 0) && (
            <div style={{ marginBottom: "1.5rem" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
                <h3 className="section-title" style={{ fontSize: "1rem", margin: 0 }}>
                  <CheckCircle2 size={18} /> Achievements &amp; Certifications
                </h3>
                {isEditing && (
                  <button
                    type="button"
                    onClick={addAch}
                    style={{
                      padding: "0.3rem 0.6rem",
                      borderRadius: "0.4rem",
                      background: "rgba(16,185,129,0.12)",
                      border: "1px solid rgba(16,185,129,0.3)",
                      color: "#10b981",
                      fontSize: "0.8rem",
                      fontWeight: 600,
                      cursor: "pointer",
                    }}
                  >
                    + Add Item
                  </button>
                )}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                {isEditing ? (
                  state.achievements.map((ach, idx) => (
                    <div key={`edit_ach_${idx}`} style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                      <input
                        type="text"
                        value={ach}
                        onChange={(e) => updateAch(idx, e.target.value)}
                        placeholder="Achievement or Certification detail..."
                        style={{
                          flex: 1,
                          padding: "0.5rem 0.75rem",
                          borderRadius: "0.4rem",
                          border: "1px solid var(--border-blue)",
                          background: "var(--tint-1)",
                          color: "var(--text-main)",
                          fontSize: "0.85rem",
                        }}
                      />
                      <button
                        type="button"
                        onClick={() => removeAch(idx)}
                        style={{
                          padding: "0.5rem",
                          borderRadius: "0.4rem",
                          background: "rgba(239,68,68,0.1)",
                          border: "1px solid rgba(239,68,68,0.2)",
                          color: "#ef4444",
                          cursor: "pointer",
                        }}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))
                ) : (
                  <>
                    {(profile.achievements ?? [])
                      .filter((ach) => {
                        const lower = ach.toLowerCase();
                        return !["undergraduate", "about me", "skills", "experience set", "passionate", "full stack developer"].some((noise) => lower.includes(noise));
                      })
                      .map((ach, idx) => (
                        <div
                          key={`ach_${idx}`}
                          style={{
                            padding: "0.6rem 0.85rem",
                            borderRadius: "0.5rem",
                            background: "rgba(16,185,129,0.06)",
                            border: "1px solid rgba(16,185,129,0.2)",
                            color: "var(--text-main)",
                            fontSize: "0.88rem",
                            fontWeight: 500,
                          }}
                        >
                          🏆 {ach}
                        </div>
                      ))}
                    {(profile.certifications ?? []).map((cert, idx) => (
                      <div
                        key={`cert_${idx}`}
                        style={{
                          padding: "0.6rem 0.85rem",
                          borderRadius: "0.5rem",
                          background: "rgba(11, 95, 255, 0.06)",
                          border: "1px solid rgba(11, 95, 255, 0.2)",
                          color: "var(--text-main)",
                          fontSize: "0.88rem",
                          fontWeight: 500,
                        }}
                      >
                        📜 {cert}
                      </div>
                    ))}
                  </>
                )}
              </div>
            </div>
          )}

          {/* ---- WORK EXPERIENCE ---- */}
          {(isEditing ? state.work_experience.length > 0 : experiences.length > 0) && (
            <div style={{ marginBottom: "1.5rem" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
              <h3 className="section-title" style={{ fontSize: "1rem", margin: 0 }}>
                <Briefcase size={18} /> Work Experience
              </h3>
              {isEditing && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={addWorkExp}
                  style={{ fontSize: "0.78rem", padding: "0.25rem 0.6rem" }}
                >
                  <Plus size={14} /> Add Experience
                </button>
              )}
            </div>

            {isEditing ? (
              <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                {state.work_experience.map((exp, index) => {
                  const isNew =
                    !exp.designation &&
                    !exp.company &&
                    !exp.start_date &&
                    !exp.end_date &&
                    !exp.location &&
                    !exp.description;

                  const showCompany =
                    isNew || Boolean(exp.company) || Boolean(revealedFields[`exp_${index}_company`]);
                  const showStartDate =
                    isNew || Boolean(exp.start_date) || Boolean(revealedFields[`exp_${index}_start_date`]);
                  const showEndDate =
                    isNew || Boolean(exp.end_date) || Boolean(revealedFields[`exp_${index}_end_date`]);
                  const showLocation =
                    isNew || Boolean(exp.location) || Boolean(revealedFields[`exp_${index}_location`]);
                  const showDescription =
                    isNew || Boolean(exp.description) || Boolean(revealedFields[`exp_${index}_description`]);

                  return (
                    <div
                      key={index}
                      style={{
                        padding: "1rem",
                        borderRadius: "0.6rem",
                        border: "1px solid var(--border-blue)",
                        background: "var(--tint-1)",
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
                        <span style={{ fontSize: "0.82rem", fontWeight: 700, color: "var(--primary)" }}>
                          Experience #{index + 1}
                        </span>
                        <button
                          type="button"
                          onClick={() => removeWorkExp(index)}
                          style={{ background: "none", border: "none", color: "var(--error)", cursor: "pointer" }}
                          title="Delete Experience"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>

                      <div className="modal-grid-2" style={{ marginBottom: "0.75rem" }}>
                        <div>
                          <label className="field-label">Designation / Role</label>
                          <input
                            type="text"
                            className="field-value"
                            style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                            value={exp.designation}
                            onChange={(e) => updateWorkExp(index, "designation", e.target.value)}
                            placeholder="e.g. Software Engineer"
                          />
                        </div>
                        {showCompany && (
                          <div>
                            <label className="field-label">Company Name</label>
                            <input
                              type="text"
                              className="field-value"
                              style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                              value={exp.company}
                              onChange={(e) => updateWorkExp(index, "company", e.target.value)}
                              placeholder="e.g. DiffuseAI Solutions"
                            />
                          </div>
                        )}
                      </div>

                      {(showStartDate || showEndDate || showLocation) && (
                        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: "0.75rem", marginBottom: "0.75rem" }}>
                          {showStartDate && (
                            <div>
                              <label className="field-label">Start Date / Year</label>
                              <input
                                type="text"
                                className="field-value"
                                style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                                value={exp.start_date}
                                onChange={(e) => updateWorkExp(index, "start_date", e.target.value)}
                                placeholder="e.g. 2023"
                              />
                            </div>
                          )}
                          {showEndDate && (
                            <div>
                              <label className="field-label">End Date / Year</label>
                              <input
                                type="text"
                                className="field-value"
                                style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                                value={exp.end_date}
                                onChange={(e) => updateWorkExp(index, "end_date", e.target.value)}
                                placeholder="e.g. Present"
                              />
                            </div>
                          )}
                          {showLocation && (
                            <div>
                              <label className="field-label">Location</label>
                              <input
                                type="text"
                                className="field-value"
                                style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                                value={exp.location}
                                onChange={(e) => updateWorkExp(index, "location", e.target.value)}
                                placeholder="e.g. India"
                              />
                            </div>
                          )}
                        </div>
                      )}

                      {showDescription && (
                        <div style={{ marginBottom: "0.75rem" }}>
                          <label className="field-label">Work Description</label>
                          <textarea
                            rows={3}
                            style={{
                              width: "100%",
                              padding: "0.6rem",
                              borderRadius: "0.4rem",
                              border: "1px solid var(--border-blue)",
                              background: "var(--bg-card)",
                              color: "var(--text-main)",
                              fontSize: "0.88rem",
                              lineHeight: "1.5",
                              maxHeight: "120px",
                              overflowY: "auto",
                              resize: "vertical",
                            }}
                            value={exp.description}
                            onChange={(e) => updateWorkExp(index, "description", e.target.value)}
                            placeholder="Describe key responsibilities..."
                          />
                        </div>
                      )}

                      {/* Quick reveal buttons for hidden empty fields */}
                      {(!showCompany || !showStartDate || !showEndDate || !showLocation || !showDescription) && (
                        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", marginTop: "0.5rem" }}>
                          {!showCompany && (
                            <button
                              type="button"
                              onClick={() => revealField(`exp_${index}_company`)}
                              style={{ background: "var(--tint-1)", border: "1px dashed var(--border-blue)", color: "var(--primary)", fontSize: "0.72rem", padding: "2px 8px", borderRadius: "4px", cursor: "pointer" }}
                            >
                              + Add Company
                            </button>
                          )}
                          {!showStartDate && (
                            <button
                              type="button"
                              onClick={() => revealField(`exp_${index}_start_date`)}
                              style={{ background: "var(--tint-1)", border: "1px dashed var(--border-blue)", color: "var(--primary)", fontSize: "0.72rem", padding: "2px 8px", borderRadius: "4px", cursor: "pointer" }}
                            >
                              + Add Start Date
                            </button>
                          )}
                          {!showEndDate && (
                            <button
                              type="button"
                              onClick={() => revealField(`exp_${index}_end_date`)}
                              style={{ background: "var(--tint-1)", border: "1px dashed var(--border-blue)", color: "var(--primary)", fontSize: "0.72rem", padding: "2px 8px", borderRadius: "4px", cursor: "pointer" }}
                            >
                              + Add End Date
                            </button>
                          )}
                          {!showLocation && (
                            <button
                              type="button"
                              onClick={() => revealField(`exp_${index}_location`)}
                              style={{ background: "var(--tint-1)", border: "1px dashed var(--border-blue)", color: "var(--primary)", fontSize: "0.72rem", padding: "2px 8px", borderRadius: "4px", cursor: "pointer" }}
                            >
                              + Add Location
                            </button>
                          )}
                          {!showDescription && (
                            <button
                              type="button"
                              onClick={() => revealField(`exp_${index}_description`)}
                              style={{ background: "var(--tint-1)", border: "1px dashed var(--border-blue)", color: "var(--primary)", fontSize: "0.72rem", padding: "2px 8px", borderRadius: "4px", cursor: "pointer" }}
                            >
                              + Add Description
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              experiences.length > 0 && (
                <div className="timeline">
                  {experiences.map((exp, index) => {
                    const hasDates = exp.start_date || exp.end_date;
                    const dateStr = hasDates ? `${exp.start_date || ""}${exp.end_date ? ` — ${exp.end_date}` : ""}` : "";
                    const subtitle = [exp.company, exp.location].filter(Boolean).join(" | ");

                    return (
                      <div
                        className="timeline-item"
                        key={index}
                        style={{
                          padding: "1rem 1.25rem",
                          borderRadius: "0.6rem",
                          background: "var(--tint-1)",
                          border: "1px solid var(--border-blue)",
                          marginBottom: "0.75rem",
                        }}
                      >
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                          <h4 style={{ fontSize: "1rem", fontWeight: 700, color: "var(--text-main)", margin: 0 }}>
                            {exp.designation || displayDesignation || "Software Role"}
                          </h4>
                          {dateStr && (
                            <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
                              {dateStr}
                            </span>
                          )}
                        </div>
                        {subtitle && (
                          <p style={{ fontSize: "0.88rem", fontWeight: 600, color: "var(--primary)", marginTop: "0.25rem" }}>
                            {subtitle}
                          </p>
                        )}
                        {exp.description && (
                          <p style={{ fontSize: "0.88rem", color: "var(--text-muted)", marginTop: "0.5rem", lineHeight: "1.55", whiteSpace: "pre-wrap" }}>
                            {exp.description}
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )
            )}
          </div>
          )}

          {/* ---- PROJECTS ---- */}
          {(isEditing ? state.projects.length > 0 : projects.length > 0) && (
            <div style={{ marginBottom: "1.5rem" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
              <h3 className="section-title" style={{ fontSize: "1rem", margin: 0 }}>
                <FolderGit2 size={18} /> Projects
              </h3>
              {isEditing && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={addProj}
                  style={{ fontSize: "0.78rem", padding: "0.25rem 0.6rem" }}
                >
                  <Plus size={14} /> Add Project
                </button>
              )}
            </div>

            {isEditing ? (
              <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                {state.projects.map((proj, index) => {
                  const isNew = !proj.name && !proj.description && !proj.technologies;
                  const showDesc = isNew || Boolean(proj.description) || Boolean(revealedFields[`proj_${index}_desc`]);
                  const showTech = isNew || Boolean(proj.technologies) || Boolean(revealedFields[`proj_${index}_tech`]);

                  return (
                    <div
                      key={index}
                      style={{
                        padding: "1rem",
                        borderRadius: "0.6rem",
                        border: "1px solid var(--border-blue)",
                        background: "var(--tint-1)",
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
                        <span style={{ fontSize: "0.82rem", fontWeight: 700, color: "var(--primary)" }}>
                          Project #{index + 1}
                        </span>
                        <button
                          type="button"
                          onClick={() => removeProj(index)}
                          style={{ background: "none", border: "none", color: "var(--error)", cursor: "pointer" }}
                          title="Delete Project"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>

                      <div style={{ marginBottom: "0.75rem" }}>
                        <label className="field-label">Project Name</label>
                        <input
                          type="text"
                          className="field-value"
                          style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                          value={proj.name}
                          onChange={(e) => updateProj(index, "name", e.target.value)}
                          placeholder="e.g. Grocery Shop Web App"
                        />
                      </div>

                      {showDesc && (
                        <div style={{ marginBottom: "0.75rem" }}>
                          <label className="field-label">Project Description</label>
                          <textarea
                            rows={3}
                            style={{
                              width: "100%",
                              padding: "0.6rem",
                              borderRadius: "0.4rem",
                              border: "1px solid var(--border-blue)",
                              background: "var(--bg-card)",
                              color: "var(--text-main)",
                              fontSize: "0.88rem",
                              lineHeight: "1.5",
                              maxHeight: "120px",
                              overflowY: "auto",
                              resize: "vertical",
                            }}
                            value={proj.description}
                            onChange={(e) => updateProj(index, "description", e.target.value)}
                            placeholder="Describe key outcomes..."
                          />
                        </div>
                      )}

                      {showTech && (
                        <div>
                          <label className="field-label">Technologies Used (comma separated)</label>
                          <input
                            type="text"
                            className="field-value"
                            style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                            value={proj.technologies}
                            onChange={(e) => updateProj(index, "technologies", e.target.value)}
                            placeholder="e.g. React, Node.js, MongoDB"
                          />
                        </div>
                      )}

                      {(!showDesc || !showTech) && (
                        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", marginTop: "0.5rem" }}>
                          {!showDesc && (
                            <button
                              type="button"
                              onClick={() => revealField(`proj_${index}_desc`)}
                              style={{ background: "var(--tint-1)", border: "1px dashed var(--border-blue)", color: "var(--primary)", fontSize: "0.72rem", padding: "2px 8px", borderRadius: "4px", cursor: "pointer" }}
                            >
                              + Add Description
                            </button>
                          )}
                          {!showTech && (
                            <button
                              type="button"
                              onClick={() => revealField(`proj_${index}_tech`)}
                              style={{ background: "var(--tint-1)", border: "1px dashed var(--border-blue)", color: "var(--primary)", fontSize: "0.72rem", padding: "2px 8px", borderRadius: "4px", cursor: "pointer" }}
                            >
                              + Add Technologies
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              projects.length > 0 && (
                <div className="timeline">
                  {projects.map((project, index) => (
                    <div
                      className="timeline-item"
                      key={index}
                      style={{
                        padding: "1rem 1.25rem",
                        borderRadius: "0.6rem",
                        background: "var(--tint-1)",
                        border: "1px solid var(--border-blue)",
                        marginBottom: "0.75rem",
                      }}
                    >
                      <h4 style={{ fontSize: "0.95rem", fontWeight: 700, color: "var(--text-main)", margin: 0 }}>
                        {project.name || "Project"}
                      </h4>
                      {project.description && (
                        <p style={{ fontSize: "0.88rem", color: "var(--text-muted)", marginTop: "0.35rem", lineHeight: "1.45", whiteSpace: "pre-wrap" }}>
                          {project.description}
                        </p>
                      )}
                      {(project.technologies ?? []).length > 0 && (
                        <div style={{ display: "flex", gap: "0.35rem", flexWrap: "wrap", marginTop: "0.5rem" }}>
                          {(project.technologies ?? []).map((tech, techIndex) => (
                            <span
                              key={techIndex}
                              style={{
                                fontSize: "0.72rem",
                                padding: "0.2rem 0.5rem",
                                borderRadius: "0.25rem",
                                background: "rgba(0, 194, 255, 0.12)",
                                /* --secondary is only 3.3:1 on this tint —
                                   the deeper step keeps the chip readable. */
                                color: "var(--secondary-hover)",
                              }}
                            >
                              {tech}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )
            )}
          </div>
          )}

          {/* ---- EDUCATION HISTORY ---- */}
          {(isEditing ? state.education.length > 0 : educations.length > 0) && (
            <div style={{ marginBottom: "1.5rem" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
                <h3 className="section-title" style={{ fontSize: "1rem", margin: 0 }}>
                  <GraduationCap size={18} /> Education History
                </h3>
                {isEditing && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={addEdu}
                    style={{ fontSize: "0.78rem", padding: "0.25rem 0.6rem" }}
                  >
                    <Plus size={14} /> Add Education
                  </button>
                )}
              </div>

              {isEditing ? (
                <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                  {state.education.map((edu, index) => (
                    <div
                      key={index}
                      style={{
                        padding: "1rem",
                        borderRadius: "0.6rem",
                        border: "1px solid var(--border-blue)",
                        background: "var(--tint-1)",
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
                        <span style={{ fontSize: "0.82rem", fontWeight: 700, color: "var(--primary)" }}>
                          Education #{index + 1}
                        </span>
                        <button
                          type="button"
                          onClick={() => removeEdu(index)}
                          style={{ background: "none", border: "none", color: "var(--error)", cursor: "pointer" }}
                          title="Delete Education"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>

                      <div className="modal-grid-2">
                        <div>
                          <label className="field-label">Degree / Field of Study</label>
                          <input
                            type="text"
                            className="field-value"
                            style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                            value={edu.degree}
                            onChange={(e) => updateEdu(index, "degree", e.target.value)}
                            placeholder="e.g. B.Tech Computer Science"
                          />
                        </div>
                        <div>
                          <label className="field-label">Institution / University</label>
                          <input
                            type="text"
                            className="field-value"
                            style={{ fontWeight: 400, width: "100%", padding: "0.4rem 0.6rem" }}
                            value={edu.institution}
                            onChange={(e) => updateEdu(index, "institution", e.target.value)}
                            placeholder="e.g. Saveetha University"
                          />
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="timeline">
                  {educations.map((edu, index) => (
                    <div
                      className="timeline-item"
                      key={index}
                      style={{
                        padding: "1rem 1.25rem",
                        borderRadius: "0.6rem",
                        background: "var(--tint-1)",
                        border: "1px solid var(--border-blue)",
                        marginBottom: "0.75rem",
                      }}
                    >
                      <h4 style={{ fontSize: "0.95rem", fontWeight: 700, color: "var(--text-main)", margin: 0 }}>
                        {edu.degree || "Degree"}
                      </h4>
                      <p style={{ fontSize: "0.88rem", color: "var(--primary)", marginTop: "0.2rem" }}>
                        {edu.institution || "Institution"} {edu.grade ? ` | Grade: ${edu.grade}` : ""}
                      </p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ---- DYNAMIC ADDITIONAL INFORMATION & CUSTOM CATEGORIES ---- */}
          {profile.additional_info && Object.keys(profile.additional_info).length > 0 && (
            <div style={{ marginBottom: "1.5rem" }}>
              {Object.entries(profile.additional_info).map(([categoryName, categoryValue]) => {
                if (!categoryValue) return null;

                const lowerName = categoryName.toLowerCase();
                if (
                  lowerName === "request_id" ||
                  lowerName === "warnings" ||
                  lowerName === "processing_time_ms" ||
                  lowerName === "pages" ||
                  lowerName.includes("experience_human") ||
                  lowerName === "confidence" ||
                  lowerName === "is_resume"
                ) {
                  return null;
                }

                if (typeof categoryValue === "string") {
                  if (!categoryValue || categoryValue.trim() === "0 months" || categoryValue.trim() === "null") return null;
                }

                if (typeof categoryValue === "object" && !Array.isArray(categoryValue)) {
                  const hasValidField = Object.values(categoryValue).some(
                    (val) => val !== null && val !== "null" && val !== ""
                  );
                  if (!hasValidField) return null;
                }

                const formattedTitle = categoryName.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
                const displayContent =
                  typeof categoryValue === "object"
                    ? Array.isArray(categoryValue)
                      ? categoryValue.map((item) => (typeof item === "object" ? JSON.stringify(item) : String(item))).join(", ")
                      : JSON.stringify(categoryValue, null, 2)
                    : String(categoryValue);

                if (!displayContent || displayContent === "{}" || displayContent === "[]") return null;

                return (
                  <div key={categoryName} style={{ marginBottom: "1rem" }}>
                    <h3 className="section-title" style={{ fontSize: "0.95rem", marginBottom: "0.5rem" }}>
                      <Layers size={16} /> {formattedTitle}
                    </h3>
                    <div
                      style={{
                        padding: "0.85rem 1rem",
                        borderRadius: "0.6rem",
                        background: "var(--tint-1)",
                        border: "1px solid var(--border-blue)",
                        fontSize: "0.88rem",
                        lineHeight: "1.5",
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {displayContent}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="modal-footer" style={{ borderTop: "1px solid var(--border-blue)", paddingTop: "1rem" }}>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={handleDownload}
            disabled={downloading}
          >
            <Download size={16} /> {downloading ? "Downloading..." : "Download Resume PDF"}
          </button>

          {candidate.status !== "verified" && (
            <button
              className="btn btn-secondary"
              style={{ borderColor: "var(--success)", color: "var(--success)" }}
              onClick={() => onVerify(candidate.id)}
              disabled={verifying}
            >
              <CheckCircle2 size={16} /> {verifying ? "Verifying..." : "Verify Profile"}
            </button>
          )}

          {isEditing ? (
            <button className="btn btn-primary" onClick={handleSave} disabled={saving} style={{ background: "var(--primary)", color: "#fff" }}>
              <Save size={16} /> {saving ? "Saving to MongoDB..." : "Save Changes to DB"}
            </button>
          ) : (
            <button className="btn btn-secondary" onClick={() => setIsEditing(true)}>
              <Edit3 size={16} /> Edit Profile
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
