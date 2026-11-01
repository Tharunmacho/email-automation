"use client";

import type { ReactNode } from "react";
import { ClipboardList } from "lucide-react";

import Checkbox from "@/components/ui/Checkbox";
import Select from "@/components/ui/Select";
import type { JobOrderGender, JobOrderTerms } from "@/types";

const GENDER_OPTIONS: { value: JobOrderGender; label: string }[] = [
  { value: "Male & Female", label: "Male & Female" },
  { value: "Male", label: "Male only" },
  { value: "Female", label: "Female only" },
];

/** The documents agents are asked for, in the order the agency wants them. */
export const STANDARD_DOCUMENTS = [
  "CV",
  "Passport copy (all pages including cover pages)",
  "PCC (Police Clearance Certificate)",
  "Experience certificates",
  "Educational documents",
  "Passport size photo",
];

/**
 * The section as the dialog edits it. Lists are held as the text the recruiter
 * typed, so a half-typed line is not tidied away under their cursor; they are
 * split only when the order is saved (`cleanTerms`).
 */
export interface TermsDraft {
  gender: JobOrderGender | "";
  processTime: string;
  b2bRate: string;
  candidatePays: string;
  advancePayment: string;
  balancePayment: string;
  /** One requirement per line (or comma). */
  requirements: string;
  /** Ticked entries of `STANDARD_DOCUMENTS`. */
  standardDocuments: string[];
  /** Anything else to send, after the standard ones. */
  otherDocuments: string;
  singlePdf: boolean;
}

/** "a, b\nc" → ["a", "b", "c"], dropping bullets and blanks. */
function splitList(text: string): string[] {
  return text
    .split(/[\n,;]+/)
    .map((item) => item.replace(/^\s*(?:[-•*·]|\d+[.)])\s*/, "").trim())
    .filter(Boolean);
}

/**
 * The draft for an order's terms. With nothing given — a new order — every
 * standard document is asked for, as one PDF. Editing an order that never had
 * terms passes `{}` and starts empty instead, so saving it adds nothing unasked.
 */
export function termsDraft(existing?: JobOrderTerms): TermsDraft {
  if (!existing) {
    return {
      gender: "",
      processTime: "",
      b2bRate: "",
      candidatePays: "",
      advancePayment: "",
      balancePayment: "",
      requirements: "",
      standardDocuments: [...STANDARD_DOCUMENTS],
      otherDocuments: "",
      singlePdf: true,
    };
  }
  const documents = existing.documents ?? [];
  return {
    gender: existing.gender ?? "",
    processTime: existing.processTime ?? "",
    b2bRate: existing.b2bRate ?? "",
    candidatePays: existing.candidatePays ?? "",
    advancePayment: existing.advancePayment ?? "",
    balancePayment: existing.balancePayment ?? "",
    requirements: (existing.requirements ?? []).join("\n"),
    standardDocuments: STANDARD_DOCUMENTS.filter((doc) => documents.includes(doc)),
    otherDocuments: documents.filter((doc) => !STANDARD_DOCUMENTS.includes(doc)).join(", "),
    singlePdf: existing.singlePdf === true,
  };
}

