"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  BadgeCheck,
  CheckCircle2,
  Download,
  FolderGit2,
  IdCard,
  Link as LinkIcon,
  Loader2,
  Mail,
  MapPin,
  Phone,
  Plane,
  Star,
} from "lucide-react";

import {
  flattenExtras,
  formatExtraValue,
  highestQualificationOf,
  humanizeKey,
  industryOf,
  isBlankValue,
  toBullets,
  toEditableState,
} from "@/lib/candidateProfile";
import {
  getCandidateIdentity,
  identityFileUrl,
  resumeDownloadUrl,
  type AadhaarRecord,
  type CandidateRecord,
  type EvaluationStatus,
  type IdentityDocuments,
  type PassportRecord,
} from "@/lib/api";
import { formatDateFull, initialsOf } from "@/lib/format";

/**
 * Download a protected file without leaving the page.
 *
 * The request deliberately carries **no `Authorization` header**. That header
 * is what made this a "non-simple" cross-origin request, and a non-simple
 * request needs a CORS preflight — which is precisely what was being dropped
 * in the field, producing "Failed to fetch" with nothing at all in the server
 * log. Authorisation instead rides in the URL's `token` query parameter, which
 * the API accepts (see `current_user`) and which `resumeDownloadUrl` and
 * `identityFileUrl` already put there. A plain GET with no custom headers is a
 * simple request: no preflight, nothing for an extension or proxy to refuse.
 *
 * The bytes then become a blob URL, which is same-origin, so the `download`
 * attribute is honoured and the file saves in place — no new tab, no
 * navigation away from the profile.
 */
async function saveFile(url: string, fallbackName: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(url);
  } catch {
    // The request never became an HTTP exchange. Nothing here can say why, and
    // the file is very probably fine — so fall back to letting the browser
    // fetch it itself, in a hidden frame so the page still does not move.
    downloadInBackground(url);
    return;
  }

  if (!response.ok) {
    let detail = `Server replied ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Not a JSON error body — the status line is what there is.
    }
    throw new Error(detail);
  }

  const objectUrl = window.URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = nameFromDisposition(response) || fallbackName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Not revoked on this tick: the click is handled asynchronously, and tearing
  // the object URL down straight away cancels the save of a large file before
  // the browser has finished reading it.
  window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 60_000);
}

/**
 * Let the browser fetch a file itself, in a frame nobody sees.
 *
 * A response carrying `Content-Disposition: attachment` is downloaded rather
 * than rendered, so the frame stays blank and the page it sits on never moves.
 * The last resort, for when `fetch` itself is blocked.
 */
function downloadInBackground(url: string): void {
  const frame = document.createElement("iframe");
  frame.style.display = "none";
  frame.src = url;
  document.body.appendChild(frame);
  window.setTimeout(() => frame.remove(), 120_000);
}

/**
 * The filename the server chose, if it sent one.
 *
 * Worth reading rather than always naming the file here: for a document cut
 * out of an application bundle only the server knows which pages it took, and
 * `application_passport_p55.pdf` is the difference between four downloads a
 * recruiter can tell apart and four called `passport.pdf`.
 *
 * Readable only because the API lists `Content-Disposition` in its CORS
 * `expose_headers`; without that the browser hides it from JavaScript.
 */
function nameFromDisposition(response: Response): string | null {
  const header = response.headers.get("content-disposition") || "";
  const plain = /filename="([^"]+)"/.exec(header);
  return plain ? plain[1] : null;
}

/**
 * "Download scan" on one identity row.
 *
 * A profile can carry two passports and an Aadhaar, so each row gets its own
 * button bound to its own record id — the id is what decides which document
 * the API serves.
 *
 * Rendered only when the server said `file_available`. That flag is not a
 * cosmetic hint — an Aadhaar scan is refused outright to anyone who is not an
 * administrator, because the card is the number the row above it masks.
 */
function ScanDownload({
  candidateId,
  documentType,
  recordId,
}: {
  candidateId: string;
  documentType: "aadhaar" | "passport";
  recordId: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const click = async () => {
    setError(null);
    setBusy(true);
    try {
      await saveFile(
        identityFileUrl(candidateId, documentType, recordId),
        `${documentType}.pdf`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not download the ${documentType} scan.`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="cprof-doc-action">
      <button type="button" className="cprof-doc-download" onClick={click} disabled={busy}>
        {busy ? <Loader2 size={13} className="icon-spin" /> : <Download size={13} />}
        {busy ? "Downloading…" : "Download scan"}
      </button>
      {/* Beside the button rather than at the top of the screen: the message is
          about this document, and a profile carrying three of them would
          otherwise say "the scan could not be downloaded" about no one in
          particular. */}
      {error && <span className="cprof-doc-download-error">{error}</span>}
    </span>
  );
}

