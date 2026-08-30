"use client";

import { useEffect, useRef, useState, type DragEvent, type FormEvent, type ReactNode } from "react";
import {
  ArrowLeft,
  BriefcaseBusiness,
  CheckCircle2,
  FileText,
  Fingerprint,
  Globe2,
  LoaderCircle,
  ScanLine,
  ShieldCheck,
  UploadCloud,
  X,
} from "lucide-react";

import Select from "@/components/ui/Select";
import {
  listCountriesAPI,
  listJobDesignationsAPI,
  type CountryRow,
  type JobDesignation,
} from "@/lib/api";

export interface CandidateUploadFiles {
  resume?: File | null;
  aadhaar?: File | null;
  passport?: File | null;
  full_name?: string;
  email?: string;
  phone?: string;
  job_id?: string;
  job_title?: string;
  destination_country?: string;
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

function UploadSlot({ id, title, copy, accept, file, icon, disabled, onChange }: {
  id: string;
  title: string;
  copy: string;
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
    if (!disabled) onChange(event.dataTransfer.files.item(0));
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
        disabled={disabled}
        onChange={(event) => onChange(event.target.files?.item(0) ?? null)}
      />
      <div className="cupload-slot-icon" aria-hidden="true">{icon}</div>
      <div className="cupload-slot-copy">
        <div className="cupload-slot-title">{title}<span>Optional</span></div>
        {file ? (
          <div className="cupload-file">
            <CheckCircle2 size={15} />
            <span title={file.name}>{file.name}</span>
            <small>{formatBytes(file.size)}</small>
          </div>
        ) : <p>{copy}</p>}
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
        <button type="button" className="cscreen-btn" disabled={disabled} onClick={() => inputRef.current?.click()}>
          <UploadCloud size={15} /> Choose file
        </button>
      )}
    </div>
  );
}

