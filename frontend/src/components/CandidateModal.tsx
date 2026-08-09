"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Briefcase,
  Check,
  CheckCircle2,
  Code,
  Copy,
  Download,
  Edit3,
  ExternalLink,
  FileText,
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
  languages: string;
  linkedin: string;
  github: string;
  skills: string;
  work_experience: EditableWorkExp[];
  education: EditableEdu[];
  projects: EditableProject[];
  achievements: string[];
  certifications: string[];
  /** Extracted fields with no slot in the schema — editable, not hard-coded. */
  extras: EditableExtra[];
}

/**
 * One editable row for a field the fixed schema has no slot for.
 *
 * `path` is the location inside `additional_info` ("personal_info.nationality"),
 * so an edited value goes back exactly where it came from. `kind` remembers the
 * original JSON type, so a list stays a list and a number stays a number after
 * a round trip through a text input.
 */
interface EditableExtra {
  path: string;
  label: string;
  value: string;
  kind: "text" | "number" | "boolean" | "list";
}

const EXTRA_PLACEHOLDERS = ["null", "n/a", "none", "0 months", "not specified"];

/**
 * The extractor always emits the duration counters — total/Indian/overseas
 * experience in months and years — and they are 0 for every fresher. Six rows
 * reading "0" say nothing the empty EXPERIENCE section has not already said,
 * so drop them rather than pad the profile with them.
 */
function isZeroDuration(key: string, value: unknown): boolean {
  if (!/experience_(months|years)$/i.test(key)) return false;
  const asNumber = typeof value === "number" ? value : Number(String(value).trim());
  return Number.isFinite(asNumber) && asNumber === 0;
}

/**
 * One label/value pair in the executive view, or nothing when the value is blank.
 *
 * "N/A" against six labels reads as six findings — the reader has to check each
 * one to learn the resume said nothing about any of them. Omitting the row says
 * the same thing at a glance, and it is what the extractor's own viewer does.
 * Both cells are direct children of `.cmv-facts` so the grid keeps its columns.
 */
function Fact({ label, value }: { label: string; value?: string | null }) {
  const text = (value ?? "").trim();
  if (!text || EXTRA_PLACEHOLDERS.includes(text.toLowerCase())) return null;
  return (
    <>
      <div style={{ color: "#64748b" }}>{label}</div>
      <div style={{ fontWeight: 700, color: "#0f172a" }}>{text}</div>
    </>
  );
}

/** "date_of_birth" -> "Date of birth" — readable without a per-field label map. */
function humanizeKey(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1).toLowerCase();
}

/** Render one extracted value as text. Returns "" for anything empty. */
function formatExtraValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") {
    const trimmed = value.trim();
    // The extractor uses these as "nothing here"; showing them is worse than
    // showing nothing at all.
    return ["null", "n/a", "none", "0 months"].includes(trimmed.toLowerCase()) ? "" : trimmed;
  }
  if (Array.isArray(value)) {
    return value.map(formatExtraValue).filter(Boolean).join(", ");
  }
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => {
        const text = formatExtraValue(v);
        return text ? `${humanizeKey(k)}: ${text}` : "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

/**
 * Flatten `additional_info` into label/value rows.
 *
 * The extractor returns more than `CandidateProfile` defines — industry,
 * highest qualification, nationality, passport blocks — and everything without
 * a field lands here. Rendering it from the data means a resume carrying a
 * field nobody anticipated still shows up, instead of being visible only in
 * the raw OCR dump.
 */
/** Label a nested path: "personal_info.date_of_birth" -> "Personal info · Date of birth". */
function labelForPath(path: string): string {
  return path.split(".").map(humanizeKey).join(" · ");
}

/**
 * Walk `additional_info` down to its leaves, one editable row per leaf.
 *
 * Recursing rather than reading the top level keeps nested blocks — the
 * `personal_info` and `passport_details` the extractor returns — editable
 * field by field instead of as a wall of JSON. Arrays of objects are skipped:
 * those are experience/education/projects shaped, and the sections above own
 * them.
 */
function extrasForEdit(value: unknown, path: string, out: EditableExtra[]): void {
  if (value === null || value === undefined) return;

  if (Array.isArray(value)) {
    const flat = value.filter((v) => typeof v === "string" || typeof v === "number");
    if (flat.length === value.length && flat.length > 0) {
      out.push({ path, label: labelForPath(path), value: flat.join(", "), kind: "list" });
    }
    return;
  }

  if (typeof value === "object") {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      extrasForEdit(child, path ? `${path}.${key}` : key, out);
    }
    return;
  }

  if (typeof value === "boolean") {
    out.push({ path, label: labelForPath(path), value: value ? "Yes" : "No", kind: "boolean" });
    return;
  }

  if (typeof value === "number") {
    if (isZeroDuration(path.split(".").pop() ?? "", value)) return;
    out.push({ path, label: labelForPath(path), value: String(value), kind: "number" });
    return;
  }

  const text = String(value).trim();
  if (!text || EXTRA_PLACEHOLDERS.includes(text.toLowerCase())) return;
  out.push({ path, label: labelForPath(path), value: text, kind: "text" });
}

/** Rebuild the nested `additional_info` object from the edited rows. */
function extrasToObject(extras: EditableExtra[]): Record<string, unknown> {
  const root: Record<string, unknown> = {};

  for (const extra of extras) {
    const text = extra.value.trim();
    // An emptied field is a deletion — writing "" back would resurrect the
    // blank rows the readable view exists to hide.
    if (!text) continue;

    let parsed: unknown = text;
    if (extra.kind === "list") {
      parsed = text.split(",").map((s) => s.trim()).filter(Boolean);
    } else if (extra.kind === "number") {
      const asNumber = Number(text);
      parsed = Number.isFinite(asNumber) ? asNumber : text;
    } else if (extra.kind === "boolean") {
      parsed = ["yes", "true", "1"].includes(text.toLowerCase());
    }

    const segments = extra.path.split(".").filter(Boolean);
    if (segments.length === 0) continue;

    let node = root;
    for (const segment of segments.slice(0, -1)) {
      if (typeof node[segment] !== "object" || node[segment] === null || Array.isArray(node[segment])) {
        node[segment] = {};
      }
      node = node[segment] as Record<string, unknown>;
    }
    node[segments[segments.length - 1]] = parsed;
  }

  return root;
}

