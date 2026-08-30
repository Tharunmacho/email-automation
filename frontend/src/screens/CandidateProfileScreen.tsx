"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Download,
  FileText,
  FolderGit2,
  Link as LinkIcon,
  Loader2,
  Mail,
  MapPin,
  Phone,
  ShieldCheck,
  Star,
} from "lucide-react";

import {
  flattenExtras,
  highestQualificationOf,
  industryOf,
  isBlankValue,
  toBullets,
  toEditableState,
} from "@/lib/candidateProfile";
import {
  fetchIdentityDocuments,
  getToken,
  resumeDownloadUrl,
  type AnsweredQuestion,
  type CandidateRecord,
  type EvaluationStatus,
  type IdentityDocument,
  type IdentityDocuments,
} from "@/lib/api";
import { formatDateFull, initialsOf } from "@/lib/format";

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
 * A question and its answer, side by side.
 *
 * The question is shown, not assumed. Some of the bot's trade questions are
 * written for one candidate — for a job no standard pack covers — so their text
 * exists nowhere but on that record: "TIG, MIG" with no question above it is a
 * value nobody can interpret. Their own wording is kept underneath where they
 * typed rather than tapped, because a standardised answer loses the detail and
 * the detail is often the point.
 */
function QuestionList({ items }: { items: AnsweredQuestion[] }) {
  if (items.length === 0) return null;
  return (
    <div className="cprof-entries">
      {items.map((item) => (
        <article key={item.id} className="cprof-entry">
          <h4 className="cprof-entry-title">{item.question}</h4>
          <p className="cprof-prose">{item.answer}</p>
          {item.raw && item.raw.trim() && item.raw.trim() !== item.answer.trim() ? (
            <p className="cprof-entry-meta">In their words: “{item.raw.trim()}”</p>
          ) : null}
        </article>
      ))}
    </div>
  );
}

/**
 * One identity document, as much of it as the reader is allowed to see.
 *
 * The masked number is what is drawn wherever both exist. A recruiter has to
 * know *which* card is on file and that it reads correctly; the full number is
 * a different question with a different answer, and the endpoint only sends it
 * to an administrator — so when it is here at all, it is here deliberately.
 *
 * `check_digits_valid: false` and `aadhaar_number_valid: false` are surfaced
 * rather than hidden. They mean the OCR misread a character, which matters
 * precisely because the number is what somebody will key into a visa
 * application from this screen.
 */