/** What a reviewer records against a profile. */
export interface Verdict {
  status: EvaluationStatus;
  score: number | null;
  notes: string | null;
}

/**
 * Turns this screen from a read-only profile into a review station.
 *
 * Passed only in the staff workspace. Its presence is the whole difference
 * between the two uses of this screen: with it, the verdict suite is live and
 * sits alongside the résumé; without it, the stored verdict is shown as a
 * read-only card and nothing on the screen can change the record.
 *
 * The evidence and the judgement have to be on one screen — that is the point.
 * A reviewer scrolling a résumé in one place and recording a decision in
 * another is being asked to hold the résumé in their head.
 */
export interface EvaluationSuite {
  saving: boolean;
  /** The next unopened profile's name, if there is one, for the advance button. */
  nextName?: string | null;
  onSave: (verdict: Verdict, advance: boolean) => void;
}

interface CandidateProfileScreenProps {
  candidate: CandidateRecord;
  verifying?: boolean;
  onBack?: () => void;
  onVerify?: (candidateId: string) => void;
  /** Supplied by the staff workspace; omitted everywhere else. */
  evaluation?: EvaluationSuite;
}

const STATUS_CHOICES: { value: EvaluationStatus; label: string }[] = [
  { value: "shortlisted", label: "Shortlisted" },
  { value: "interviewing", label: "Interviewing" },
  { value: "rejected", label: "Rejected" },
];

/**
 * One label/value pair, or nothing when the value is blank.
 *
 * "N/A" against six labels reads as six findings — the reader has to check each
 * one to learn the resume said nothing about any of them. Omitting the row says
 * the same thing at a glance. Both cells are direct children of `.cprof-facts`
 * so the grid keeps its columns.
 */
function Fact({ label, value }: { label: string; value?: string | null }) {
  if (isBlankValue(value)) return null;
  return (
    <>
      <div className="cprof-fact-label">{label}</div>
      <div className="cprof-fact-value">{(value ?? "").trim()}</div>
    </>
  );
}

/**
 * How long a passport has left, as a recruiter needs to hear it.
 *
 * Overseas placement turns on this: a passport that expires inside the
 * deployment window is a stopped application, and the number on the card does
 * not say that by itself. Returns null when the date is not one this can read —
 * MRZ dates arrive in more than one shape, and guessing at an unparseable one
 * would be worse than saying nothing.
 */
