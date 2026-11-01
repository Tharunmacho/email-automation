"use client";

import { useRef, useState } from "react";
import { Globe, ImagePlus, Loader2, Trash2 } from "lucide-react";

import Checkbox from "@/components/ui/Checkbox";
import Select from "@/components/ui/Select";
import { jobPosterSrc, uploadJobOrderPosterAPI } from "@/lib/api";
import type { JobOrderWebsite, JobOrderWebsiteType } from "@/types";

const JOB_TYPES: JobOrderWebsiteType[] = ["Full time", "Contract", "Part time", "Remote"];
const JOB_TYPE_OPTIONS = JOB_TYPES.map((value) => ({ value, label: value }));
const POSTER_TYPES = ["image/jpeg", "image/png", "image/webp"];
const POSTER_MAX_BYTES = 5 * 1024 * 1024;

/** A new order is listed on the website unless someone says otherwise. */
export function websiteDefaults(existing?: JobOrderWebsite): JobOrderWebsite {
  return { show: true, jobType: "Full time", ...(existing ?? {}) };
}

export function websiteIsShown(website?: JobOrderWebsite): boolean {
  return website?.show === true;
}

/** Trimmed, with empty fields left out, as it is saved on the order. */
export function cleanWebsite(website: JobOrderWebsite): JobOrderWebsite {
  const out: JobOrderWebsite = { show: websiteIsShown(website) };
  const text = (value?: string) => (value ?? "").trim();
  if (text(website.posterUrl)) out.posterUrl = text(website.posterUrl);
  if (text(website.city)) out.city = text(website.city);
  if (text(website.category)) out.category = text(website.category);
  if (website.jobType) out.jobType = website.jobType;
  if (text(website.description)) out.description = text(website.description);
  return out;
}

interface WebsiteListingFieldsProps {
  value: JobOrderWebsite;
  onChange: (next: JobOrderWebsite) => void;
  /** The order's industry, shown as the tag when no category is given. */
  industry?: string;
  /** Lets the dialog hold its save button while a poster is still uploading. */
  onUploadingChange?: (uploading: boolean) => void;
}

/**
 * The "Website listing" part of the job order dialogs.
 *
 * Everything here is public: it becomes the card on the website's Current Jobs
 * page while the order is open. The order's remarks stay internal.
 */
export default function WebsiteListingFields({
  value,
  onChange,
  industry,
  onUploadingChange,
}: WebsiteListingFieldsProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [posterError, setPosterError] = useState("");
  const shown = websiteIsShown(value);
  const set = (patch: Partial<JobOrderWebsite>) => onChange({ ...value, ...patch });

  const busy = (next: boolean) => {
    setUploading(next);
    onUploadingChange?.(next);
  };

  const handlePoster = async (file: File | undefined) => {
    if (!file) return;
    setPosterError("");
    if (!POSTER_TYPES.includes(file.type)) {
      setPosterError("Use a JPG, PNG or WebP image.");
      return;
    }
    if (file.size > POSTER_MAX_BYTES) {
      setPosterError("The poster is larger than 5 MB.");
      return;
    }
    busy(true);
    try {
      const { url } = await uploadJobOrderPosterAPI(file);
      set({ posterUrl: url });
    } catch (err) {
      setPosterError(err instanceof Error ? err.message : "The poster could not be uploaded.");
    } finally {
      busy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const industryTag = industry && industry !== "General" ? industry : "";

  return (
    <fieldset className="jo-web">
      <legend className="jo-web-legend">
        <Globe size={14} aria-hidden="true" />
        Website listing
      </legend>

      <Checkbox
        checked={shown}
        onChange={(next) => set({ show: next })}
        label="Show on adiragroups.com"
        hint="Listed on the Current Jobs page while this order is open, and taken down when it is closed."
      />

      {shown && (
        <>
          <div className="jo-web-grid">
            <div className="jo-web-poster">
              {value.posterUrl ? (
                <figure className="jo-web-poster-frame">
                  {/* eslint-disable-next-line @next/next/no-img-element -- an API-served upload, not a static asset */}
                  <img src={jobPosterSrc(value.posterUrl)} alt="Job poster preview" />
                  <div className="jo-web-poster-actions">
                    <button type="button" className="jod-btn" onClick={() => fileRef.current?.click()} disabled={uploading}>
                      {uploading ? <Loader2 size={13} className="icon-spin" /> : <ImagePlus size={13} />}
                      <span>Replace</span>
                    </button>
                    <button
                      type="button"
                      className="jod-btn is-danger"
                      onClick={() => set({ posterUrl: undefined })}
                      disabled={uploading}
                      aria-label="Remove poster"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </figure>
              ) : (
                <button
                  type="button"
                  className="jo-web-poster-drop"
                  onClick={() => fileRef.current?.click()}
                  disabled={uploading}
                >
                  {uploading ? <Loader2 size={20} className="icon-spin" /> : <ImagePlus size={20} />}
                  <span className="jo-web-poster-title">{uploading ? "Uploading…" : "Upload poster"}</span>
                  <span className="jo-web-poster-note">JPG, PNG or WebP · up to 5 MB</span>
                </button>
              )}
              <input
                ref={fileRef}
                type="file"
                accept={POSTER_TYPES.join(",")}
                hidden
                onChange={(e) => void handlePoster(e.target.files?.[0])}
              />
              {posterError && (
                <span className="jo-web-error" role="alert">
                  {posterError}
                </span>
              )}
            </div>

            <div className="jo-web-fields">
              <div>
                <label className="modal-label" htmlFor="jo-web-city">
                  City
                </label>
                <input
                  id="jo-web-city"
                  type="text"
                  className="modal-input"
                  placeholder="e.g. Jeddah"
                  value={value.city ?? ""}
                  onChange={(e) => set({ city: e.target.value })}
                  required
                />
              </div>
              <div>
                <span className="modal-label">Job type</span>
                <Select
                  value={value.jobType ?? "Full time"}
                  options={JOB_TYPE_OPTIONS}
                  onChange={(next) => set({ jobType: next as JobOrderWebsiteType })}
                  ariaLabel="Job type"
                />
              </div>
              <div>
                <label className="modal-label" htmlFor="jo-web-category">
                  Category tag
                </label>
                <input
                  id="jo-web-category"
                  type="text"
                  className="modal-input"
                  placeholder={industryTag ? `${industryTag} (from industry)` : "e.g. Piping"}
                  value={value.category ?? ""}
                  onChange={(e) => set({ category: e.target.value })}
                />
              </div>
            </div>
          </div>

          <div>
            <label className="modal-label" htmlFor="jo-web-description">
              Public description
            </label>
            <textarea
              id="jo-web-description"
              className="modal-textarea"
              placeholder="Shown on the job card. Leave blank to list the vacancies, experience and skills."
              value={value.description ?? ""}
              onChange={(e) => set({ description: e.target.value })}
            />
          </div>
        </>
      )}
    </fieldset>
  );
}
