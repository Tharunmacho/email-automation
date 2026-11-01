"use client";

import { Globe } from "lucide-react";

import Checkbox from "@/components/ui/Checkbox";
import Select from "@/components/ui/Select";
import type { JobOrderWebsite, JobOrderWebsiteType } from "@/types";

const JOB_TYPES: JobOrderWebsiteType[] = ["Full time", "Contract", "Part time", "Remote"];
const JOB_TYPE_OPTIONS = JOB_TYPES.map((value) => ({ value, label: value }));

/** A new order is listed on the website unless someone says otherwise. */
export function websiteDefaults(existing?: JobOrderWebsite): JobOrderWebsite {
  return { show: true, jobType: "Full time", ...(existing ?? {}) };
}

export function websiteIsShown(website?: JobOrderWebsite): boolean {
  return website?.show === true;
}

/**
 * Trimmed, with empty fields left out, as it is saved on the order.
 *
 * The poster is not part of it: it lives on the order (`posterUrl`, see
 * `JobPosterField`) because agents get it on WhatsApp too. An old
 * `website.posterUrl` is dropped here once the order is saved with its poster
 * moved up.
 */
export function cleanWebsite(website: JobOrderWebsite): JobOrderWebsite {
  const out: JobOrderWebsite = { show: websiteIsShown(website) };
  const text = (value?: string) => (value ?? "").trim();
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
}

/**
 * The "Website listing" part of the job order dialogs.
 *
 * Everything here is public: it becomes the card on the website's Current Jobs
 * page while the order is open, with the order's poster as its picture. The
 * order's remarks stay internal.
 */
export default function WebsiteListingFields({ value, onChange, industry }: WebsiteListingFieldsProps) {
  const shown = websiteIsShown(value);
  const set = (patch: Partial<JobOrderWebsite>) => onChange({ ...value, ...patch });
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
        hint="Listed on the Current Jobs page, with the job poster, while this order is open, and taken down when it is closed."
      />

      {shown && (
        <>
          <div className="jo-web-row-3">
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