/** The terms as saved on the order: trimmed, empty fields left out, `undefined` when there are none. */
export function cleanTerms(draft: TermsDraft): JobOrderTerms | undefined {
  const out: JobOrderTerms = {};
  const text = (value: string) => value.trim();
  if (draft.gender) out.gender = draft.gender;
  if (text(draft.processTime)) out.processTime = text(draft.processTime);
  if (text(draft.b2bRate)) out.b2bRate = text(draft.b2bRate);
  if (text(draft.candidatePays)) out.candidatePays = text(draft.candidatePays);
  if (text(draft.advancePayment)) out.advancePayment = text(draft.advancePayment);
  if (text(draft.balancePayment)) out.balancePayment = text(draft.balancePayment);
  const requirements = splitList(draft.requirements);
  if (requirements.length) out.requirements = requirements;
  const documents = [
    ...STANDARD_DOCUMENTS.filter((doc) => draft.standardDocuments.includes(doc)),
    ...splitList(draft.otherDocuments).filter((doc) => !STANDARD_DOCUMENTS.includes(doc)),
  ];
  if (documents.length) {
    out.documents = documents;
    if (draft.singlePdf) out.singlePdf = true;
  }
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
  const payment = [
    terms.advancePayment && `${terms.advancePayment} in advance after offer acceptance`,
    terms.balancePayment && `Balance: ${terms.balancePayment}`,
  ].filter(Boolean);
  const rows: { label: string; value: ReactNode; wide?: boolean }[] = [];
  if (gender) rows.push({ label: "Gender", value: gender });
  if (terms.processTime) rows.push({ label: "Process time", value: terms.processTime });
  if (terms.b2bRate) rows.push({ label: "Ref B2B rate", value: terms.b2bRate });
  if (terms.candidatePays) rows.push({ label: "Borne by candidate", value: terms.candidatePays });
  if (payment.length) rows.push({ label: "Payment", value: payment.join(" · "), wide: true });
  if (terms.requirements?.length) {
    rows.push({
      label: "Requirements",
      wide: true,
      value: (
        <ul className="jod-spec-list">
          {terms.requirements.map((item) => <li key={item}>{item}</li>)}
        </ul>
      ),
    });
  }
  if (terms.documents?.length) {
    rows.push({
      label: terms.singlePdf ? "Documents (single PDF, in this order)" : "Documents (in this order)",
      wide: true,
      value: (
        <ol className="jod-spec-list">
          {terms.documents.map((item) => <li key={item}>{item}</li>)}
        </ol>
      ),
    });
  }
  return (
    <>
      {rows.map((row) => (
        <div key={row.label} className={row.wide ? "jod-spec jod-spec-wide" : "jod-spec"}>
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
 * The "Terms & requirements" part of the job order dialogs: who may apply, the
 * process time, what it costs and when it is paid, what a candidate needs, and
 * the documents to send. It goes to agents with the new-order WhatsApp
 * message; none of it is shown on the website.
 */
export default function JobOrderTermsFields({ value, onChange, idPrefix }: JobOrderTermsFieldsProps) {
  const set = (patch: Partial<TermsDraft>) => onChange({ ...value, ...patch });
  const id = (name: string) => `${idPrefix}-${name}`;

  const toggleDocument = (doc: string, on: boolean) => {
    const ticked = new Set(value.standardDocuments);
    if (on) ticked.add(doc);
    else ticked.delete(doc);
    set({ standardDocuments: STANDARD_DOCUMENTS.filter((item) => ticked.has(item)) });
  };

  const textField = (name: keyof TermsDraft, label: string, placeholder: string) => (
    <div>
      <label className="modal-label" htmlFor={id(name)}>
        {label}
      </label>
      <input
        id={id(name)}
        type="text"
        className="modal-input"
        placeholder={placeholder}
        value={value[name] as string}
        onChange={(e) => set({ [name]: e.target.value } as Partial<TermsDraft>)}
      />
    </div>
  );

  return (
    <fieldset className="jo-web">
      <legend className="jo-web-legend">
        <ClipboardList size={14} aria-hidden="true" />
        Terms &amp; requirements
      </legend>
      <p className="jo-terms-note">Sent to agents with the new job order message. Not shown on the website.</p>

      <div className="modal-row-2">
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
        {textField("processTime", "Process time", "e.g. 6-8 months")}
      </div>

      <div className="modal-row-2">
        {textField("b2bRate", "Ref B2B rate", "e.g. 4.9 with Ticket")}
        {textField("candidatePays", "Borne by candidate", "e.g. VFS, Embassy fee")}
      </div>

      <div className="modal-row-2">
        {textField("advancePayment", "Advance after offer acceptance", "e.g. €946")}
        {textField("balancePayment", "Balance payment", "e.g. After the visa")}
      </div>

      <div>
        <label className="modal-label" htmlFor={id("requirements")}>
          Requirements
        </label>
        <textarea
          id={id("requirements")}
          className="modal-textarea"
          placeholder={"One per line, e.g.\nRelevant work experience\nBasic English\nValid passport & PCC\nUpdated CV"}
          value={value.requirements}
          onChange={(e) => set({ requirements: e.target.value })}
        />
      </div>

      <div className="jo-terms-docs">
        <span className="modal-label">Documents to send, in this order</span>
        <ol className="jo-terms-doc-list">
          {STANDARD_DOCUMENTS.map((doc) => (
            <li key={doc}>
              <Checkbox
                checked={value.standardDocuments.includes(doc)}
                onChange={(on) => toggleDocument(doc, on)}
                label={doc}
              />
            </li>
          ))}
        </ol>
        {textField("otherDocuments", "Other documents (after the ones above)", "e.g. Driving licence, Trade certificate")}
        <Checkbox
          checked={value.singlePdf}
          onChange={(on) => set({ singlePdf: on })}
          label="System-scanned, in a single PDF"
        />
      </div>
    </fieldset>
  );
}