function expiryNotice(value?: string | null): { tone: "expired" | "soon"; text: string } | null {
  const text = (value ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
  const expiry = new Date(`${text}T00:00:00`);
  if (Number.isNaN(expiry.getTime())) return null;

  const days = Math.round((expiry.getTime() - Date.now()) / 86_400_000);
  if (days < 0) return { tone: "expired", text: "Expired" };
  // Six months is the window most destinations require on arrival.
  if (days <= 180) return { tone: "soon", text: `Expires in ${days} day${days === 1 ? "" : "s"}` };
  return null;
}

/**
 * Printed-page fields the MRZ has already supplied. Shown once, from the MRZ,
 * which is the machine-read half of the page and the half with check digits
 * behind it.
 *
 * Compared after `mrzKey` normalises the name, because the two halves of the
 * response label the same field differently: the MRZ block uses `given_names`
 * and the printed-field list says "Given Names". Matching the raw strings let
 * every one of them through, so the card repeated the whole passport under
 * itself.
 */
const MRZ_FIELDS = new Set([
  "passport_number",
  "surname",
  "given_names",
  "name",
  "nationality",
  "issuing_country",
  "country",
  "date_of_birth",
  "sex",
  "gender",
  "expiry_date",
  "date_of_expiry",
  "date_of_issue",
  "personal_number",
  // Labels the printed-field list uses that the MRZ answers under another name.
  "document_type",
  "type",
]);

/** "Given Names" and "given_names" are the same field. */
function mrzKey(label: string): string {
  return label.trim().toLowerCase().replace(/[\s-]+/g, "_");
}

/**
 * One scanned document, exactly as the OCR read it.
 *
 * Read-only with no edit path anywhere near it, and that is the point: this is
 * evidence about a file somebody sent, not a field of the candidate record. It
 * carries its own provenance — which attachment, off which pages, read when —
 * because a documentation officer checking a misread digit needs to know which
 * page to open, and it carries the service's own integrity verdict rather than
 * presenting a failed checksum as fact.
 */
function DocumentCard({
  title,
  icon,
  badges,
  action,
  source,
  readAt,
  warnings,
  extractionNotes,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  badges?: React.ReactNode;
  /** Sits at the far end of the heading. The download, where there is one. */
  action?: React.ReactNode;
  source?: { filename?: string; pages?: number[] };
  readAt?: string;
  /** Things that want a human: a field that could not be read, a check that
      did not pass. Always visible — this is the half worth interrupting for. */
  warnings?: string[];
  /** What the OCR had to do to read the document, all of it successful.
      Folded away: on a clean scan there are five or six of these, and they
      would otherwise bury the fields somebody opened the card to read. */
  extractionNotes?: string[];
  children: React.ReactNode;
}) {
  const pages = source?.pages ?? [];
  const provenance = [
    source?.filename,
    pages.length > 0 ? `page${pages.length === 1 ? "" : "s"} ${pages.join(", ")}` : null,
    readAt ? `read ${formatDateFull(new Date(readAt))}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <article className="cprof-doc">
      <div className="cprof-doc-head">
        <span className="cprof-doc-icon" aria-hidden="true">
          {icon}
        </span>
        <h4 className="cprof-doc-title">{title}</h4>
        {badges}
        {action}
      </div>

      <div className="cprof-facts">{children}</div>

      {(warnings?.length ?? 0) > 0 && (
        <ul className="cprof-doc-warnings">
          {warnings!.map((warning, index) => (
            <li key={index}>
              <AlertTriangle size={13} aria-hidden="true" /> {warning}
            </li>
          ))}
        </ul>
      )}

      {/* Deliberately not styled as a warning. Every one of these describes the
          document being read *successfully* — a page straightened, an MRZ
          located, a field recovered by a second look. Shown because it is the
          answer to "why does this field look odd", and shut because on a clean
          scan it is six lines of housekeeping above the passport number. */}
      {(extractionNotes?.length ?? 0) > 0 && (
        <details className="cprof-doc-notes">
          <summary>
            How this was read · {extractionNotes!.length} step
            {extractionNotes!.length === 1 ? "" : "s"}
          </summary>
          <ul>
            {extractionNotes!.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </details>
      )}

      {provenance && <p className="cprof-doc-source">{provenance}</p>}
    </article>
  );
}

/** The OCR's own verdict on whether it read the document correctly. */
function CheckBadge({ valid, label }: { valid?: boolean | null; label: string }) {
  if (valid === null || valid === undefined) return null;
  return (
    <span className={`cprof-doc-badge ${valid ? "is-ok" : "is-bad"}`}>
      {valid ? <BadgeCheck size={13} /> : <AlertTriangle size={13} />}
      {valid ? label : `${label} failed`}
    </span>
  );
}

function BulletText({ text }: { text: string }) {
  const lines = toBullets(text);
  if (lines.length === 0) return null;
  if (lines.length === 1) return <p className="cprof-prose">{lines[0]}</p>;
  return (
    <ul className="cprof-bullets">
      {lines.map((line, index) => (
        <li key={index}>{line}</li>
      ))}
    </ul>
  );
}

/**
 * The executive profile, read-only by construction.
 *
 * There is no edit control anywhere on this screen and no editable state behind
 * it — changing a candidate is a different screen with a different job. That
 * separation is the point: this one can be read, scrolled and shown to someone
 * without any risk that a stray click alters the record. Verifying is the one
 * exception, and it changes a status rather than any of the parsed fields.
 */
export default function CandidateProfileScreen({
  candidate,
  verifying = false,
  onBack,
  onVerify,
  evaluation,
}: CandidateProfileScreenProps) {

  /**
   * The Aadhaar and passport scans, fetched separately because they are stored
   * separately — in their own collections, so the reads that build the
   * candidate list cannot serve a government identity number to a browser.
   *
   * The candidate id is held alongside the answer rather than cleared when the
   * candidate changes: a screen that blanks it on the way in renders one frame
   * of "no documents" for a candidate who has them, and one that does not
   * check whose answer this is renders the previous candidate's passport
   * against this one's name. Comparing ids covers both, and is the only thing
   * that does.
   */
  const [identity, setIdentity] = useState<{
    candidateId: string;
    documents: IdentityDocuments | null;
    error: string | null;
  } | null>(null);

  // The verdict controls, seeded from whatever is already on the record so
  // re-opening an evaluated profile shows the decision that was made rather
  // than an empty form.
  //
  // Seeded once, from the initial value, and that is sufficient: "Save & next"
  // swaps the candidate under this screen, and the caller keys it on the
  // candidate id so a different candidate is a different component instance.
  // Resetting these in an effect instead would render the previous reviewer's
  // notes for a frame before correcting itself.
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [score, setScore] = useState(candidate.evaluation_score ?? 0);
  const [status, setStatus] = useState<EvaluationStatus>(
    candidate.evaluation_status && candidate.evaluation_status !== "pending"
      ? candidate.evaluation_status
      : "shortlisted",
  );
  const [notes, setNotes] = useState(candidate.evaluation_notes ?? "");

  const profile = candidate.profile ?? {};
  const view = useMemo(() => toEditableState(candidate.profile ?? {}, candidate), [candidate]);

  useEffect(() => {
    let cancelled = false;
    const candidateId = candidate.id;

    getCandidateIdentity(candidateId)
      .then((documents) => {
        if (!cancelled) setIdentity({ candidateId, documents, error: null });
      })
      .catch((err) => {
        // A profile is still worth reading when the identity lookup fails, so
        // this reports rather than throws — the document sections simply say
        // they could not be loaded.
        if (!cancelled) {
          setIdentity({
            candidateId,
            documents: null,
            error: err instanceof Error ? err.message : "Could not load identity documents.",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [candidate.id]);

  /** Only this candidate's answer counts; an older one is still in flight. */
  const identityFor = identity?.candidateId === candidate.id ? identity : null;
  const identityError = identityFor?.error ?? null;

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

  // ---- what they applied for ---------------------------------------------- #
  // The job title as stored, falling back to the controlled category so a
  // record written before titles were kept still names the job. The category
  // then only earns its own row when it is saying something the title did not.
  const jobTitle = view.job_title || (view.job_category ? humanizeKey(view.job_category) : "");
  const jobCategory = view.job_title && view.job_category ? humanizeKey(view.job_category) : "";
  const tradeSkills = view.trade_skills
    .split(",")
    .map((skill) => skill.trim())
    .filter(Boolean);
  const jobAnswers = view.job_answers.filter((entry) => entry.question.trim() || entry.answer.trim());

  const hasJobDetails = Boolean(
    jobTitle ||
      view.course_or_trade ||
      view.destination_country ||
      view.state_preference ||
      view.job_preference ||
      view.available_from ||
      tradeSkills.length > 0 ||
      jobAnswers.length > 0,
  );

  // ---- the scans ----------------------------------------------------------- #
  const passports: PassportRecord[] = identityFor?.documents?.passport ?? [];
  const aadhaars: AadhaarRecord[] = identityFor?.documents?.aadhaar ?? [];
  // A passport the résumé mentioned but no scan backs. Worth a row of its own —
  // "we have the number, we have not seen the document" is a different state
  // from either having the scan or having nothing.
  const statedPassport = Boolean(profile.passport_number || profile.passport_expiry);
  const hasPassportSection = passports.length > 0 || statedPassport;
  const hasAadhaarSection = aadhaars.length > 0;
  const isVerified = candidate.status === "verified";
  const ingestedOn = candidate.created_at ? formatDateFull(new Date(candidate.created_at)) : "—";

  const reviewing = Boolean(evaluation);
  // The stored verdict is worth showing on its own only when nothing on screen
  // can change it. In review mode the live suite is the verdict, and a card
  // repeating it above would be a second, stale copy of the same fields.
  const hasVerdict =
    !reviewing &&
    Boolean(candidate.evaluation_status || candidate.evaluation_notes || candidate.evaluation_score);

  /** Only sections that actually carry something are offered or drawn. */
  const sections = [
    { id: "details", label: "Details", present: true },
    { id: "job", label: "Job & preferences", present: hasJobDetails },
    { id: "passport", label: "Passport", present: hasPassportSection },
    { id: "aadhaar", label: "Aadhaar", present: hasAadhaarSection },
    { id: "verdict", label: "Verdict & Evaluation", present: hasVerdict },
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
      await saveFile(
        resumeDownloadUrl(candidate.id),
        candidate.resume?.original_filename || "resume.pdf",
      );
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

  /**
   * Whether there is actually a file behind the download button.
   *
   * A WhatsApp candidate for a role the CV policy exempts has no résumé, and
   * that is a complete record rather than a broken one. Offering a download
   * that can only 404, and captioning the profile with a "resume.pdf" nobody
   * uploaded, both tell the recruiter something untrue.
   */
  const hasResume = Boolean(
    candidate.resume?.storage_key || candidate.resume?.original_filename,
  );
  const fromWhatsApp = candidate.source === "whatsapp";
  const sourceLabel = candidate.source === "upload"
    ? "Admin upload"
    : candidate.source === "manual" ? "Legacy manual entry" : fromWhatsApp ? "WhatsApp" : "Email";

  /** Where they are, and where they want to go — shown as two separate facts. */
  const originLine = [
    sourceLabel,
    candidate.profile?.country ? `from ${candidate.profile.country}` : null,
    candidate.profile?.destination_country
      ? `→ ${candidate.profile.destination_country}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const submit = (advance: boolean) =>
    evaluation?.onSave(
      { status, score: score > 0 ? score : null, notes: notes.trim() || null },
      advance,
    );

  return (
    <div className="cscreen" style={{ animation: "fadeIn 0.3s ease" }}>
      <div className="cscreen-topbar">
        {onBack && (
          <button type="button" className="jod-back" onClick={onBack} title="Back to the candidates list">
            <ArrowLeft size={15} /> Back to candidates
          </button>
        )}

        <div className="cscreen-topbar-actions">
          {/* Only offered when a file exists. See `hasResume`. */}
          {hasResume && (
            <button
              type="button"
              className="cscreen-btn"
              onClick={handleDownload}
              disabled={downloading}
            >
              <Download size={15} /> {downloading ? "Downloading…" : "Download resume"}
            </button>
          )}

          {/* Shown when onVerify is passed; hidden in StaffScreen view where staff focus on evaluations */}
          {onVerify && (
            <button
              type="button"
              className={`cscreen-btn ${isVerified ? "is-verified" : "is-primary"}`}
              onClick={() => onVerify(candidate.id)}
              disabled={verifying || isVerified}
            >
              <CheckCircle2 size={15} />
              {verifying ? "Saving review…" : isVerified ? "Review completed" : "Complete review"}
            </button>
          )}
        </div>
      </div>

      {downloadError && <div className="cscreen-error">Resume download failed — {downloadError}</div>}

      {/* Reported rather than swallowed, and reported once.
          A failed lookup and a candidate with no scans on file produce the same
          empty sections, and those are opposite facts: one means there is no
          passport, the other means nobody knows. Saying so here keeps the
          sections below driven purely by what was actually read. */}
      {identityError && (
        <div className="cscreen-error">
          Passport and Aadhaar scans could not be loaded — {identityError}
        </div>
      )}

      <header className="cprof-hero">
        <span className="cprof-monogram" aria-hidden="true">
          {initialsOf(view.full_name)}
        </span>

        <div className="cprof-hero-text">
          <div className="cprof-hero-line">
            <h2 className="cprof-name">{view.full_name}</h2>
            <span className="crm-record-id is-prominent">
              Candidate ID · {candidate.candidate_code || `CAN-${candidate.id.slice(-12).toUpperCase()}`}
            </span>
            <span className={`db-pill ${isVerified ? "is-verified" : "is-info"}`}>
              {isVerified ? "Verified" : "Active"}
            </span>
            {/* "Read-only" would be a lie next to a live verdict form. */}
            {!reviewing && <span className="cprof-readonly-tag">Read-only view</span>}
          </div>

          {!isBlankValue(view.designation) && <p className="cprof-role">{view.designation}</p>}

          {/*
            A CV-less candidate gets a description of what they are, not a
            filename standing in for a file that was never sent. "CV not
            required" is the actual state of the record and is what a recruiter
            needs to see before they go looking for a document.
          */}
          <p className="cprof-meta">
            {hasResume
              ? candidate.resume?.original_filename
              : candidate.cv_required
                ? "No CV on file — one is required"
                : "CV not required for this role"}
            {" · "}
            {originLine} · Ingested {ingestedOn}
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

      {/* In review mode the résumé and the verdict are two columns of one grid,
          and the verdict column sticks as the résumé scrolls — the decision has
          to stay reachable from wherever in the CV the reviewer forms it. The
          grid collapses to a single column on narrow screens, which puts the
          verdict at the bottom, directly after the evidence. */}
      <div className={reviewing ? "cprof-review" : "cprof-review is-plain"}>
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
            <Fact label="Received at (To email)" value={candidate.source_email?.to_addr} />
            <Fact label="Phone" value={view.phone} />
            <Fact label="LinkedIn" value={view.linkedin} />
            <Fact label="GitHub" value={view.github} />
            <Fact label="Address" value={view.location} />
          </div>
        </section>

        {hasJobDetails && (
          <section className="cprof-card" id={sectionId("job")}>
            <h3 className="cprof-card-title">Job &amp; preferences</h3>
            <div className="cprof-facts">
              <Fact label="Job applied for" value={jobTitle} />
              <Fact label="Job category" value={jobCategory} />
              <Fact label="Course / trade" value={view.course_or_trade} />
              <Fact label="Destination country" value={view.destination_country} />
              <Fact label="State / district preference" value={view.state_preference} />
              <Fact label="Job preference" value={view.job_preference} />
              <Fact label="Available to join" value={view.available_from} />
            </div>

            {tradeSkills.length > 0 && (
              <>
                <p className="cprof-subhead">Trade skills</p>
                <div className="cprof-skills">
                  {tradeSkills.map((skill, index) => (
                    <span key={`${skill}-${index}`} className="cprof-skill">
                      {skill}
                    </span>
                  ))}
                </div>
              </>
            )}

            {/* Asked when they registered, and answered in their own words.
                The question is stored with the answer rather than looked up,
                so a question reworded since is not put in their mouth. */}
            {jobAnswers.length > 0 && (
              <>
                <p className="cprof-subhead">Screening questions</p>
                <div className="cprof-qa">
                  {jobAnswers.map((entry, index) => (
                    <div key={entry.question_id || index} className="cprof-qa-row">
                      <p className="cprof-qa-question">
                        {entry.question.trim() || `Question ${index + 1}`}
                      </p>
                      <p className="cprof-qa-answer">
                        {entry.answer.trim() || "No answer recorded"}
                      </p>
                    </div>
                  ))}
                </div>
              </>
            )}
          </section>
        )}

        {hasPassportSection && (
          <section className="cprof-card" id={sectionId("passport")}>
            <h3 className="cprof-card-title">Passport</h3>

            {passports.length > 0 && (
              <div className="cprof-docs">
                {passports.map((passport, index) => {
                  const expiry = expiryNotice(passport.expiry_date);
                  const printed = (passport.printed_fields ?? {}) as Record<string, unknown>;
                  return (
                    <DocumentCard
                      key={passport._id || index}
                      title={
                        [passport.given_names, passport.surname].filter(Boolean).join(" ") ||
                        `Passport ${index + 1}`
                      }
                      icon={<Plane size={15} />}
                      badges={
                        <>
                          <CheckBadge valid={passport.check_digits_valid} label="MRZ check digits" />
                          {expiry && (
                            <span
                              className={`cprof-doc-badge ${
                                expiry.tone === "expired" ? "is-bad" : "is-warn"
                              }`}
                            >
                              <AlertTriangle size={13} /> {expiry.text}
                            </span>
                          )}
                        </>
                      }
                      action={
                        passport.file_available && passport._id ? (
                          <ScanDownload
                            candidateId={candidate.id}
                            documentType="passport"
                            recordId={passport._id}
                          />
                        ) : null
                      }
                      source={passport.source}
                      readAt={passport.updated_at}
                      warnings={passport.warnings}
                      extractionNotes={passport.extraction_notes}
                    >
                      <Fact label="Passport number" value={passport.passport_number} />
                      <Fact label="Surname" value={passport.surname} />
                      <Fact label="Given names" value={passport.given_names} />
                      <Fact label="Nationality" value={passport.nationality} />
                      <Fact label="Issuing country" value={passport.issuing_country} />
                      <Fact label="Date of birth" value={passport.date_of_birth} />
                      <Fact label="Sex" value={passport.sex} />
                      <Fact label="Date of issue" value={passport.date_of_issue} />
                      <Fact label="Date of expiry" value={passport.expiry_date} />
                      <Fact label="Personal number" value={passport.personal_number} />
                      {/* Read off the printed data page rather than the MRZ,
                          which is where the place of issue lives and nowhere
                          else. Anything the MRZ already gave is dropped: the
                          two agree on most of the page, and a second "Passport
                          number" row reads as a second passport. */}
                      {Object.entries(printed)
                        .filter(([key]) => !MRZ_FIELDS.has(mrzKey(key)))
                        .map(([key, value]) => (
                          <Fact key={key} label={humanizeKey(key)} value={formatExtraValue(value)} />
                        ))}
                    </DocumentCard>
                  );
                })}
              </div>
            )}

            {/* What the application claimed, when no scan backs it. */}
            {statedPassport && (
              <>
                {passports.length > 0 && (
                  <p className="cprof-subhead">As stated on the application</p>
                )}
                <div className="cprof-facts">
                  <Fact label="Passport number" value={profile.passport_number} />
                  <Fact label="Passport expiry" value={profile.passport_expiry} />
                </div>
              </>
            )}

            {passports.length === 0 && (
              <p className="cprof-doc-note">
                No passport scan has been read — the number above is what the application stated.
              </p>
            )}
          </section>
        )}

        {hasAadhaarSection && (
          <section className="cprof-card" id={sectionId("aadhaar")}>
            <h3 className="cprof-card-title">Aadhaar</h3>
            <div className="cprof-docs">
              {aadhaars.map((aadhaar, index) => (
                <DocumentCard
                  key={aadhaar._id || index}
                  title={aadhaar.name || `Aadhaar card ${index + 1}`}
                  icon={<IdCard size={15} />}
                  badges={
                    <>
                      <CheckBadge valid={aadhaar.aadhaar_number_valid} label="Checksum" />
                      {aadhaar.document_side && (
                        <span className="cprof-doc-badge">
                          {humanizeKey(aadhaar.document_side)}
                        </span>
                      )}
                    </>
                  }
                  action={
                    aadhaar.file_available && aadhaar._id ? (
                      <ScanDownload
                        candidateId={candidate.id}
                        documentType="aadhaar"
                        recordId={aadhaar._id}
                      />
                    ) : null
                  }
                  source={aadhaar.source}
                  readAt={aadhaar.updated_at}
                  warnings={aadhaar.warnings}
                  extractionNotes={aadhaar.extraction_notes}
                >
                  <Fact label="Name" value={aadhaar.name} />
                  {/* The full number reaches the browser for administrators
                      only; everyone else is served the masked one, and the
                      server is what decides which. */}
                  <Fact
                    label="Aadhaar number"
                    value={aadhaar.aadhaar_number || aadhaar.masked_aadhaar_number}
                  />
                  <Fact label="Date of birth" value={aadhaar.date_of_birth} />
                  <Fact
                    label="Year of birth"
                    value={aadhaar.year_of_birth ? String(aadhaar.year_of_birth) : ""}
                  />
                  <Fact label="Gender" value={aadhaar.gender} />
                  <Fact label="Mobile number" value={aadhaar.mobile_number} />
                  <Fact label="Care of" value={aadhaar.care_of} />
                  <Fact label="Address" value={aadhaar.address} />
                  <Fact label="Pincode" value={aadhaar.pincode} />
                  <Fact label="VID" value={aadhaar.vid} />
                  <Fact label="Enrolment ID" value={aadhaar.enrollment_id} />
                </DocumentCard>
              ))}
            </div>

            {/* Only when a number was actually read and withheld. A card the
                OCR could not read a number off is a different story, and
                telling that reader about masking explains nothing. */}
            {aadhaars.some((card) => !card.aadhaar_number && card.masked_aadhaar_number) && (
              <p className="cprof-doc-note">
                Aadhaar numbers are masked, and the scan is not offered — the card is the
                number. Only an administrator is served either.
              </p>
            )}
          </section>
        )}

        {hasVerdict && (
          <section className="cprof-card" id={sectionId("verdict")}>
            <h3 className="cprof-card-title">Staff Verdict & Evaluation</h3>
            <div className="cprof-facts">
              <Fact
                label="Verdict Status"
                value={
                  candidate.evaluation_status
                    ? candidate.evaluation_status.replace("_", " ").toUpperCase()
                    : "Pending"
                }
              />
              {candidate.evaluation_score ? (
                <>
                  <div className="cprof-fact-label">Rating</div>
                  <div
                    className="cprof-fact-value"
                    style={{ display: "flex", alignItems: "center", gap: "4px", color: "var(--warning)" }}
                  >
                    {Array.from({ length: candidate.evaluation_score }).map((_, i) => (
                      <Star key={i} size={16} fill="currentColor" />
                    ))}
                    <span style={{ color: "var(--dash-ink)", fontSize: "0.85rem", marginLeft: "6px" }}>
                      ({candidate.evaluation_score} of 5)
                    </span>
                  </div>
                </>
              ) : null}
              {candidate.evaluated_at ? (
                <Fact label="Evaluated At" value={formatDateFull(new Date(candidate.evaluated_at))} />
              ) : null}
              {candidate.evaluation_notes ? (
                <React.Fragment>
                  <div className="cprof-fact-label">Evaluation Notes</div>
                  <div className="cprof-fact-value is-multiline">{candidate.evaluation_notes}</div>
                </React.Fragment>
              ) : null}
            </div>
          </section>
        )}

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

        {evaluation && (
          <aside className="cprof-verdict" aria-label="Record your evaluation">
            <div className="cprof-verdict-inner">
              <h3 className="cprof-card-title">Your verdict</h3>
              <p className="cprof-verdict-sub">
                Rate the fit, record the outcome, and say what you would ask them.
              </p>

              <div className="eval-stars">
                {[1, 2, 3, 4, 5].map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={`eval-star ${score >= value ? "is-on" : ""}`}
                    // Clicking the star you are already on clears the rating,
                    // because there is otherwise no way back to "not rated".
                    onClick={() => setScore(score === value ? 0 : value)}
                    aria-label={`Rate ${value} star${value === 1 ? "" : "s"}`}
                  >
                    <Star size={20} fill={score >= value ? "currentColor" : "none"} />
                  </button>
                ))}
                <span className="eval-score-label">{score > 0 ? `${score} of 5` : "No rating"}</span>
              </div>

              <div className="eval-choices" role="radiogroup" aria-label="Evaluation status">
                {STATUS_CHOICES.map((choice) => (
                  <button
                    key={choice.value}
                    type="button"
                    role="radio"
                    aria-checked={status === choice.value}
                    className={`eval-choice is-${choice.value} ${
                      status === choice.value ? "is-on" : ""
                    }`}
                    onClick={() => setStatus(choice.value)}
                  >
                    {choice.label}
                  </button>
                ))}
              </div>

              <div className="eval-field">
                <label htmlFor="cprof-eval-notes">Notes</label>
                <textarea
                  id="cprof-eval-notes"
                  rows={5}
                  value={notes}
                  onChange={(event) => setNotes(event.target.value)}
                  placeholder="What stood out, and what would you ask them."
                />
              </div>

              {candidate.evaluated_at && (
                <p className="cprof-verdict-stamp">
                  Last recorded {formatDateFull(new Date(candidate.evaluated_at))}.
                </p>
              )}

              <div className="eval-actions is-stacked">
                <button
                  type="button"
                  className="db-btn is-primary"
                  onClick={() => submit(true)}
                  disabled={evaluation.saving}
                  title={
                    evaluation.nextName
                      ? `Save, then open ${evaluation.nextName}`
                      : "Save — nothing else is waiting on a first read"
                  }
                >
                  {evaluation.saving ? (
                    <Loader2 size={14} className="icon-spin" />
                  ) : (
                    <ArrowRight size={14} />
                  )}
                  {evaluation.nextName ? "Save & next" : "Save & finish"}
                </button>
                <button
                  type="button"
                  className="db-btn"
                  onClick={() => submit(false)}
                  disabled={evaluation.saving}
                >
                  {evaluation.saving ? (
                    <Loader2 size={14} className="icon-spin" />
                  ) : (
                    <CheckCircle2 size={14} />
                  )}
                  Save verdict
                </button>
                {onBack && (
                  <button
                    type="button"
                    className="db-btn is-quiet"
                    onClick={onBack}
                    disabled={evaluation.saving}
                  >
                    Back to candidates
                  </button>
                )}
              </div>
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
