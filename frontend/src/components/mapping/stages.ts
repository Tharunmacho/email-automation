/**
 * The Mapping Candidates steps, as the console names and draws them.
 *
 *   resume -> interview -> selection -> offer -> visa -> travel
 *
 * Which step may be answered is the backend's decision (`app/mapping_pipeline.py`
 * computes it and every row arrives with it). This module only says how each
 * step reads: its title, the question it asks, the choices on offer and what
 * each choice does — so the dropdown can say "closes this mapping" before
 * anybody picks it, rather than a toast saying so after.
 */

import {
  CalendarClock,
  FileCheck2,
  FileSignature,
  PlaneTakeoff,
  Stamp,
  UserCheck,
  type LucideIcon,
} from "lucide-react";

import type { SelectOption } from "@/components/ui/Select";
import type {
  MappedCandidateRow,
  MappingStageEntry,
  MappingStageKey,
  MappingStageView,
} from "@/lib/api";

export const MAPPING_STAGES: MappingStageKey[] = [
  "resume",
  "interview",
  "selection",
  "offer",
  "visa",
  "travel",
];

export interface StageMeta {
  title: string;
  /** The question the step asks, in the words a recruiter would use. */
  question: string;
  /** A row's status while this step has no answer yet. */
  waiting: string;
  icon: LucideIcon;
  /** The choices for single-question steps. The visa step has two lists below. */
  options?: SelectOption[];
}

export const STAGE_META: Record<MappingStageKey, StageMeta> = {
  resume: {
    title: "Resume submitted",
    question: "Has the candidate's resume been submitted to the client?",
    waiting: "Awaiting resume submission",
    icon: FileCheck2,
    options: [
      { value: "submitted", label: "Yes — resume submitted", hint: "Opens the interview step." },
      { value: "not_submitted", label: "Not yet", hint: "Keeps this step open until it is sent." },
    ],
  },
  interview: {
    title: "Interview scheduled",
    question: "Has the client scheduled an interview?",
    waiting: "Awaiting interview",
    icon: CalendarClock,
    options: [
      { value: "scheduled", label: "Yes — interview scheduled", hint: "Record the date and time. Opens selection." },
      { value: "not_scheduled", label: "Not yet", hint: "Keeps this step open." },
    ],
  },
  selection: {
    title: "Selection",
    question: "What did the client decide after the interview?",
    waiting: "Awaiting selection",
    icon: UserCheck,
    options: [
      { value: "selected", label: "Selected", hint: "Opens the offer step." },
      {
        value: "rejected",
        label: "Rejected",
        hint: "Closes this mapping and returns the candidate to the available pool.",
      },
    ],
  },
  offer: {
    title: "Offer",
    question: "Did the candidate accept the offer?",
    waiting: "Awaiting offer decision",
    icon: FileSignature,
    options: [
      {
        value: "accepted",
        label: "Offer accepted",
        hint: "Locks the candidate to this placement and opens the visa step.",
      },
      {
        value: "rejected",
        label: "Offer rejected",
        hint: "Closes this mapping and returns the candidate to the available pool.",
      },
    ],
  },
  visa: {
    title: "Visa process",
    question: "Where are visa stamping and the medical?",
    waiting: "Awaiting visa process",
    icon: Stamp,
  },
  travel: {
    title: "Travel",
    question: "Has the candidate travelled?",
    waiting: "Awaiting travel",
    icon: PlaneTakeoff,
    options: [
      { value: "travelled", label: "Yes — travelled", hint: "Record the date, departure time and flight." },
      { value: "not_travelled", label: "Not yet", hint: "Keeps this step open." },
    ],
  },
};

export const STAMPING_OPTIONS: SelectOption[] = [
  { value: "completed", label: "Stamping completed", hint: "The visa is stamped in the passport." },
  { value: "pending", label: "Stamping in progress", hint: "Submitted, or not yet submitted, for stamping." },
];

export const MEDICAL_OPTIONS: SelectOption[] = [
  { value: "fit", label: "Medically fit" },
  { value: "unfit", label: "Medically unfit", hint: "Holds travel until the result changes or a manager skips." },
  { value: "pending", label: "Medical result awaited" },
];

/** What makes a step complete, so a form can say why "Save" will not open the next. */
export const PASSING_ANSWER: Partial<Record<MappingStageKey, string>> = {
  resume: "submitted",
  interview: "scheduled",
  selection: "selected",
  offer: "accepted",
  travel: "travelled",
};