function flattenExtras(extra: Record<string, unknown>, skip: string[]): [string, string][] {
  const skipped = new Set(skip);
  return Object.entries(extra)
    .filter(([key]) => !skipped.has(key))
    .filter(([key, value]) => !isZeroDuration(key, value))
    .map(([key, value]) => [humanizeKey(key), formatExtraValue(value)] as [string, string])
    .filter(([, value]) => value.length > 0);
}

function toEditableState(profile: CandidateProfile, candidate?: CandidateRecord): EditableState {
  const rawData = candidate?.raw_ocr || profile.raw_ocr;
  const rawExp = rawData?.experience || rawData?.work_experience || [];
  const rawEdu = rawData?.education || [];
  const rawProj = rawData?.projects || [];
  const rawAch = rawData?.achievements || rawData?.awards || [];
  const rawCert = rawData?.certifications || rawData?.certificates || [];

  const experiences = (profile.work_experience && profile.work_experience.length > 0)
    ? profile.work_experience
    : (Array.isArray(rawExp) ? rawExp.map((w: any) => ({
        company: w.company || "",
        designation: w.designation || w.role || w.title || "",
        start_date: w.start_date || "",
        end_date: w.end_date || "",
        location: w.location || "",
        description: w.description || "",
      })) : []);

  const educations = (profile.education && profile.education.length > 0)
    ? profile.education
    : (Array.isArray(rawEdu) ? rawEdu.map((e: any) => ({
        institution: e.institution || e.school || e.university || "",
        degree: e.degree || e.qualification || "",
        field_of_study: e.field_of_study || e.major || "",
        start_date: e.start_date || "",
        end_date: e.end_date || "",
        grade: e.grade || e.gpa || "",
      })) : []);

  const projects = (profile.projects && profile.projects.length > 0)
    ? profile.projects
    : (Array.isArray(rawProj) ? rawProj.map((p: any) => ({
        name: p.name || p.title || "",
        description: p.description || "",
        technologies: Array.isArray(p.technologies) ? p.technologies : [],
      })) : []);

  const achievements = (profile.achievements && profile.achievements.length > 0)
    ? profile.achievements
    : (Array.isArray(rawAch) ? rawAch.map((a: any) => typeof a === "object" ? (a.title || a.name || JSON.stringify(a)) : String(a)) : []);

  const certifications = (profile.certifications && profile.certifications.length > 0)
    ? profile.certifications
    : (Array.isArray(rawCert) ? rawCert.map((c: any) => typeof c === "object" ? (c.title || c.name || JSON.stringify(c)) : String(c)) : []);

  const languages = (profile.languages && profile.languages.length > 0)
    ? profile.languages
    : (Array.isArray(rawData?.languages) ? rawData.languages : []);

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
    languages: languages.length > 0 ? languages.join(", ") : "",
    linkedin: profile.linkedin_url ?? "",
    github: profile.github_url ?? "",
    skills: (profile.skills ?? []).join(", "),
    work_experience: experiences.map((exp) => {
      const companyVal = (exp.company ?? "").trim();
      const cleanCompany =
        companyVal.toLowerCase() === "company" || companyVal.toLowerCase() === "n/a" ? "" : companyVal;
      return {
        company: cleanCompany,
        designation: exp.designation || (exp as any).title || "",
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
    certifications: certifications,
    extras: (() => {
      const rows: EditableExtra[] = [];
      extrasForEdit(profile.additional_info ?? {}, "", rows);
      return rows;
    })(),
  };
}

function RawOcrViewer({ candidate }: { candidate: CandidateRecord }) {
  const [activePage, setActivePage] = useState<number>(0);
  const [copied, setCopied] = useState<boolean>(false);
  const rawData = candidate.raw_ocr || candidate.profile?.raw_ocr;

  const jsonString = useMemo(() => {
    if (rawData) {
      return JSON.stringify(rawData, null, 2);
    }
    const fallbackObj = {
      note: "Raw OCR JSON payload from MongoDB Atlas",
      filename: candidate.resume?.original_filename || "resume",
      extraction_method: candidate.resume?.extraction_method || "unknown",
      ocr_used: candidate.resume?.ocr_used || false,
      profile: candidate.profile,
    };
    return JSON.stringify(fallbackObj, null, 2);
  }, [rawData, candidate]);

  const handleCopyJson = () => {
    if (!jsonString) return;
    navigator.clipboard.writeText(jsonString).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {
      // Fallback if clipboard API is blocked in non-secure context
      const textarea = document.createElement("textarea");
      textarea.value = jsonString;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  const pages = (rawData?.pages as any[]) || [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {/* Pages selector tabs if multi-page OCR document */}
      {pages.length > 1 && (
        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
          {pages.map((p, idx) => (
            <button
              key={idx}
              type="button"
              onClick={() => setActivePage(idx)}
              style={{
                padding: "0.4rem 0.85rem",
                borderRadius: "0.4rem",
                fontSize: "0.82rem",
                fontWeight: 600,
                border: activePage === idx ? "1px solid #2563eb" : "1px solid #dbeafe",
                background: activePage === idx ? "#2563eb" : "#eff6ff",
                color: activePage === idx ? "#ffffff" : "#1e40af",
                cursor: "pointer",
              }}
            >
              Page {p.page_number || idx + 1} ({p.lines?.length || 0} lines)
            </button>
          ))}
        </div>
      )}

      {/* Page detail summary card if pages available */}
      {pages.length > 0 && pages[activePage] && (
        <div style={{
          padding: "0.85rem 1rem",
          background: "#f0f7ff",
          borderRadius: "0.5rem",
          border: "1px solid #dbeafe",
          fontSize: "0.85rem",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem", fontWeight: 600, color: "#1e40af" }}>
            <span>Page {pages[activePage].page_number || activePage + 1} Metadata</span>
            {pages[activePage].average_confidence !== undefined && (
              <span>Avg Confidence: {((pages[activePage].average_confidence || 0) * 100).toFixed(1)}%</span>
            )}
          </div>
          {pages[activePage].text && (
            <div style={{ marginBottom: "0.5rem" }}>
              <strong style={{ display: "block", marginBottom: "0.25rem", color: "#0f172a" }}>Raw Page Text:</strong>
              <div style={{
                maxHeight: "140px",
                overflowY: "auto",
                background: "#ffffff",
                padding: "0.6rem 0.75rem",
                borderRadius: "0.4rem",
                whiteSpace: "pre-wrap",
                fontFamily: "monospace",
                fontSize: "0.8rem",
                border: "1px solid #dbeafe",
                color: "#1e293b",
              }}>
                {pages[activePage].text}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Raw JSON viewer block */}
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
          <span style={{ fontSize: "0.88rem", fontWeight: 600, color: "#0f172a" }}>
            Full JSON Payload (MongoDB Atlas Document)
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
            <button
              type="button"
              onClick={handleCopyJson}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "0.4rem",
                padding: "0.35rem 0.75rem",
                borderRadius: "0.4rem",
                fontSize: "0.8rem",
                fontWeight: 600,
                border: copied ? "1px solid #16a34a" : "1px solid #2563eb",
                background: copied ? "#f0fdf4" : "#eff6ff",
                color: copied ? "#15803d" : "#2563eb",
                cursor: "pointer",
                transition: "all 0.2s ease",
              }}
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
              {copied ? "Copied!" : "Copy JSON"}
            </button>
            <span style={{ fontSize: "0.78rem", color: "#64748b" }}>
              {jsonString.length.toLocaleString()} bytes
            </span>
          </div>
        </div>
        <pre style={{
          maxHeight: "440px",
          overflow: "auto",
          background: "#0f172a",
          color: "#e2e8f0",
          padding: "1rem 1.25rem",
          borderRadius: "0.6rem",
          fontSize: "0.82rem",
          lineHeight: "1.55",
          fontFamily: "ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace",
          border: "1px solid #334155",
          boxShadow: "inset 0 2px 8px rgba(0,0,0,0.3)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}>
          <code>{jsonString}</code>
        </pre>
      </div>
    </div>
  );
}

function BulletText({ text }: { text: string }) {
  if (!text) return null;
  const lines = text
    .split(/\r?\n|•/)
    .map((l) => l.trim().replace(/^[\-\*\u2022\u25cf\s]+/, ""))
    .filter(Boolean);

  if (lines.length <= 1) {
    return <div style={{ fontSize: "0.9rem", color: "#334155", lineHeight: "1.6" }}>{text}</div>;
  }

  return (
    <ul className="cmv-bullet-list">
      {lines.map((item, idx) => (
        <li key={idx}>
          {item}
        </li>
      ))}
    </ul>
  );
}

function PageTextViewer({ candidate }: { candidate: CandidateRecord }) {
  const rawData = candidate.raw_ocr || candidate.profile?.raw_ocr;
  const pages = (rawData?.pages as any[]) || [];

  if (pages.length === 0) {
    return (
      <div style={{ padding: "1.5rem", background: "#f0f7ff", borderRadius: "0.6rem", border: "1px solid #dbeafe", color: "#64748b", fontSize: "0.9rem" }}>
        No individual page OCR details recorded for this candidate.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {pages.map((p, idx) => (
        <div key={idx} style={{ background: "#ffffff", borderRadius: "0.6rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.04)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.75rem", fontWeight: 700, color: "#1e40af", fontSize: "0.95rem" }}>
            <span>Page {p.page_number || idx + 1}</span>
            {p.average_confidence !== undefined && (
              <span style={{ fontSize: "0.8rem", color: "#64748b", fontWeight: 500 }}>
                Confidence: {((p.average_confidence || 0) * 100).toFixed(1)}%
              </span>
            )}
          </div>
          <div style={{
            maxHeight: "220px",
            overflowY: "auto",
            background: "#f8fafc",
            padding: "0.85rem 1rem",
            borderRadius: "0.5rem",
            whiteSpace: "pre-wrap",
            fontFamily: "monospace",
            fontSize: "0.85rem",
            color: "#1e293b",
            lineHeight: 1.6,
            border: "1px solid #e2e8f0",
          }}>
            {p.text || "No text extracted for this page."}
          </div>
        </div>
      ))}
    </div>
  );
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
  const profile = candidate.profile || {};

  const [isEditing, setIsEditing] = useState(Boolean(initialEditMode));
  const [downloading, setDownloading] = useState(false);
  const [state, setState] = useState<EditableState>(() => toEditableState(profile, candidate));
  const tabsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setState(toEditableState(candidate.profile || {}, candidate));
  }, [candidate]);

  const updateRoot = (field: keyof EditableState) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => {
    setState((prev) => ({ ...prev, [field]: e.target.value }));
  };

  const handleSave = () => {
    const merged: CandidateProfile = {
      ...profile,
      full_name: state.full_name,
      current_designation: state.designation,
      resume_summary: state.summary,
      email: state.email,
      phone: state.phone,
      location: state.location,
      total_experience_years: parseFloat(state.experience) || profile.total_experience_years || 0,
      languages: state.languages.split(",").map((s) => s.trim()).filter(Boolean),
      linkedin_url: state.linkedin,
      github_url: state.github,
      skills: state.skills.split(",").map((s) => s.trim()).filter(Boolean),
      work_experience: state.work_experience.map((w) => ({
        company: w.company,
        designation: w.designation,
        title: w.designation,
        start_date: w.start_date,
        end_date: w.end_date,
        location: w.location,
        description: w.description,
      })),
      education: state.education,
      projects: state.projects.map((p) => ({
        name: p.name,
        description: p.description,
        technologies: p.technologies.split(",").map((s) => s.trim()).filter(Boolean),
      })),
      achievements: state.achievements.filter((a) => a.trim() !== ""),
      certifications: state.certifications.filter((c) => c.trim() !== ""),
      // Rebuilt from the edited rows rather than carried over from `...profile`,
      // so edits to extracted fields persist and cleared ones are removed.
      additional_info: extrasToObject(state.extras),
    };
    onSave(candidate.id, merged);
    setIsEditing(false);
  };

  const handleDownload = async () => {
    try {
      setDownloading(true);
      const token = getToken();
      const res = await fetch(resumeDownloadUrl(candidate.id), {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error("Download failed");
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = candidate.resume?.original_filename || "resume.pdf";
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      alert("Could not download resume file.");
    } finally {
      setDownloading(false);
    }
  };

  const addWorkExp = () =>
    setState((prev) => ({
      ...prev,
      work_experience: [
        ...prev.work_experience,
        { company: "", designation: "", start_date: "", end_date: "", location: "", description: "" },
      ],
    }));

  const removeWorkExp = (index: number) =>
    setState((prev) => ({
      ...prev,
      work_experience: prev.work_experience.filter((_, i) => i !== index),
    }));

  const updateWorkExp = (index: number, field: keyof EditableWorkExp, value: string) =>
    setState((prev) => {
      const list = [...prev.work_experience];
      list[index] = { ...list[index], [field]: value };
      return { ...prev, work_experience: list };
    });

  const addEdu = () =>
    setState((prev) => ({
      ...prev,
      education: [...prev.education, { degree: "", institution: "", start_date: "", end_date: "", grade: "" }],
    }));

  const removeEdu = (index: number) =>
    setState((prev) => ({
      ...prev,
      education: prev.education.filter((_, i) => i !== index),
    }));

  const updateEdu = (index: number, field: keyof EditableEdu, value: string) =>
    setState((prev) => {
      const list = [...prev.education];
      list[index] = { ...list[index], [field]: value };
      return { ...prev, education: list };
    });

  const addProj = () =>
    setState((prev) => ({
      ...prev,
      projects: [...prev.projects, { name: "", description: "", technologies: "" }],
    }));

  const removeProj = (index: number) =>
    setState((prev) => ({
      ...prev,
      projects: prev.projects.filter((_, i) => i !== index),
    }));

  const updateProj = (index: number, field: keyof EditableProject, value: string) =>
    setState((prev) => {
      const list = [...prev.projects];
      list[index] = { ...list[index], [field]: value };
      return { ...prev, projects: list };
    });

  const addAch = () =>
    setState((prev) => ({
      ...prev,
      achievements: [...prev.achievements, ""],
    }));

  const removeAch = (index: number) =>
    setState((prev) => ({
      ...prev,
      achievements: prev.achievements.filter((_, i) => i !== index),
    }));

  const updateAch = (index: number, value: string) =>
    setState((prev) => {
      const list = [...prev.achievements];
      list[index] = value;
      return { ...prev, achievements: list };
    });

  const addCert = () =>
    setState((prev) => ({
      ...prev,
      certifications: [...prev.certifications, ""],
    }));

  const removeCert = (index: number) =>
    setState((prev) => ({
      ...prev,
      certifications: prev.certifications.filter((_, i) => i !== index),
    }));

  const updateCert = (index: number, value: string) =>
    setState((prev) => {
      const list = [...prev.certifications];
      list[index] = value;
      return { ...prev, certifications: list };
    });

  const addExtra = () =>
    setState((prev) => ({
      ...prev,
      extras: [...prev.extras, { path: "", label: "", value: "", kind: "text" }],
    }));

  const removeExtra = (index: number) =>
    setState((prev) => ({ ...prev, extras: prev.extras.filter((_, i) => i !== index) }));

  const updateExtraValue = (index: number, value: string) =>
    setState((prev) => {
      const list = [...prev.extras];
      list[index] = { ...list[index], value };
      return { ...prev, extras: list };
    });

  /**
   * Renaming a field rewrites where it is stored. The label is what the user
   * types; the path is derived from it, keeping the parent so a renamed nested
   * field stays inside its block ("Personal info · Nationality" continues to
   * live under personal_info).
   */
  const updateExtraLabel = (index: number, label: string) =>
    setState((prev) => {
      const list = [...prev.extras];
      const current = list[index];
      const parent = current.path.includes(".")
        ? current.path.slice(0, current.path.lastIndexOf("."))
        : "";
      const leaf = label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
      list[index] = { ...current, label, path: leaf ? (parent ? `${parent}.${leaf}` : leaf) : "" };
      return { ...prev, extras: list };
    });

  const scrollTabsLeft = () => {
    if (tabsRef.current) {
      tabsRef.current.scrollBy({ left: -200, behavior: "smooth" });
    }
  };

  const scrollTabsRight = () => {
    if (tabsRef.current) {
      tabsRef.current.scrollBy({ left: 200, behavior: "smooth" });
    }
  };

  const displayName = state.full_name || profile.full_name || candidate.source_email?.from_name || "Candidate Profile";
  const displayDesignation = state.designation || profile.current_designation || "";

  const experiences = profile.work_experience ?? [];
  const projects = profile.projects ?? [];

  const parsedSkills = state.skills
    .split(",")
    .map((s) => s.trim().replace(/\.$/, ""))
    .filter((s) => Boolean(s));

  const [subTab, setSubTab] = useState<
    "readable" | "experience" | "skills" | "projects" | "education" | "achievements" | "certifications" | "pages" | "raw_ocr"
  >("readable");

  const showAll = subTab === "readable";

  // Veris returns these two, but `CandidateProfile` has no field for either, so
  // they land in `additional_info` and were never rendered.
  const extra = (profile.additional_info ?? {}) as Record<string, unknown>;
  const industry = typeof extra.industry === "string" ? extra.industry : "";

  /**
   * Prefer what the extractor concluded. Falling back to `education[0]` alone
   * reported "10th Grade" for a candidate holding a B.E., because the list is
   * in chronological order and the first entry is the earliest schooling — so
   * the last entry, not the first, is the closest local guess.
   */
  const highestQualification =
    (typeof extra.highest_qualification === "string" && extra.highest_qualification) ||
    profile.education?.[profile.education.length - 1]?.degree ||
    profile.education?.[0]?.degree ||
    "";

  // Everything else the extractor returned, flattened into label/value rows.
  // Driven by the data rather than a hard-coded list, so a field we have never
  // seen before still reaches the screen instead of being silently dropped.
  const additionalEntries = flattenExtras(extra, ["industry", "highest_qualification"]);

  /**
   * Which sections the readable view renders.
   *
   * A section the resume said nothing about is noise: "No experience records
   * available." tells the reader less than the section simply not being there.
   * So on the readable tab a section appears only once it has content.
   *
   * Two deliberate exceptions:
   *  - while editing, every section stays visible, otherwise there is nowhere
   *    to click "+ Add Experience" for a resume that arrived without any;
   *  - opening a section's own tab shows it regardless — the user asked for
   *    that section by name, and an empty panel answers the question.
   */
  const sectionVisible = (tab: string, hasContent: boolean) =>
    subTab === tab || (showAll && (isEditing || hasContent));

  return (
    <div
      className="modal-overlay active"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="modal-container"
        role="dialog"
        aria-modal="true"
        style={{
          maxWidth: "1050px",
          background: "#ffffff",
          color: "#0f172a",
          display: "flex",
          flexDirection: "column",
          maxHeight: "90vh",
          borderRadius: "0.75rem",
          boxShadow: "0 20px 30px -10px rgba(37, 99, 235, 0.15), 0 8px 10px -6px rgba(0, 0, 0, 0.05)",
          border: "1px solid #dbeafe",
        }}
      >
        {/* WHITE & FASCINATING BLUE TOP HEADER BAR */}
        <div
          className="modal-header cmv-headerbar"
          style={{
            borderBottom: "1px solid #e2e8f0",
            background: "#ffffff",
            borderTopLeftRadius: "0.75rem",
            borderTopRightRadius: "0.75rem",
          }}
        >
          <div className="cmv-header-title-box">
            <div style={{ minWidth: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: "0.55rem", flexWrap: "wrap" }}>
                <h2 className="page-title" style={{ fontSize: "1.35rem", fontWeight: 700, margin: 0, color: "#1e40af" }}>
                  Resume Result
                </h2>
                <span
                  className={`badge ${candidate.status === "verified" ? "badge-verified" : "badge-active"}`}
                  style={{ fontSize: "0.75rem", background: candidate.status === "verified" ? "rgba(16,185,129,0.12)" : "rgba(37,99,235,0.12)", color: candidate.status === "verified" ? "#059669" : "#2563eb", fontWeight: 700 }}
                >
                  {candidate.status || "Ingested"}
                </span>
              </div>
              <div style={{ fontSize: "0.8rem", color: "#64748b", marginTop: "0.2rem", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {candidate.resume?.original_filename || "temp_ocr.pdf"} · {new Date(candidate.created_at || Date.now()).toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" })}
              </div>
            </div>
            <button className="modal-close" onClick={onClose} aria-label="Close profile" style={{ background: "#f8fafc", border: "1px solid #e2e8f0", borderRadius: "0.375rem", padding: "0.4rem", cursor: "pointer", color: "#64748b" }}>
              <X size={18} />
            </button>
          </div>

          {/* VIEW MODES & TOP ACTION CONTROLS */}
          <div className="cmv-header-actions">
            <button
              type="button"
              className={`btn ${subTab === "readable" && !isEditing ? "btn-primary" : "btn-secondary"}`}
              onClick={() => { setSubTab("readable"); setIsEditing(false); }}
              style={{
                fontSize: "0.82rem",
                padding: "0.45rem 0.85rem",
                background: subTab === "readable" && !isEditing ? "linear-gradient(135deg, #1e40af, #2563eb)" : "#eff6ff",
                color: subTab === "readable" && !isEditing ? "#ffffff" : "#1e40af",
                border: "1px solid #dbeafe",
                fontWeight: 600,
              }}
            >
              <FileText size={14} /> Executive
            </button>

            <button
              type="button"
              className={`btn ${isEditing ? "btn-primary" : "btn-secondary"}`}
              onClick={() => setIsEditing(!isEditing)}
              style={{
                fontSize: "0.82rem",
                padding: "0.45rem 0.85rem",
                background: isEditing ? "#2563eb" : "#eff6ff",
                color: isEditing ? "#ffffff" : "#1e40af",
                border: "1px solid #dbeafe",
                fontWeight: 600,
              }}
            >
              <Edit3 size={14} /> {isEditing ? "Done" : "Edit"}
            </button>

            <button
              type="button"
              className={`btn ${subTab === "raw_ocr" ? "btn-primary" : "btn-secondary"}`}
              onClick={() => setSubTab("raw_ocr")}
              style={{
                fontSize: "0.82rem",
                padding: "0.45rem 0.85rem",
                background: subTab === "raw_ocr" ? "linear-gradient(135deg, #1e40af, #2563eb)" : "#eff6ff",
                color: subTab === "raw_ocr" ? "#ffffff" : "#1e40af",
                border: "1px solid #dbeafe",
                fontWeight: 600,
              }}
            >
              <Code size={14} /> Raw JSON
            </button>
          </div>
        </div>

        {/* WHITE THEME MODAL BODY (SCROLLABLE) */}
        <div className="modal-body cmv-body" style={{ overflowY: "auto", overflowX: "hidden", flex: 1, background: "#ffffff" }}>

          {/* VERIS MATCHING HORIZONTAL NAVIGATION BAR WITH BLUE / ORANGE SLIDER & SCROLL ARROWS */}
          <div style={{ position: "relative", marginBottom: "1.5rem", background: "#f0f7ff", padding: "0.6rem 0.8rem", borderRadius: "0.75rem", border: "1px solid #dbeafe" }}>
            <div
              ref={tabsRef}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.4rem",
                overflowX: "auto",
                scrollbarWidth: "none",
                msOverflowStyle: "none",
                paddingBottom: "0.4rem",
              }}
            >
              {[
                { id: "readable", label: "Readable" },
                { id: "experience", label: "Experience" },
                { id: "skills", label: "Skills" },
                { id: "projects", label: "Projects" },
                { id: "education", label: "Education" },
                { id: "achievements", label: "Achievements" },
                { id: "certifications", label: "Certifications" },
                { id: "pages", label: "Pages" },
                { id: "raw_ocr", label: "Raw JSON" },
              ].map((t) => {
                const isActive = subTab === t.id;
                return (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => setSubTab(t.id as any)}
                    style={{
                      padding: "0.45rem 1rem",
                      fontSize: "0.88rem",
                      fontWeight: isActive ? 700 : 500,
                      color: isActive ? "#ffffff" : "#475569",
                      background: isActive ? "linear-gradient(135deg, #1e40af, #2563eb)" : "transparent",
                      borderRadius: "0.5rem",
                      border: "none",
                      cursor: "pointer",
                      whiteSpace: "nowrap",
                      transition: "all 0.15s ease",
                      boxShadow: isActive ? "0 2px 6px rgba(37,99,235,0.3)" : "none",
                    }}
                  >
                    {t.label}
                  </button>
                );
              })}
            </div>

            {/* FASCINATING BLUE SLIDER LINE WITH LEFT & RIGHT ARROWS */}
            <div style={{ display: "flex", alignItems: "center", gap: "0.3rem", marginTop: "0.3rem" }}>
              <button
                type="button"
                onClick={scrollTabsLeft}
                aria-label="Scroll tabs left"
                style={{
                  background: "none",
                  border: "none",
                  color: "#2563eb",
                  fontSize: "0.75rem",
                  cursor: "pointer",
                  padding: "0 0.2rem",
                  lineHeight: 1,
                  fontWeight: 800,
                }}
              >
                ◄
              </button>
              <div style={{ flex: 1, height: "3px", background: "linear-gradient(90deg, #2563eb, #3b82f6)", borderRadius: "2px" }} />
              <button
                type="button"
                onClick={scrollTabsRight}
                aria-label="Scroll tabs right"
                style={{
                  background: "none",
                  border: "none",
                  color: "#2563eb",
                  fontSize: "0.75rem",
                  cursor: "pointer",
                  padding: "0 0.2rem",
                  lineHeight: 1,
                  fontWeight: 800,
                }}
              >
                ►
              </button>
            </div>
          </div>

          {/* SUBDIVISION CONTENT PANELS - FASCINATING BLUE & WHITE THEME */}
          {subTab === "raw_ocr" ? (
            <RawOcrViewer candidate={candidate} />
          ) : subTab === "pages" ? (
            <PageTextViewer candidate={candidate} />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "1.75rem" }}>
              {/* CANDIDATE DETAILS */}
              {showAll && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      CANDIDATE DETAILS
                    </h3>
                    <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                      {isEditing ? "Done" : "Edit Section"}
                    </button>
                  </div>

                  {isEditing ? (
                    <div className="cmv-form-2">
                      <div>
                        <label style={{ fontSize: "0.8rem", color: "#64748b", fontWeight: 600 }}>Name</label>
                        <input type="text" value={state.full_name} onChange={updateRoot("full_name")} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                      </div>
                      <div>
                        <label style={{ fontSize: "0.8rem", color: "#64748b", fontWeight: 600 }}>Designation</label>
                        <input type="text" value={state.designation} onChange={updateRoot("designation")} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                      </div>
                      <div>
                        <label style={{ fontSize: "0.8rem", color: "#64748b", fontWeight: 600 }}>Email</label>
                        <input type="text" value={state.email} onChange={updateRoot("email")} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                      </div>
                      <div>
                        <label style={{ fontSize: "0.8rem", color: "#64748b", fontWeight: 600 }}>Phone</label>
                        <input type="text" value={state.phone} onChange={updateRoot("phone")} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                      </div>
                      <div>
                        <label style={{ fontSize: "0.8rem", color: "#64748b", fontWeight: 600 }}>Address / Location</label>
                        <input type="text" value={state.location} onChange={updateRoot("location")} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                      </div>
                    </div>
                  ) : (
                    <div className="cmv-facts">
                      <Fact label="Name" value={displayName} />
                      <Fact label="Industry" value={industry} />
                      <Fact label="Highest qualification" value={highestQualification} />
                      <Fact label="Email" value={state.email} />
                      <Fact label="Phone" value={state.phone} />
                      <Fact label="LinkedIn" value={state.linkedin} />
                      <Fact label="GitHub" value={state.github} />
                      <Fact label="Address" value={state.location} />
                    </div>
                  )}
                </div>
              )}

              {/* PERSONAL INFORMATION */}
              {showAll && (isEditing || Boolean(state.languages.trim())) && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      PERSONAL INFORMATION
                    </h3>
                    <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                      {isEditing ? "Done" : "Edit Section"}
                    </button>
                  </div>

                  {isEditing ? (
                    <div>
                      <label style={{ fontSize: "0.8rem", color: "#64748b", fontWeight: 600 }}>Languages (comma separated)</label>
                      <input type="text" value={state.languages} onChange={updateRoot("languages")} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                    </div>
                  ) : (
                    <div className="cmv-facts">
                      <Fact label="Languages" value={state.languages} />
                    </div>
                  )}
                </div>
              )}

              {/* EXPERIENCE */}
              {sectionVisible("experience", state.work_experience.length > 0) && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      EXPERIENCE
                    </h3>
                    <div style={{ display: "flex", gap: "0.5rem" }}>
                      {isEditing && (
                        <button type="button" className="btn btn-secondary" onClick={addWorkExp} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                          + Add Experience
                        </button>
                      )}
                      <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                        {isEditing ? "Done Editing" : "Edit Section"}
                      </button>
                    </div>
                  </div>

                  {isEditing ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                      {state.work_experience.map((exp, idx) => (
                        <div key={idx} style={{ background: "#ffffff", borderRadius: "0.6rem", padding: "1rem", border: "1px solid #dbeafe" }}>
                          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
                            <strong style={{ fontSize: "0.85rem", color: "#1e40af" }}>Experience #{idx + 1}</strong>
                            <button type="button" onClick={() => removeWorkExp(idx)} style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer" }}>
                              <Trash2 size={14} />
                            </button>
                          </div>
                          <div className="cmv-form-2" style={{ marginBottom: "0.6rem" }}>
                            <input type="text" placeholder="Designation" value={exp.designation} onChange={(e) => updateWorkExp(idx, "designation", e.target.value)} style={{ padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                            <input type="text" placeholder="Company Name" value={exp.company} onChange={(e) => updateWorkExp(idx, "company", e.target.value)} style={{ padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                          </div>
                          <textarea rows={3} placeholder="Work Description..." value={exp.description} onChange={(e) => updateWorkExp(idx, "description", e.target.value)} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                        </div>
                      ))}
                    </div>
                  ) : (
                    state.work_experience.length > 0 ? (
                      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                        {state.work_experience.map((exp, idx) => (
                          <div key={idx} style={{ background: "#ffffff", borderRadius: "0.6rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 1px 3px rgba(37,99,235,0.05)" }}>
                            <div style={{ fontSize: "1.05rem", fontWeight: 700, color: "#0f172a", marginBottom: "0.25rem" }}>
                              {exp.designation} {exp.company ? `· ${exp.company}` : ""}
                            </div>
                            {/* No dates means the extractor found none. "0 months"
                                reads as a measured duration; a blank row is honest. */}
                            {(exp.start_date || exp.end_date) && (
                              <div style={{ fontSize: "0.85rem", color: "#2563eb", fontWeight: 600, marginBottom: "0.75rem" }}>
                                {[exp.start_date, exp.end_date].filter(Boolean).join(" – ")}
                              </div>
                            )}
                            {exp.description && (
                              <BulletText text={exp.description} />
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div style={{ color: "#64748b", fontSize: "0.9rem" }}>No experience records available.</div>
                    )
                  )}
                </div>
              )}

              {/* SKILLS */}
              {sectionVisible("skills", parsedSkills.length > 0) && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      SKILLS
                    </h3>
                    <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                      {isEditing ? "Done Editing" : "Edit Section"}
                    </button>
                  </div>

                  {isEditing ? (
                    <textarea rows={4} value={state.skills} onChange={updateRoot("skills")} style={{ width: "100%", padding: "0.75rem", borderRadius: "0.5rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} placeholder="Skills (comma separated)..." />
                  ) : (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: "0.6rem" }}>
                      {parsedSkills.map((skill, idx) => (
                        <span key={idx} style={{ background: "#eff6ff", border: "1px solid #dbeafe", color: "#1e40af", borderRadius: "9999px", padding: "0.4rem 0.9rem", fontSize: "0.85rem", fontWeight: 600, boxShadow: "0 1px 2px rgba(37,99,235,0.05)" }}>
                          {skill}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* PROJECTS */}
              {sectionVisible("projects", state.projects.length > 0) && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      PROJECTS
                    </h3>
                    <div style={{ display: "flex", gap: "0.5rem" }}>
                      {isEditing && (
                        <button type="button" className="btn btn-secondary" onClick={addProj} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                          + Add Project
                        </button>
                      )}
                      <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                        {isEditing ? "Done Editing" : "Edit Section"}
                      </button>
                    </div>
                  </div>

                  {isEditing ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                      {state.projects.map((proj, idx) => (
                        <div key={idx} style={{ background: "#ffffff", borderRadius: "0.6rem", padding: "1rem", border: "1px solid #dbeafe" }}>
                          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
                            <strong style={{ fontSize: "0.85rem", color: "#1e40af" }}>Project #{idx + 1}</strong>
                            <button type="button" onClick={() => removeProj(idx)} style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer" }}>
                              <Trash2 size={14} />
                            </button>
                          </div>
                          <input type="text" placeholder="Project Name" value={proj.name} onChange={(e) => updateProj(idx, "name", e.target.value)} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a", marginBottom: "0.5rem" }} />
                          <textarea rows={3} placeholder="Project Description..." value={proj.description} onChange={(e) => updateProj(idx, "description", e.target.value)} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                      {state.projects.map((proj, idx) => (
                        <div key={idx} style={{ background: "#ffffff", borderRadius: "0.6rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 1px 3px rgba(37,99,235,0.05)" }}>
                          <div style={{ fontSize: "1.05rem", fontWeight: 700, color: "#0f172a", marginBottom: "0.5rem" }}>
                            {proj.name}
                          </div>
                          {proj.description && (
                            <BulletText text={proj.description} />
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* EDUCATION */}
              {sectionVisible("education", state.education.length > 0) && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      EDUCATION
                    </h3>
                    <div style={{ display: "flex", gap: "0.5rem" }}>
                      {isEditing && (
                        <button type="button" className="btn btn-secondary" onClick={addEdu} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                          + Add Education
                        </button>
                      )}
                      <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                        {isEditing ? "Done Editing" : "Edit Section"}
                      </button>
                    </div>
                  </div>

                  {isEditing ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                      {state.education.map((edu, idx) => (
                        <div key={idx} style={{ background: "#ffffff", borderRadius: "0.6rem", padding: "1rem", border: "1px solid #dbeafe" }}>
                          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
                            <strong style={{ fontSize: "0.85rem", color: "#1e40af" }}>Education #{idx + 1}</strong>
                            <button type="button" onClick={() => removeEdu(idx)} style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer" }}>
                              <Trash2 size={14} />
                            </button>
                          </div>
                          <input type="text" placeholder="Degree / Qualification" value={edu.degree} onChange={(e) => updateEdu(idx, "degree", e.target.value)} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a", marginBottom: "0.5rem" }} />
                          <input type="text" placeholder="Institution Name" value={edu.institution} onChange={(e) => updateEdu(idx, "institution", e.target.value)} style={{ width: "100%", padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
                      {state.education.map((edu, idx) => (
                        <div key={idx} style={{ background: "#ffffff", borderRadius: "0.6rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 1px 3px rgba(37,99,235,0.05)" }}>
                          <div style={{ fontSize: "1.05rem", fontWeight: 700, color: "#0f172a", marginBottom: "0.25rem" }}>
                            {edu.degree}
                          </div>
                          <div style={{ fontSize: "0.88rem", color: "#64748b" }}>
                            {[edu.institution, [edu.start_date, edu.end_date].filter(Boolean).join(" – ")].filter(Boolean).join(" · ")}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* ACHIEVEMENTS */}
              {sectionVisible("achievements", state.achievements.length > 0) && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      ACHIEVEMENTS
                    </h3>
                    <div style={{ display: "flex", gap: "0.5rem" }}>
                      {isEditing && (
                        <button type="button" className="btn btn-secondary" onClick={addAch} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                          + Add Item
                        </button>
                      )}
                      <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                        {isEditing ? "Done Editing" : "Edit Section"}
                      </button>
                    </div>
                  </div>

                  {isEditing ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                      {state.achievements.map((ach, idx) => (
                        <div key={idx} style={{ display: "flex", gap: "0.5rem" }}>
                          <input type="text" value={ach} onChange={(e) => updateAch(idx, e.target.value)} style={{ flex: 1, padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                          <button type="button" onClick={() => removeAch(idx)} style={{ color: "#ef4444", background: "none", border: "none", cursor: "pointer" }}><Trash2 size={14} /></button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <ul className="cmv-bullet-list">
                      {state.achievements.map((ach, idx) => (
                        <li key={idx}>
                          {ach}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {/* CERTIFICATIONS */}
              {sectionVisible("certifications", state.certifications.length > 0) && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      CERTIFICATIONS
                    </h3>
                    <div style={{ display: "flex", gap: "0.5rem" }}>
                      {isEditing && (
                        <button type="button" className="btn btn-secondary" onClick={addCert} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                          + Add Item
                        </button>
                      )}
                      <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                        {isEditing ? "Done Editing" : "Edit Section"}
                      </button>
                    </div>
                  </div>

                  {isEditing ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                      {state.certifications.map((cert, idx) => (
                        <div key={idx} style={{ display: "flex", gap: "0.5rem" }}>
                          <input type="text" value={cert} onChange={(e) => updateCert(idx, e.target.value)} style={{ flex: 1, padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }} />
                          <button type="button" onClick={() => removeCert(idx)} style={{ color: "#ef4444", background: "none", border: "none", cursor: "pointer" }}><Trash2 size={14} /></button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <ul className="cmv-bullet-list">
                      {state.certifications.map((cert, idx) => (
                        <li key={idx}>
                          {cert}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {/* ADDITIONAL INFORMATION — whatever the extractor returned that
                  the fixed schema has no field for. Both the display and the
                  form are built from the data, so a resume carrying a field
                  nobody anticipated is shown AND editable without a code
                  change. Edits go back to the same path inside additional_info. */}
              {showAll && (isEditing || additionalEntries.length > 0) && (
                <div style={{ background: "#f8fafc", borderRadius: "0.75rem", padding: "1.25rem", border: "1px solid #dbeafe", boxShadow: "0 2px 8px rgba(37,99,235,0.03)" }}>
                  <div className="cmv-section-head">
                    <h3 style={{ fontSize: "0.85rem", fontWeight: 700, color: "#1e40af", textTransform: "uppercase", letterSpacing: "0.06em", margin: 0, paddingBottom: "0.3rem", borderBottom: "2px solid #2563eb" }}>
                      ADDITIONAL INFORMATION
                    </h3>
                    <div style={{ display: "flex", gap: "0.5rem" }}>
                      {isEditing && (
                        <button type="button" className="btn btn-secondary" onClick={addExtra} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                          + Add Field
                        </button>
                      )}
                      <button type="button" className="btn btn-secondary" onClick={() => setIsEditing(!isEditing)} style={{ fontSize: "0.75rem", padding: "0.25rem 0.6rem", background: "#ffffff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}>
                        {isEditing ? "Done Editing" : "Edit Section"}
                      </button>
                    </div>
                  </div>

                  {isEditing ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
                      {state.extras.length === 0 && (
                        <div style={{ color: "#64748b", fontSize: "0.85rem" }}>
                          Nothing extra was extracted. Use “+ Add Field” to record something of your own.
                        </div>
                      )}
                      {state.extras.map((extra, idx) => (
                        <div key={`${extra.path}-${idx}`} className="cmv-extra-row">
                          <input
                            type="text"
                            value={extra.label}
                            placeholder="Field name"
                            onChange={(e) => updateExtraLabel(idx, e.target.value)}
                            style={{ padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#334155", fontSize: "0.85rem" }}
                          />
                          <input
                            type="text"
                            value={extra.value}
                            placeholder={extra.kind === "list" ? "Comma separated values" : "Value"}
                            onChange={(e) => updateExtraValue(idx, e.target.value)}
                            style={{ padding: "0.4rem 0.6rem", borderRadius: "0.4rem", border: "1px solid #cbd5e1", background: "#ffffff", color: "#0f172a" }}
                          />
                          <button type="button" onClick={() => removeExtra(idx)} style={{ color: "#ef4444", background: "none", border: "none", cursor: "pointer" }}>
                            <Trash2 size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="cmv-facts">
                      {additionalEntries.map(([label, value]) => (
                        <React.Fragment key={label}>
                          <div style={{ color: "#64748b" }}>{label}</div>
                          <div style={{ fontWeight: 700, color: "#0f172a", whiteSpace: "pre-wrap" }}>
                            {value}
                          </div>
                        </React.Fragment>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* WHITE & FASCINATING BLUE BOTTOM ACTION FOOTER BAR */}
        <div
          className="modal-footer cmv-footer"
          style={{
            borderTop: "1px solid #e2e8f0",
            background: "#ffffff",
            borderBottomLeftRadius: "0.75rem",
            borderBottomRightRadius: "0.75rem",
          }}
        >
          <div className="cmv-footer-group">
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
                  fontSize: "0.82rem",
                  padding: "0.5rem 0.9rem",
                  borderColor: "rgba(239,68,68,0.4)",
                  background: "rgba(239,68,68,0.08)",
                  color: "#ef4444",
                  fontWeight: 600,
                }}
              >
                <Trash2 size={15} /> Delete Candidate
              </button>
            )}
          </div>

          <div className="cmv-footer-group">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleDownload}
              disabled={downloading}
              style={{ fontSize: "0.85rem", padding: "0.5rem 1rem", background: "#eff6ff", border: "1px solid #dbeafe", color: "#1e40af", fontWeight: 600 }}
            >
              <Download size={15} /> {downloading ? "Downloading..." : "Download Resume"}
            </button>

            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => onVerify(candidate.id)}
              disabled={verifying || candidate.status === "verified"}
              style={{
                fontSize: "0.85rem",
                padding: "0.5rem 1rem",
                borderColor: candidate.status === "verified" ? "rgba(16,185,129,0.5)" : "#dbeafe",
                background: candidate.status === "verified" ? "rgba(16,185,129,0.12)" : "#eff6ff",
                color: candidate.status === "verified" ? "#059669" : "#1e40af",
                fontWeight: 600,
              }}
            >
              <CheckCircle2 size={15} />
              {verifying ? "Verifying..." : candidate.status === "verified" ? "Verified Profile" : "Verify Profile"}
            </button>

            <button
              type="button"
              className="btn btn-primary"
              onClick={handleSave}
              disabled={saving}
              style={{ fontSize: "0.85rem", padding: "0.5rem 1.25rem", background: "linear-gradient(135deg, #1e40af, #2563eb)", color: "#ffffff", fontWeight: 700, border: "none", boxShadow: "0 2px 8px rgba(37,99,235,0.3)" }}
            >
              <Save size={15} /> {saving ? "Saving to DB..." : "Save to DB"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function CandidateModal(props: CandidateModalProps) {
  if (!props.candidate) return null;
  return <CandidateModalBody {...props} candidate={props.candidate} />;
}
