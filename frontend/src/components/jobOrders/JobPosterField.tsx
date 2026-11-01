"use client";

import { useRef, useState } from "react";
import { ImagePlus, Loader2, Trash2 } from "lucide-react";

import { jobPosterSrc, uploadJobOrderPosterAPI } from "@/lib/api";

// JPG and PNG only: the poster is the picture on the agents' WhatsApp message,
// and WhatsApp's image header accepts nothing else, at 5 MB at most.
const POSTER_TYPES = ["image/jpeg", "image/png"];
const POSTER_MAX_BYTES = 5 * 1024 * 1024;

interface JobPosterFieldProps {
  /** `/public/job-posters/…` from `uploadJobOrderPosterAPI`, or an absolute URL. */
  value?: string;
  onChange: (next: string | undefined) => void;
  /** A new order cannot be created without one; there is then no Remove button. */
  required?: boolean;
  /** Lets the dialog hold its save button while a poster is still uploading. */
  onUploadingChange?: (uploading: boolean) => void;
}

/**
 * The job order's poster, in its own section of the job order dialogs.
 *
 * It is not a website-only setting: agents get it as the picture on the new
 * job order WhatsApp message, and the website's Current Jobs card shows it.
 */
export default function JobPosterField({ value, onChange, required = false, onUploadingChange }: JobPosterFieldProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  const busy = (next: boolean) => {
    setUploading(next);
    onUploadingChange?.(next);
  };

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    setError("");
    if (!POSTER_TYPES.includes(file.type)) {
      setError("Use a JPG or PNG image — WhatsApp does not accept other types.");
      return;
    }
    if (file.size > POSTER_MAX_BYTES) {
      setError("The poster is larger than 5 MB.");
      return;
    }
    busy(true);
    try {
      const { url } = await uploadJobOrderPosterAPI(file);
      onChange(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The poster could not be uploaded.");
    } finally {
      busy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <fieldset className="jo-web jo-poster">
      <legend className="jo-web-legend">
        <ImagePlus size={14} aria-hidden="true" />
        Job poster{required && <span className="jo-required" aria-hidden="true">*</span>}
      </legend>

      <div className="jo-poster-body">
        <div className="jo-web-poster">
          {value ? (
            <figure className="jo-web-poster-frame">
              {/* eslint-disable-next-line @next/next/no-img-element -- an API-served upload, not a static asset */}
              <img src={jobPosterSrc(value)} alt="Job poster preview" />
              <div className="jo-web-poster-actions">
                <button type="button" className="jod-btn" onClick={() => fileRef.current?.click()} disabled={uploading}>
                  {uploading ? <Loader2 size={13} className="icon-spin" /> : <ImagePlus size={13} />}
                  <span>Replace</span>
                </button>
                {!required && (
                  <button
                    type="button"
                    className="jod-btn is-danger"
                    onClick={() => onChange(undefined)}
                    disabled={uploading}
                    aria-label="Remove poster"
                  >
                    <Trash2 size={13} />
                  </button>
                )}
              </div>
            </figure>
          ) : (
            <button
              type="button"
              className="jo-web-poster-drop"
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
              aria-label={required ? "Upload job poster (required)" : "Upload job poster"}
            >
              {uploading ? <Loader2 size={20} className="icon-spin" /> : <ImagePlus size={20} />}
              <span className="jo-web-poster-title">{uploading ? "Uploading…" : "Upload poster"}</span>
              <span className="jo-web-poster-note">JPG or PNG · up to 5 MB</span>
            </button>
          )}
          <input
            ref={fileRef}
            type="file"
            accept={POSTER_TYPES.join(",")}
            hidden
            onChange={(e) => void handleFile(e.target.files?.[0])}
          />
        </div>

        <div className="jo-poster-copy">
          <p>
            Agents get this poster with the new job order message on WhatsApp, and it is the picture on the
            website&rsquo;s job card.
          </p>
          {required && !value && !uploading && (
            <p className="jo-poster-needed">Upload the poster to create the order.</p>
          )}
          {error && (
            <p className="jo-web-error" role="alert">
              {error}
            </p>
          )}
        </div>
      </div>
    </fieldset>
  );
}
