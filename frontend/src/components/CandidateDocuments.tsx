"use client";

/**
 * A candidate's CV and every other file on their record, and — for whoever
 * may edit the candidate — the controls to add to them.
 *
 * Uploads take effect the moment they finish. They are not part of an edit
 * screen's "Save changes": a file is either on the record or it is not, and
 * holding one in the browser until a form is saved is how files get lost.
 *
 * Replacing the CV never deletes the old one. The server keeps it as a
 * "Previous CV", because a verdict may already rest on it and an emailed
 * candidate's passport pages are cut out of it.
 */

import { useMemo, useRef, useState, type ChangeEvent } from "react";
import { Download, FileText, Loader2, Paperclip, Plus, Trash2, Upload, X } from "lucide-react";

import Select from "@/components/ui/Select";
import {
  addCandidateAttachment,
  attachmentDownloadUrl,
  deleteCandidateAttachment,
  replaceCandidateResume,
  resumeDownloadUrl,
  type AttachmentKind,
  type CandidateAttachment,
  type CandidateReadContext,
  type CandidateRecord,
} from "@/lib/api";
import { saveFile } from "@/lib/download";
import { formatDateFull } from "@/lib/format";

/** The server's limit (`MAX_RESUME_BYTES`), checked here to fail before the upload. */
const MAX_BYTES = 20 * 1024 * 1024;
const ACCEPT = ".pdf,.doc,.docx,.rtf,.txt,.jpg,.jpeg,.png,.heic,.heif,.webp,.tif,.tiff";

const KIND_LABELS: Record<AttachmentKind, string> = {
  certificate: "Certificate",
  experience_letter: "Experience letter",
  passport: "Passport copy",
  photo: "Photo",
  visa: "Visa",
  medical: "Medical report",
  offer_letter: "Offer letter",
  other: "Other document",
  previous_cv: "Previous CV",
};

/** What a recruiter can pick. `previous_cv` is assigned by the server alone. */
const PICKABLE: AttachmentKind[] = [
  "certificate",
  "experience_letter",
  "passport",
  "photo",
  "visa",
  "medical",
  "offer_letter",
  "other",
];

