"use client";

import { useRef, useState, type DragEvent, type FormEvent, type ReactNode } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  FileText,
  Fingerprint,
  LoaderCircle,
  ScanLine,
  ShieldCheck,
  UploadCloud,
  X,
} from "lucide-react";

export interface CandidateUploadFiles {
  resume: File;
  aadhaar?: File | null;
  passport?: File | null;
}

interface CandidateUploadScreenProps {
  saving: boolean;
  error?: string | null;
  onBack?: () => void;
  onSubmit: (files: CandidateUploadFiles) => void;
}

const IDENTITY_ACCEPT = ".pdf,.jpg,.jpeg,.png,.webp,.tif,.tiff,.heic,.heif";
const RESUME_ACCEPT = `${IDENTITY_ACCEPT},.doc,.docx,.rtf,.txt`;

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function UploadSlot({
  id,
  title,
  copy,
  required = false,
  accept,
  file,
  icon,
  disabled,
  onChange,
}: {
  id: string;
  title: string;
  copy: string;
  required?: boolean;
  accept: string;
  file: File | null;
  icon: ReactNode;
  disabled: boolean;
  onChange: (file: File | null) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const takeDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    if (disabled) return;
    onChange(event.dataTransfer.files.item(0));
  };

  return (
    <div
      className={`cupload-slot ${file ? "has-file" : ""} ${dragging ? "is-dragging" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={takeDrop}
    >
      <input
        ref={inputRef}
        id={id}
        className="cupload-native"
        type="file"
        accept={accept}
        required={required}
        disabled={disabled}
        onChange={(event) => onChange(event.target.files?.item(0) ?? null)}
      />

      <div className="cupload-slot-icon" aria-hidden="true">{icon}</div>
      <div className="cupload-slot-copy">
        <div className="cupload-slot-title">
          {title}
          <span className={required ? "is-required" : ""}>{required ? "Required" : "Optional"}</span>
        </div>
        {file ? (
          <div className="cupload-file">
            <CheckCircle2 size={15} />
            <span title={file.name}>{file.name}</span>
            <small>{formatBytes(file.size)}</small>
          </div>
        ) : (
          <p>{copy}</p>
        )}
      </div>

      {file ? (
        <button
          type="button"
          className="cupload-remove"
          aria-label={`Remove ${title}`}
          disabled={disabled}
          onClick={() => {
            onChange(null);
            if (inputRef.current) inputRef.current.value = "";
          }}
        >
          <X size={16} />
        </button>
      ) : (
        <button
          type="button"
          className="cscreen-btn"
          disabled={disabled}
          onClick={() => inputRef.current?.click()}
        >
          <UploadCloud size={15} /> Choose file
        </button>
      )}
    </div>
  );
}

export default function CandidateUploadScreen({
  saving,
  error = null,
  onBack,
  onSubmit,
}: CandidateUploadScreenProps) {
  const [resume, setResume] = useState<File | null>(null);
  const [aadhaar, setAadhaar] = useState<File | null>(null);
  const [passport, setPassport] = useState<File | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!resume || saving) return;
    onSubmit({ resume, aadhaar, passport });
  };

  return (
    <form className="cscreen cupload" onSubmit={submit} style={{ animation: "fadeIn 0.3s ease" }}>
      <div className={`cscreen-topbar ${onBack ? "" : "cupload-toolbar"}`}>
        {onBack ? (
          <button type="button" className="jod-back" onClick={onBack} disabled={saving}>
            <ArrowLeft size={15} /> Back to candidates
          </button>
        ) : (
          <span className="cupload-toolbar-note">
            {saving ? "Extraction continues if you leave this section." : "One candidate per submission."}
          </span>
        )}
        <button type="submit" className="cscreen-btn is-primary" disabled={!resume || saving}>
          {saving ? <LoaderCircle className="cupload-spin" size={16} /> : <ScanLine size={16} />}
          {saving ? "VeriIS is extracting…" : "Extract and add candidate"}
        </button>
      </div>

      <header className="cedit-hero cupload-hero">
        <span className="cprof-monogram" aria-hidden="true"><ScanLine size={28} /></span>
        <div>
          <h2 className="cprof-name">Upload candidate documents</h2>
          <p className="cprof-meta">
            Add the files only. VeriIS will read them and create the structured candidate profile.
          </p>
        </div>
      </header>

      {error && <div className="cscreen-error" role="alert">{error}</div>}
      {saving && (
        <div className="cupload-active" role="status">
          <LoaderCircle className="cupload-spin" size={18} />
          <div>
            <strong>VeriIS is reading the uploaded documents</strong>
            <span>You can open another section. The top bar will notify you when extraction finishes.</span>
          </div>
        </div>
      )}

      <div className="cupload-grid">
        <UploadSlot
          id="candidate-resume"
          title="Résumé"
          copy="PDF, Word, text or a clear image. Maximum 20 MB."
          required
          accept={RESUME_ACCEPT}
          file={resume}
          icon={<FileText size={22} />}
          disabled={saving}
          onChange={setResume}
        />
        <UploadSlot
          id="candidate-aadhaar"
          title="Aadhaar"
          copy="Upload a clear scan or a PDF containing both sides."
          accept={IDENTITY_ACCEPT}
          file={aadhaar}
          icon={<Fingerprint size={22} />}
          disabled={saving}
          onChange={setAadhaar}
        />
        <UploadSlot
          id="candidate-passport"
          title="Passport"
          copy="Upload the full passport data page with a readable MRZ."
          accept={IDENTITY_ACCEPT}
          file={passport}
          icon={<ShieldCheck size={22} />}
          disabled={saving}
          onChange={setPassport}
        />
      </div>

      <section className="cupload-process" aria-label="Extraction process">
        <div><span>1</span><strong>Upload</strong><small>Your original files are validated.</small></div>
        <div><span>2</span><strong>VeriIS OCR</strong><small>Each file uses its dedicated extraction mode.</small></div>
        <div><span>3</span><strong>Candidate created</strong><small>Only approved CRM fields are shown.</small></div>
      </section>

      <div className="cupload-notice">
        <ShieldCheck size={18} />
        <p>
          Aadhaar stays in the protected identity collection and remains masked for staff.
          Raw VeriIS payloads are never returned to this screen.
        </p>
      </div>
    </form>
  );
}