export default function CandidateUploadScreen({ saving, error = null, onBack, onSubmit }: CandidateUploadScreenProps) {
  const [resume, setResume] = useState<File | null>(null);
  const [aadhaar, setAadhaar] = useState<File | null>(null);
  const [passport, setPassport] = useState<File | null>(null);
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [jobId, setJobId] = useState("");
  const [destinationCountry, setDestinationCountry] = useState("");
  const [jobs, setJobs] = useState<JobDesignation[]>([]);
  const [countries, setCountries] = useState<CountryRow[]>([]);
  const [taxonomyError, setTaxonomyError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([listJobDesignationsAPI(), listCountriesAPI()])
      .then(([jobResponse, countryResponse]) => {
        if (cancelled) return;
        setJobs(jobResponse.items.filter((item) => item.active));
        setCountries(countryResponse.items.filter((item) => item.active));
        setTaxonomyError(null);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setTaxonomyError(reason instanceof Error ? reason.message : "Could not load job and country preferences.");
      });
    return () => { cancelled = true; };
  }, []);

  const manualIdentityComplete = Boolean(fullName.trim() && (email.trim() || phone.trim()));
  const canSubmit = Boolean(resume || manualIdentityComplete);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit || saving) return;
    onSubmit({
      resume,
      aadhaar,
      passport,
      full_name: fullName.trim(),
      email: email.trim(),
      phone: phone.trim(),
      job_id: jobId,
      job_title: jobs.find((job) => job.id === jobId)?.title,
      destination_country: destinationCountry,
    });
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
            {saving ? "Creation continues if you leave this section." : "One candidate per submission."}
          </span>
        )}
        <button type="submit" className="cscreen-btn is-primary" disabled={!canSubmit || saving}>
          {saving ? <LoaderCircle className="cupload-spin" size={16} /> : <ScanLine size={16} />}
          {saving ? "Creating candidate..." : resume ? "Extract and add candidate" : "Add candidate"}
        </button>
      </div>

      <header className="cedit-hero cupload-hero">
        <span className="cprof-monogram" aria-hidden="true"><ScanLine size={28} /></span>
        <div>
          <h2 className="cprof-name">Add candidate</h2>
          <p className="cprof-meta">Enter candidate details and preferences. A resume can be added now or later.</p>
        </div>
      </header>

      {error && <div className="cscreen-error" role="alert">{error}</div>}
      {saving && (
        <div className="cupload-active" role="status">
          <LoaderCircle className="cupload-spin" size={18} />
          <div>
            <strong>{resume || aadhaar || passport ? "VeriIS is reading the documents" : "Creating candidate"}</strong>
            <span>You can open another section. The top bar will notify you when this finishes.</span>
          </div>
        </div>
      )}

      <section className="cupload-details" aria-labelledby="candidate-details-title">
        <div className="cupload-section-title">
          <h3 id="candidate-details-title">Candidate details</h3>
          <p>Without a resume, name and either email or phone are required.</p>
        </div>
        <div className="cupload-form-grid">
          <label className="cupload-field">
            <span>Full name {!resume && <em>Required</em>}</span>
            <input className="modal-input" value={fullName} onChange={(event) => setFullName(event.target.value)} placeholder="Candidate name" autoComplete="name" disabled={saving} />
          </label>
          <label className="cupload-field">
            <span>Email</span>
            <input className="modal-input" type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="candidate@example.com" autoComplete="email" disabled={saving} />
          </label>
          <label className="cupload-field">
            <span>Phone</span>
            <input className="modal-input" type="tel" value={phone} onChange={(event) => setPhone(event.target.value)} placeholder="+91 98765 43210" autoComplete="tel" disabled={saving} />
          </label>
          <div className="cupload-field">
            <label htmlFor="candidate-job"><BriefcaseBusiness size={14} /> Job preference</label>
            <Select id="candidate-job" value={jobId} options={jobs.map((job) => ({ value: job.id, label: job.title }))} onChange={setJobId} placeholder="Select a job" disabled={saving} />
          </div>
          <div className="cupload-field">
            <label htmlFor="candidate-country"><Globe2 size={14} /> Country preference</label>
            <Select id="candidate-country" value={destinationCountry} options={countries.map((country) => ({ value: country.name, label: country.name }))} onChange={setDestinationCountry} placeholder="Select a country" disabled={saving} />
          </div>
        </div>
        {taxonomyError && <p className="cupload-taxonomy-error">{taxonomyError}</p>}
      </section>

      <div className="cupload-grid">
        <UploadSlot id="candidate-resume" title="Resume" copy="PDF, Word, text or a clear image. Maximum 20 MB." accept={RESUME_ACCEPT} file={resume} icon={<FileText size={22} />} disabled={saving} onChange={setResume} />
        <UploadSlot id="candidate-aadhaar" title="Aadhaar" copy="Upload a clear scan or a PDF containing both sides." accept={IDENTITY_ACCEPT} file={aadhaar} icon={<Fingerprint size={22} />} disabled={saving} onChange={setAadhaar} />
        <UploadSlot id="candidate-passport" title="Passport" copy="Upload the full passport data page with a readable MRZ." accept={IDENTITY_ACCEPT} file={passport} icon={<ShieldCheck size={22} />} disabled={saving} onChange={setPassport} />
      </div>

      <section className="cupload-process" aria-label="Candidate creation process">
        <div><span>1</span><strong>Enter details</strong><small>Add contact and work preferences.</small></div>
        <div><span>2</span><strong>Optional OCR</strong><small>VeriIS reads any documents you attach.</small></div>
        <div><span>3</span><strong>Candidate created</strong><small>Only approved CRM fields are shown.</small></div>
      </section>

      <div className="cupload-notice">
        <ShieldCheck size={18} />
        <p>Aadhaar stays in the protected identity collection and remains masked for staff. Raw VeriIS payloads are never returned to this screen.</p>
      </div>
    </form>
  );
}
