/**
 * Everything the profile screen and the edit screen both need to know about a
 * candidate record.
 *
 * The two screens are deliberately separate — one shows, one changes — but they
 * have to agree on what a field is called, where it lives inside the record and
 * what counts as "the extractor said nothing here". That agreement lives in
 * this module, so a rule fixed for the reader is automatically fixed for the
 * editor.
 */

import type { CandidateProfile, CandidateRecord } from "@/lib/api";

// --------------------------------------------------------------------------- //
//  Editable shapes
// --------------------------------------------------------------------------- //

export interface EditableWorkExp {
  company: string;
  designation: string;
  start_date: string;
  end_date: string;
  location: string;
  description: string;
}

export interface EditableEdu {
  degree: string;
  institution: string;
  start_date: string;
  end_date: string;
  grade: string;
}

export interface EditableProject {
  name: string;
  description: string;
  technologies: string;
}

/**
 * One editable row for a field the fixed schema has no slot for.
 *
 * `path` is the location inside `additional_info` ("personal_info.nationality"),
 * so an edited value goes back exactly where it came from. `kind` remembers the
 * original JSON type, so a list stays a list and a number stays a number after
 * a round trip through a text input.
 */
export interface EditableExtra {
  path: string;
  label: string;
  value: string;
  kind: "text" | "number" | "boolean" | "list";
}

export interface EditableState {
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

// --------------------------------------------------------------------------- //
//  "Nothing here" detection
// --------------------------------------------------------------------------- //

export const EXTRA_PLACEHOLDERS = ["null", "n/a", "none", "0 months", "not specified"];

/** True when the extractor's answer amounts to "the resume did not say". */
export function isBlankValue(value?: string | null): boolean {
  const text = (value ?? "").trim();
  return !text || EXTRA_PLACEHOLDERS.includes(text.toLowerCase());
}

/**
 * The extractor always emits the duration counters — total/Indian/overseas
 * experience in months and years — and they are 0 for every fresher. Six rows
 * reading "0" say nothing the empty EXPERIENCE section has not already said,
 * so drop them rather than pad the profile with them.
 */
export function isZeroDuration(key: string, value: unknown): boolean {
  if (!/experience_(months|years)$/i.test(key)) return false;
  const asNumber = typeof value === "number" ? value : Number(String(value).trim());
  return Number.isFinite(asNumber) && asNumber === 0;
}

// --------------------------------------------------------------------------- //
//  additional_info — read, flatten, rebuild
// --------------------------------------------------------------------------- //

/** "date_of_birth" -> "Date of birth" — readable without a per-field label map. */
export function humanizeKey(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1).toLowerCase();
}

/** Label a nested path: "personal_info.date_of_birth" -> "Personal info · Date of birth". */
export function labelForPath(path: string): string {
  return path.split(".").map(humanizeKey).join(" · ");
}

/** Render one extracted value as text. Returns "" for anything empty. */
export function formatExtraValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") {
    const trimmed = value.trim();
    // The extractor uses these as "nothing here"; showing them is worse than
    // showing nothing at all.
    return isBlankValue(trimmed) ? "" : trimmed;
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
 * Walk `additional_info` down to its leaves, one editable row per leaf.
 *
 * Recursing rather than reading the top level keeps nested blocks — the
 * `personal_info` and `passport_details` the extractor returns — editable
 * field by field instead of as a wall of JSON. Arrays of objects are skipped:
 * those are experience/education/projects shaped, and the sections above own
 * them.
 */
export function extrasForEdit(value: unknown, path: string, out: EditableExtra[]): void {
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
  if (isBlankValue(text)) return;
  out.push({ path, label: labelForPath(path), value: text, kind: "text" });
}

