"use client";

import { ClipboardList } from "lucide-react";

import Select from "@/components/ui/Select";
import type { JobOrderGender, JobOrderTerms } from "@/types";

const GENDER_OPTIONS: { value: JobOrderGender; label: string }[] = [
  { value: "Male & Female", label: "Male & Female" },
  { value: "Male", label: "Male only" },
  { value: "Female", label: "Female only" },
];

/** The section as the dialog edits it. */
export interface TermsDraft {
  gender: JobOrderGender | "";
  processTime: string;
  b2bRate: string;
}

/**
 * The draft for an order's terms. Only the fields this section still asks for
 * are read, so saving an order drops anything older (payment terms,
 * requirements, documents) that the form no longer collects.
 */
export function termsDraft(existing?: JobOrderTerms): TermsDraft {
  return {
    gender: existing?.gender ?? "",
    processTime: existing?.processTime ?? "",
    b2bRate: existing?.b2bRate ?? "",
  };
}

/** The terms as saved on the order: trimmed, empty fields left out, `undefined` when there are none. */
export function cleanTerms(draft: TermsDraft): JobOrderTerms | undefined {
  const out: JobOrderTerms = {};
  if (draft.gender) out.gender = draft.gender;
  if (draft.processTime.trim()) out.processTime = draft.processTime.trim();
  if (draft.b2bRate.trim()) out.b2bRate = draft.b2bRate.trim();
  return Object.keys(out).length ? out : undefined;
}

/**
 * The terms in the job order's detail panel, as `<dl>` rows beside the other
 * specs. Only what the order has is shown, so an order without terms looks as
 * it always did.
 */
export function JobOrderTermsSpecs({ terms }: { terms?: JobOrderTerms }) {
  if (!terms) return null;
  const gender = GENDER_OPTIONS.find((option) => option.value === terms.gender)?.label;
  const rows: { label: string; value: string }[] = [];
  if (gender) rows.push({ label: "Gender", value: gender });
  if (terms.processTime) rows.push({ label: "Process time", value: terms.processTime });
  if (terms.b2bRate) rows.push({ label: "Ref B2B rate", value: terms.b2bRate });
  return (
    <>
      {rows.map((row) => (
        <div key={row.label} className="jod-spec">
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </>
  );
}

interface JobOrderTermsFieldsProps {
  value: TermsDraft;
  onChange: (next: TermsDraft) => void;
  /** Distinguishes the create and edit dialogs' field ids. */
  idPrefix: string;
}

/**
 * The "Terms" part of the job order dialogs: who may apply, the process time
 * and the B2B rate. It goes to agents with the new-order WhatsApp message;
 * none of it is shown on the website.
 */
export default function JobOrderTermsFields({ value, onChange, idPrefix }: JobOrderTermsFieldsProps) {
  const set = (patch: Partial<TermsDraft>) => onChange({ ...value, ...patch });
  const id = (name: string) => `${idPrefix}-${name}`;

  return (
    <fieldset className="jo-web">
      <legend className="jo-web-legend">
        <ClipboardList size={14} aria-hidden="true" />
        Terms
      </legend>
      <p className="jo-terms-note">Sent to agents with the new job order message. Not shown on the website.</p>

      <div className="jo-web-row-3">
        <div>
          <span className="modal-label">Gender</span>
          <Select
            value={value.gender}
            options={GENDER_OPTIONS}
            onChange={(next) => set({ gender: next as JobOrderGender })}
            placeholder="Select gender"
            ariaLabel="Gender"
          />
        </div>
        <div>
          <label className="modal-label" htmlFor={id("process-time")}>
            Process time
          </label>
          <input
            id={id("process-time")}
            type="text"
            className="modal-input"
            placeholder="e.g. 6-8 months"
            value={value.processTime}
            onChange={(e) => set({ processTime: e.target.value })}
          />
        </div>
        <div>
          <label className="modal-label" htmlFor={id("b2b-rate")}>
            Ref B2B rate
          </label>
          <input
            id={id("b2b-rate")}
            type="text"
            className="modal-input"
            placeholder="e.g. 4.9 with Ticket"
            value={value.b2bRate}
            onChange={(e) => set({ b2bRate: e.target.value })}
          />
        </div>
      </div>
    </fieldset>
  );
}