function IdentityCard({ document }: { document: IdentityDocument }) {
  const aadhaar = document.document_type !== "passport";
  const suspect = aadhaar
    ? document.aadhaar_number_valid === false
    : document.check_digits_valid === false;

  return (
    <article className="cprof-entry">
      <h4 className="cprof-entry-title">
        {aadhaar
          ? document.masked_aadhaar_number || document.aadhaar_number || "Aadhaar card"
          : document.passport_number || "Passport"}
        {document.document_side ? (
          <span className="cprof-entry-at"> · {document.document_side}</span>
        ) : null}
      </h4>

      <div className="cprof-facts">
        {aadhaar ? (
          <>
            <Fact label="Name" value={document.name} />
            <Fact label="Date of birth" value={document.date_of_birth} />
            <Fact label="Gender" value={document.gender} />
            <Fact label="Address" value={document.address} />
            <Fact label="Care of" value={document.care_of} />
            <Fact label="PIN code" value={document.pincode} />
            {/* Present only for an administrator. */}
            <Fact label="Number" value={document.aadhaar_number} />
          </>
        ) : (
          <>
            <Fact
              label="Name"
              value={[document.given_names, document.surname].filter(Boolean).join(" ")}
            />
            <Fact label="Date of birth" value={document.date_of_birth} />
            <Fact label="Nationality" value={document.nationality} />
            <Fact label="Issued" value={document.date_of_issue} />
            <Fact label="Expires" value={document.expiry_date} />
            <Fact label="Issuing country" value={document.issuing_country} />
          </>
        )}
        <Fact label="File" value={document.source?.filename} />
      </div>

      {suspect ? (
        <p className="cprof-entry-meta">
          The number failed its own check — the scan was probably misread. Confirm it against
          the document before using it.
        </p>
      ) : null}
    </article>
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
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  // The verdict controls, seeded from whatever is already on the record so
  // re-opening an evaluated profile shows the decision that was made rather
  // than an empty form.
  //
  // Seeded once, from the initial value, and that is sufficient: "Save & next"
  // swaps the candidate under this screen, and the caller keys it on the
  // candidate id so a different candidate is a different component instance.
  // Resetting these in an effect instead would render the previous reviewer's
  // notes for a frame before correcting itself.
  const [score, setScore] = useState(candidate.evaluation_score ?? 0);
  const [status, setStatus] = useState<EvaluationStatus>(
    candidate.evaluation_status && candidate.evaluation_status !== "pending"
      ? candidate.evaluation_status
      : "shortlisted",
  );
  const [notes, setNotes] = useState(candidate.evaluation_notes ?? "");

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

  /**
   * The Aadhaar and passport, fetched separately because they are stored
   * separately — see `fetchIdentityDocuments`. Empty until it answers, and
   * empty for good if there are none, which is most candidates.
   */
  const [identity, setIdentity] = useState<IdentityDocuments | null>(null);

  useEffect(() => {
    let live = true;
    // Only for candidates who could have one. An email candidate's application
    // is a résumé; the identity collections are fed by the bot and by the
    // bundle reader, and asking for a candidate who has neither is a request
    // whose answer is always empty.
    if (candidate.source !== "whatsapp") {
      setIdentity(null);
      return;
    }
    fetchIdentityDocuments(candidate.id).then((found) => {
      if (live) setIdentity(found);
    });
    return () => {
      live = false;
    };
  }, [candidate.id, candidate.source]);

  const identityDocuments = useMemo(
    () => [...(identity?.aadhaar ?? []), ...(identity?.passport ?? [])],
    [identity],
  );

  const job = candidate.job ?? null;
  const registration = candidate.registration ?? null;

  /** Everything the job panel would draw, so an empty one is never drawn. */
  const hasJob = Boolean(
    job &&
      (job.job ||
        job.job_category ||
        job.availability?.band ||
        job.availability?.date ||
        job.country?.destination_country ||
        job.country?.preference ||
        (job.country?.selected?.length ?? 0) > 0 ||
        (job.questions?.length ?? 0) > 0 ||
        job.course_or_trade?.course ||
        job.course_or_trade?.primary_trade ||
        (job.course_or_trade?.questions?.length ?? 0) > 0),
  );

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
    { id: "verdict", label: "Verdict & Evaluation", present: hasVerdict },
    { id: "job", label: "Job", present: hasJob },
    { id: "identity", label: "Identity documents", present: identityDocuments.length > 0 },
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

  /** Where they are, and where they want to go — shown as two separate facts. */
  const originLine = [
    fromWhatsApp ? "WhatsApp" : "Email",
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
              {verifying ? "Verifying…" : isVerified ? "Verified profile" : "Verify profile"}
            </button>
          )}
        </div>
      </div>

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
            <Fact label="Phone" value={view.phone} />
            <Fact label="LinkedIn" value={view.linkedin} />
            <Fact label="GitHub" value={view.github} />
            <Fact label="Address" value={view.location} />
          </div>
        </section>

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
                    style={{ display: "flex", alignItems: "center", gap: "4px", color: "#f59e0b" }}
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

        {hasJob && job && (
          <section className="cprof-card" id={sectionId("job")}>
            <h3 className="cprof-card-title">Job</h3>

            <div className="cprof-facts">
              <Fact label="Job wanted" value={job.job} />
              <Fact label="Category" value={job.job_category_title || job.job_category} />
              <Fact
                label="Course / trade"
                value={
                  [job.course_or_trade?.course, job.course_or_trade?.primary_trade_title]
                    .filter(Boolean)
                    .join(" · ") || null
                }
              />
              <Fact label="Highest qualification" value={job.course_or_trade?.education} />
              <Fact
                label="Destination"
                value={job.country?.destination_country || job.country?.preference}
              />
              <Fact
                label="Countries chosen"
                value={
                  (job.country?.selected_names?.length
                    ? job.country.selected_names
                    : job.country?.selected ?? []
                  ).join(", ") || null
                }
              />
              {/* Spelled out rather than shown as a code, because it is the one
                  line on this panel that constrains what a recruiter may do:
                  a strict candidate must not be shortlisted outside their list
                  without being asked first. */}
              <Fact
                label="Country preference"
                value={
                  job.country?.strict
                    ? "Strict — do not shortlist outside the countries listed without asking"
                    : job.country?.strictness
                      ? "Open to other countries"
                      : null
                }
              />
              <Fact
                label="Can join"
                value={
                  [job.availability?.band, job.availability?.date]
                    .filter(Boolean)
                    .join(" · ") || job.availability?.note || null
                }
              />
            </div>

            {(job.course_or_trade?.questions?.length ?? 0) > 0 && (
              <>
                <h4 className="cprof-entry-title" style={{ marginTop: "1rem" }}>
                  About the course and trade
                </h4>
                <QuestionList items={job.course_or_trade!.questions!} />
              </>
            )}

            {(job.questions?.length ?? 0) > 0 && (
              <>
                <h4 className="cprof-entry-title" style={{ marginTop: "1rem" }}>
                  About the job
                </h4>
                <QuestionList items={job.questions!} />
              </>
            )}

            {/* Said once, at the foot of the panel it qualifies. A blank on a
                half-finished registration is a question nobody has asked yet,
                not an answer of "none". */}
            {registration && !registration.complete ? (
              <p className="cprof-entry-meta" style={{ marginTop: "1rem" }}>
                This registration is still in progress
                {registration.outstanding_documents?.length
                  ? ` — still to come: ${registration.outstanding_documents.join(", ")}`
                  : ""}
                . Anything blank here has not been asked yet.
              </p>
            ) : null}
          </section>
        )}

        {identityDocuments.length > 0 && (
          <section className="cprof-card" id={sectionId("identity")}>
            <h3 className="cprof-card-title">
              <ShieldCheck size={15} style={{ verticalAlign: "-2px", marginRight: "6px" }} />
              Identity documents
            </h3>
            <div className="cprof-entries">
              {identityDocuments.map((document, index) => (
                <IdentityCard key={document._id ?? index} document={document} />
              ))}
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