/** Rebuild the nested `additional_info` object from the edited rows. */
export function extrasToObject(extras: EditableExtra[]): Record<string, unknown> {
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

/**
 * `additional_info` as label/value rows for the read-only view.
 *
 * Driven by the data rather than a hard-coded list, so a resume carrying a
 * field nobody anticipated still reaches the screen instead of being visible
 * only in the raw OCR dump.
 */
export function flattenExtras(extra: Record<string, unknown>, skip: string[]): [string, string][] {
  const skipped = new Set(skip);
  return Object.entries(extra)
    .filter(([key]) => !skipped.has(key))
    .filter(([key, value]) => !isZeroDuration(key, value))
    .map(([key, value]) => [humanizeKey(key), formatExtraValue(value)] as [string, string])
    .filter(([, value]) => value.length > 0);
}

/**
 * Renaming an extra field rewrites where it is stored. The label is what the
 * user types; the path is derived from it, keeping the parent so a renamed
 * nested field stays inside its block ("Personal info · Nationality" continues
 * to live under personal_info).
 */
export function renameExtra(extra: EditableExtra, label: string): EditableExtra {
  const parent = extra.path.includes(".") ? extra.path.slice(0, extra.path.lastIndexOf(".")) : "";
  const leaf = label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return { ...extra, label, path: leaf ? (parent ? `${parent}.${leaf}` : leaf) : "" };
}

// --------------------------------------------------------------------------- //
//  Raw OCR fallbacks
//
//  `raw_ocr` is whatever the extractor happened to emit — untyped by nature and
//  inconsistent between resumes, which is why every reader below names several
//  candidate keys for the same idea ("designation" or "role" or "title"). The
//  helpers narrow it without pretending it has a shape.
// --------------------------------------------------------------------------- //

type RawRecord = Record<string, unknown>;

function asText(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number") return String(value);
  return "";
}

/** The first of `keys` that carries text, or "". */
function pick(row: RawRecord, ...keys: string[]): string {
  for (const key of keys) {
    const text = asText(row[key]);
    if (text) return text;
  }
  return "";
}

function asRows(value: unknown): RawRecord[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is RawRecord => typeof item === "object" && item !== null);
}

function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(asText).filter(Boolean);
}

/** An achievement or certification: sometimes a string, sometimes a titled object. */
function asTitleList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (item && typeof item === "object") return pick(item as RawRecord, "title", "name");
      return asText(item);
    })
    .filter(Boolean);
}

// --------------------------------------------------------------------------- //
//  Record <-> editable state
// --------------------------------------------------------------------------- //

export function toEditableState(profile: CandidateProfile, candidate?: CandidateRecord): EditableState {
  const rawData: RawRecord = (candidate?.raw_ocr || profile.raw_ocr || {}) as RawRecord;

  const experiences = (profile.work_experience && profile.work_experience.length > 0)
    ? profile.work_experience
    : asRows(rawData.experience ?? rawData.work_experience).map((row) => ({
        company: pick(row, "company"),
        designation: pick(row, "designation", "role", "title"),
        start_date: pick(row, "start_date"),
        end_date: pick(row, "end_date"),
        location: pick(row, "location"),
        description: pick(row, "description"),
      }));

  const educations = (profile.education && profile.education.length > 0)
    ? profile.education
    : asRows(rawData.education).map((row) => ({
        institution: pick(row, "institution", "school", "university"),
        degree: pick(row, "degree", "qualification"),
        field_of_study: pick(row, "field_of_study", "major"),
        start_date: pick(row, "start_date"),
        end_date: pick(row, "end_date"),
        grade: pick(row, "grade", "gpa"),
      }));

  const projects = (profile.projects && profile.projects.length > 0)
    ? profile.projects
    : asRows(rawData.projects).map((row) => ({
        name: pick(row, "name", "title"),
        description: pick(row, "description"),
        technologies: asStrings(row.technologies),
      }));

  const achievements = (profile.achievements && profile.achievements.length > 0)
    ? profile.achievements
    : asTitleList(rawData.achievements ?? rawData.awards);

  const certifications = (profile.certifications && profile.certifications.length > 0)
    ? profile.certifications
    : asTitleList(rawData.certifications ?? rawData.certificates);

  const languages = (profile.languages && profile.languages.length > 0)
    ? profile.languages
    : asStrings(rawData.languages);

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
    email,
    phone,
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
        // `title` is not on WorkExperience but the API round-trips it — an
        // entry saved by an older build carries the role there and nowhere else.
        designation: exp.designation || asText((exp as RawRecord).title),
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
    achievements,
    certifications,
    extras: (() => {
      const rows: EditableExtra[] = [];
      extrasForEdit(profile.additional_info ?? {}, "", rows);
      return rows;
    })(),
  };
}

/** The edited state folded back onto the record the editor was opened with. */
export function editableToProfile(base: CandidateProfile, state: EditableState): CandidateProfile {
  return {
    ...base,
    full_name: state.full_name,
    current_designation: state.designation,
    resume_summary: state.summary,
    email: state.email,
    phone: state.phone,
    location: state.location,
    total_experience_years: parseFloat(state.experience) || base.total_experience_years || 0,
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
    // Rebuilt from the edited rows rather than carried over from `...base`, so
    // edits to extracted fields persist and cleared ones are removed.
    additional_info: extrasToObject(state.extras),
  } as CandidateProfile;
}