const ANSWER_LABEL: Record<string, string> = {
  submitted: "Submitted",
  not_submitted: "Not submitted yet",
  scheduled: "Scheduled",
  not_scheduled: "Not scheduled yet",
  selected: "Selected",
  rejected: "Rejected",
  accepted: "Accepted",
  travelled: "Travelled",
  not_travelled: "Not travelled yet",
};

const CLOSED_REASON: Record<string, string> = {
  rejected: "Rejected at selection",
  offer_rejected: "Offer rejected",
  offer_declined: "Offer declined",
};

export type Tone = "ok" | "info" | "warn" | "bad" | "neutral";

/** The answer recorded on a step, in a few words. */
export function answerLabel(stage: MappingStageKey, entry: MappingStageEntry | null | undefined): string {
  if (!entry) return "Not answered";
  if (entry.skipped) return "Skipped";
  if (stage === "visa") {
    const stamping = entry.stamping === "completed" ? "Stamped" : "Stamping pending";
    const medical =
      entry.medical === "fit" ? "medically fit" : entry.medical === "unfit" ? "medically unfit" : "medical awaited";
    return `${stamping} · ${medical}`;
  }
  return ANSWER_LABEL[entry.answer ?? ""] ?? entry.answer ?? "—";
}

/** The tone of a step's answer chip. */
export function answerTone(step: MappingStageView): Tone {
  if (step.state === "passed") return "ok";
  if (step.state === "skipped") return "neutral";
  if (step.state === "failed") return "bad";
  if (step.key === "visa" && step.entry?.medical === "unfit") return "bad";
  return step.entry ? "warn" : "info";
}

export function formatDay(value?: string | null): string {
  if (!value) return "—";
  // A bare yyyy-mm-dd is a calendar day, not an instant: split by hand so it
  // is not read as UTC midnight and shown as the day before.
  const bare = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const date = bare ? new Date(Number(bare[1]), Number(bare[2]) - 1, Number(bare[3])) : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

export function formatMoment(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-IN", {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** The facts a step's answer carries beyond the answer itself. */
export function entryFacts(stage: MappingStageKey, entry: MappingStageEntry | null | undefined): { label: string; value: string }[] {
  if (!entry || entry.skipped) return [];
  if (stage === "interview" && entry.interview_at) {
    return [{ label: "Interview", value: formatMoment(entry.interview_at) }];
  }
  if (stage === "travel" && entry.answer === "travelled") {
    return [
      { label: "Travel date", value: formatDay(entry.travel_date) },
      { label: "Departure", value: entry.travel_time || "—" },
      { label: "Flight", value: [entry.airline, entry.flight_number].filter(Boolean).join(" · ") || "—" },
    ];
  }
  return [];
}

/** How far through the six steps a row is, for sorting: closed last, travelled after every open row. */
export function progressRank(row: MappedCandidateRow): number {
  if (row.mapping.status === "closed") return 100;
  if (row.mapping.status === "completed") return MAPPING_STAGES.length;
  return MAPPING_STAGES.indexOf(row.mapping.current_stage ?? "resume");
}

/** The one line that says where a mapped candidate stands. */
export function rowStatus(row: MappedCandidateRow): { label: string; tone: Tone } {
  const { mapping } = row;
  if (mapping.status === "completed") return { label: "Travelled", tone: "ok" };
  if (mapping.status === "closed") {
    return { label: CLOSED_REASON[mapping.closed?.reason ?? ""] ?? "Closed", tone: "bad" };
  }
  const key = mapping.current_stage ?? "resume";
  const entry = mapping.stages.find((step) => step.key === key)?.entry;
  if (key === "visa" && entry) {
    if (entry.medical === "unfit") return { label: "Medically unfit", tone: "bad" };
    return { label: "Visa in progress", tone: "info" };
  }
  if (entry?.answer?.startsWith("not_")) return { label: answerLabel(key, entry), tone: "warn" };
  return { label: STAGE_META[key].waiting, tone: "info" };
}

/** When the mapping last moved: the latest recorded step, else the submission. */
export function lastActivity(row: MappedCandidateRow): string | null {
  const times = row.mapping.stages
    .map((step) => (step.entry && !step.entry.derived ? step.entry.at : null))
    .filter((value): value is string => Boolean(value));
  if (row.mapping.closed?.at) times.push(row.mapping.closed.at);
  if (row.submission_date) times.push(row.submission_date);
  if (times.length === 0) return row.updated_at ?? null;
  return times.reduce((latest, value) => (new Date(value) > new Date(latest) ? value : latest));
}