function formatSize(bytes?: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function tooLarge(file: File): string | null {
  return file.size > MAX_BYTES
    ? `${file.name} is ${formatSize(file.size)}; the limit is ${formatSize(MAX_BYTES)}.`
    : null;
}

interface CandidateDocumentsProps {
  candidate: CandidateRecord;
  readContext?: CandidateReadContext;
  /** May replace the CV and add attachments — the same rule as editing the profile. */
  canManage: boolean;
  /** Administrators may remove any attachment; everyone else only their own uploads. */
  isAdmin: boolean;
  currentUserId?: string;
  /** The server has changed the record: re-read it, and log `summary`. */
  onChanged: (summary: string) => void;
  onToast: (message: string, type?: "info" | "success" | "error") => void;
}

export default function CandidateDocuments({
  candidate,
  readContext,
  canManage,
  isAdmin,
  currentUserId,
  onChanged,
  onToast,
}: CandidateDocumentsProps) {
  const cvInput = useRef<HTMLInputElement>(null);
  const attachmentInput = useRef<HTMLInputElement>(null);

  /** One operation at a time; the id names which row's spinner to show. */
  const [busy, setBusy] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [kind, setKind] = useState<AttachmentKind>("certificate");
  const [label, setLabel] = useState("");
  const [file, setFile] = useState<File | null>(null);

  const resume = candidate.resume;
  const hasResume = Boolean(resume?.storage_key || resume?.original_filename);

  // Newest first, and replaced CVs after everything a person chose to attach:
  // they are history, the rest is the candidate's current paperwork.
  const attachments = useMemo(
    () =>
      [...(candidate.attachments ?? [])].sort((a, b) => {
        const history = Number(a.kind === "previous_cv") - Number(b.kind === "previous_cv");
        return history || new Date(b.uploaded_at).getTime() - new Date(a.uploaded_at).getTime();
      }),
    [candidate.attachments],
  );

  const run = async (
    id: string,
    work: () => Promise<CandidateRecord>,
    done: string,
    summary: string,
  ) => {
    setBusy(id);
    try {
      const updated = await work();
      onToast(updated.notice || done, updated.notice ? "info" : "success");
      // A same-file upload comes back with a notice and changes nothing.
      if (!updated.notice) onChanged(summary);
      return true;
    } catch (err) {
      onToast(err instanceof Error ? err.message : "That did not work. Try again.", "error");
      return false;
    } finally {
      setBusy(null);
    }
  };

  const download = async (id: string, url: string, name: string) => {
    setBusy(id);
    try {
      await saveFile(url, name);
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not download the file.", "error");
    } finally {
      setBusy(null);
    }
  };

  const pickCv = (event: ChangeEvent<HTMLInputElement>) => {
    const chosen = event.target.files?.[0];
    event.target.value = "";
    if (!chosen) return;
    const problem = tooLarge(chosen);
    if (problem) {
      onToast(problem, "error");
      return;
    }
    if (
      hasResume &&
      !confirm(
        `Replace the current CV with "${chosen.name}"?\n\nThe current CV is kept on the record as "Previous CV".`,
      )
    ) {
      return;
    }
    void run(
      "cv",
      () => replaceCandidateResume(candidate.id, chosen),
      "CV updated.",
      hasResume
        ? `CV replaced with ${chosen.name}; the previous CV is kept.`
        : `CV uploaded: ${chosen.name}.`,
    );
  };

  const pickAttachment = (event: ChangeEvent<HTMLInputElement>) => {
    const chosen = event.target.files?.[0] ?? null;
    event.target.value = "";
    if (chosen) {
      const problem = tooLarge(chosen);
      if (problem) {
        onToast(problem, "error");
        return;
      }
    }
    setFile(chosen);
  };

  const closeForm = () => {
    setAdding(false);
    setFile(null);
    setLabel("");
    setKind("certificate");
  };

  const upload = async () => {
    if (!file) return;
    const ok = await run(
      "attach",
      () => addCandidateAttachment(candidate.id, file, kind, label.trim()),
      `${KIND_LABELS[kind]} attached.`,
      `${KIND_LABELS[kind]} attached: ${file.name}.`,
    );
    if (ok) closeForm();
  };

  const remove = (attachment: CandidateAttachment) => {
    const name = attachment.label || KIND_LABELS[attachment.kind];
    if (!confirm(`Remove "${name}" (${attachment.original_filename}) from this candidate?`)) return;
    void run(
      attachment.id,
      () => deleteCandidateAttachment(candidate.id, attachment.id),
      "Attachment removed.",
      `${KIND_LABELS[attachment.kind]} removed: ${attachment.original_filename}.`,
    );
  };

  const canRemove = (attachment: CandidateAttachment) =>
    canManage &&
    attachment.kind !== "previous_cv" &&
    (isAdmin || (Boolean(currentUserId) && attachment.uploaded_by === currentUserId));

  return (
    <section className="cprof-card cdocs">
      <div className="cdocs-head">
        <h3 className="cprof-card-title">CV &amp; attachments</h3>
        {canManage && (
          <div className="cdocs-head-actions">
            <button
              type="button"
              className="cscreen-btn"
              onClick={() => cvInput.current?.click()}
              disabled={busy !== null}
            >
              {busy === "cv" ? <Loader2 size={15} className="icon-spin" /> : <Upload size={15} />}
              {busy === "cv" ? "Uploading…" : hasResume ? "Upload new CV" : "Upload CV"}
            </button>
            <button
              type="button"
              className="cscreen-btn"
              onClick={() => setAdding(true)}
              disabled={busy !== null || adding}
            >
              <Plus size={15} /> Add attachment
            </button>
            <input ref={cvInput} type="file" accept={ACCEPT} hidden onChange={pickCv} />
          </div>
        )}
      </div>

      {adding && (
        <div className="cdocs-form">
          <div className="cdocs-form-grid">
            <label className="cedit-field">
              <span className="cedit-label">Document type</span>
              <Select
                value={kind}
                options={PICKABLE.map((value) => ({ value, label: KIND_LABELS[value] }))}
                onChange={(value) => setKind(value as AttachmentKind)}
                ariaLabel="Document type"
              />
            </label>
            <label className="cedit-field">
              <span className="cedit-label">
                Description <span className="cedit-hint">optional</span>
              </span>
              <input
                className="cedit-input"
                type="text"
                value={label}
                maxLength={120}
                placeholder="e.g. 6G welding certificate"
                onChange={(event) => setLabel(event.target.value)}
              />
            </label>
          </div>

          <div className="cdocs-form-file">
            <button
              type="button"
              className="cscreen-btn"
              onClick={() => attachmentInput.current?.click()}
              disabled={busy !== null}
            >
              <Paperclip size={15} /> {file ? "Choose another file" : "Choose file"}
            </button>
            <span className="cdocs-form-filename">
              {file ? `${file.name} · ${formatSize(file.size)}` : "PDF, Word or image, up to 20 MB"}
            </span>
            <input ref={attachmentInput} type="file" accept={ACCEPT} hidden onChange={pickAttachment} />
          </div>

          <div className="cdocs-form-actions">
            <button type="button" className="cscreen-btn" onClick={closeForm} disabled={busy === "attach"}>
              <X size={15} /> Cancel
            </button>
            <button
              type="button"
              className="cscreen-btn is-primary"
              onClick={() => void upload()}
              disabled={!file || busy !== null}
            >
              {busy === "attach" ? <Loader2 size={15} className="icon-spin" /> : <Upload size={15} />}
              {busy === "attach" ? "Uploading…" : "Upload"}
            </button>
          </div>
        </div>
      )}

      <ul className="cdocs-list">
        <li className="cdocs-row">
          <span className="cprof-doc-icon" aria-hidden="true">
            <FileText size={15} />
          </span>
          <span className="cdocs-row-text">
            <strong>
              {hasResume ? resume?.original_filename || "CV" : "No CV on file"}
              {hasResume && <em className="cprof-doc-badge is-ok">Current CV</em>}
            </strong>
            <small>
              {hasResume
                ? formatSize(resume?.size) || "CV"
                : candidate.cv_required === false
                  ? "Not required for this role"
                  : canManage
                    ? "Use “Upload CV” to add one."
                    : "Nothing has been uploaded."}
            </small>
          </span>
          {hasResume && (
            <span className="cdocs-row-actions">
              <button
                type="button"
                className="cprof-doc-download"
                onClick={() =>
                  void download("resume", resumeDownloadUrl(candidate.id, readContext), resume?.original_filename || "resume.pdf")
                }
                disabled={busy !== null}
              >
                {busy === "resume" ? <Loader2 size={13} className="icon-spin" /> : <Download size={13} />}
                Download
              </button>
            </span>
          )}
        </li>

        {attachments.map((attachment) => (
          <li key={attachment.id} className={`cdocs-row ${attachment.kind === "previous_cv" ? "is-history" : ""}`}>
            <span className="cprof-doc-icon" aria-hidden="true">
              {attachment.kind === "previous_cv" ? <FileText size={15} /> : <Paperclip size={15} />}
            </span>
            <span className="cdocs-row-text">
              <strong>
                {attachment.label && attachment.kind !== "previous_cv"
                  ? attachment.label
                  : KIND_LABELS[attachment.kind] ?? "Document"}
                {attachment.label && attachment.kind !== "previous_cv" && (
                  <em className="cprof-doc-badge">{KIND_LABELS[attachment.kind] ?? attachment.kind}</em>
                )}
              </strong>
              <small>
                {[
                  attachment.original_filename,
                  formatSize(attachment.size),
                  attachment.kind === "previous_cv"
                    ? `replaced ${formatDateFull(new Date(attachment.uploaded_at))}`
                    : `added ${formatDateFull(new Date(attachment.uploaded_at))}${
                        attachment.uploaded_by_name ? ` by ${attachment.uploaded_by_name}` : ""
                      }`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </small>
            </span>
            <span className="cdocs-row-actions">
              <button
                type="button"
                className="cprof-doc-download"
                onClick={() =>
                  void download(
                    attachment.id,
                    attachmentDownloadUrl(candidate.id, attachment.id, readContext),
                    attachment.original_filename,
                  )
                }
                disabled={busy !== null}
              >
                {busy === attachment.id ? <Loader2 size={13} className="icon-spin" /> : <Download size={13} />}
                Download
              </button>
              {canRemove(attachment) && (
                <button
                  type="button"
                  className="cedit-remove"
                  onClick={() => remove(attachment)}
                  disabled={busy !== null}
                  aria-label={`Remove ${attachment.original_filename}`}
                  title="Remove this attachment"
                >
                  <Trash2 size={14} />
                </button>
              )}
            </span>
          </li>
        ))}
      </ul>

      {canManage && (
        <p className="cprof-doc-note">
          Uploads are saved immediately. A replaced CV is kept as &ldquo;Previous CV&rdquo;.
        </p>
      )}
    </section>
  );
}