/**
 * Prefer what the extractor concluded. Falling back to `education[0]` alone
 * reported "10th Grade" for a candidate holding a B.E., because the list is in
 * chronological order and the first entry is the earliest schooling — so the
 * last entry, not the first, is the closest local guess.
 */
export function highestQualificationOf(profile: CandidateProfile): string {
  const extra = (profile.additional_info ?? {}) as Record<string, unknown>;
  return (
    (typeof extra.highest_qualification === "string" && extra.highest_qualification) ||
    profile.education?.[profile.education.length - 1]?.degree ||
    profile.education?.[0]?.degree ||
    ""
  );
}

/** Veris returns this, but `CandidateProfile` has no field for it. */
export function industryOf(profile: CandidateProfile): string {
  const extra = (profile.additional_info ?? {}) as Record<string, unknown>;
  return typeof extra.industry === "string" ? extra.industry : "";
}

/** Splits a free-text description into the bullets it was written as. */
export function toBullets(text: string): string[] {
  return text
    .split(/\r?\n|•/)
    .map((line) => line.trim().replace(/^[\-\*•●\s]+/, ""))
    .filter(Boolean);
}

/**
 * A one-line summary of what an edit actually changed.
 *
 * "Profile saved" tells a reader that something happened; it does not tell them
 * what, which is the only question an audit trail exists to answer. This walks
 * the fields a recruiter edits and reports the ones that moved, so the entry in
 * the history reads "Key skills changed to 'Driver, Forklift'" rather than
 * "updated".
 */
export function summariseProfileChange(
  before: CandidateProfile,
  after: CandidateProfile,
): string {
  const parts: string[] = [];

  const text = (value: unknown): string => (typeof value === "string" ? value.trim() : "");
  const list = (value: unknown): string =>
    Array.isArray(value) ? value.map((v) => String(v).trim()).filter(Boolean).join(", ") : "";

  /** Truncated so one pasted essay cannot make the log unreadable. */
  const clip = (value: string, limit = 60) =>
    value.length > limit ? `${value.slice(0, limit - 1)}…` : value;

  const scalars: [string, keyof CandidateProfile][] = [
    ["Name", "full_name"],
    ["Designation", "current_designation"],
    ["Email", "email"],
    ["Phone", "phone"],
    ["Location", "location"],
    ["Summary", "resume_summary"],
  ];
  for (const [label, key] of scalars) {
    const from = text(before[key]);
    const to = text(after[key]);
    if (from === to) continue;
    parts.push(to ? `${label} changed to '${clip(to)}'` : `${label} cleared`);
  }

  const lists: [string, keyof CandidateProfile][] = [
    ["Key skills", "skills"],
    ["Languages", "languages"],
    ["Certifications", "certifications"],
    ["Achievements", "achievements"],
  ];
  for (const [label, key] of lists) {
    const from = list(before[key]);
    const to = list(after[key]);
    if (from === to) continue;
    parts.push(to ? `${label} changed to '${clip(to)}'` : `${label} cleared`);
  }

  if (before.total_experience_years !== after.total_experience_years) {
    parts.push(`Experience changed to '${after.total_experience_years ?? 0} yrs'`);
  }

  // Counts rather than contents: a reader wants to know a role was added, not
  // to read the whole entry back inside a log line.
  const sections: [string, keyof CandidateProfile][] = [
    ["Experience entries", "work_experience"],
    ["Education entries", "education"],
    ["Projects", "projects"],
  ];
  for (const [label, key] of sections) {
    const from = Array.isArray(before[key]) ? (before[key] as unknown[]).length : 0;
    const to = Array.isArray(after[key]) ? (after[key] as unknown[]).length : 0;
    if (from !== to) parts.push(`${label} ${to > from ? "added" : "removed"} (${from} → ${to})`);
  }

  if (parts.length === 0) return "Saved with no field changes.";
  // Three is enough to say what happened; the rest is noise in one line.
  const shown = parts.slice(0, 3).join(" | ");
  const rest = parts.length - 3;
  return rest > 0 ? `${shown} | +${rest} more field${rest === 1 ? "" : "s"}` : shown;
}
