/**
 * Per-candidate activity history.
 *
 * Two logs exist in this app and an entry belongs to exactly one of them.
 * Anything scoped to a person — viewed, edited, verified, deleted — is that
 * person's history and belongs here; the dashboard's trace is reserved for the
 * pipeline. Entries are keyed strictly by candidate id, so one candidate's
 * screen can never show another's events.
 *
 * The history is mirrored into localStorage. Without it every reload wiped the
 * record of who edited what, which makes an activity log worth nothing: the
 * question it answers ("what happened to this profile?") is almost always asked
 * after the session that changed it has ended.
 */

/** Short verb shown in its own column, so the log reads as a table not prose. */
export type CandidateLogAction =
  | "Created"
  | "Viewed"
  | "Opened editor"
  | "Edited"
  | "Renamed"
  | "Verified"
  /** A staff verdict recorded against the profile. */
  | "Evaluated"
  | "Deleted"
  | "Failed";

export interface CandidateLog {
  /** ISO timestamp. The sort key, and what survives a reload intact. */
  at: string;
  /** Clock text as first rendered. Kept so entries stored by older builds still show. */
  time: string;
  type: "info" | "success" | "warn" | "error";
  action: CandidateLogAction;
  message: string;
  candidateId: string;
  /** Who did it. Optional because entries stored before this field existed have none. */
  actor?: string;
}

const STORAGE_KEY = "candidate_activity_logs";

/**
 * Entries kept in the mirror. Generous enough that no real candidate's history
 * is ever truncated, small enough that the store cannot grow without bound —
 * the oldest events across all candidates are the ones dropped.
 */
const MAX_STORED = 1000;

const ACTIONS: CandidateLogAction[] = [
  "Created",
  "Viewed",
  "Opened editor",
  "Edited",
  "Renamed",
  "Verified",
  "Evaluated",
  "Deleted",
  "Failed",
];

const TYPES: CandidateLog["type"][] = ["info", "success", "warn", "error"];

/** One entry, stamped now. */
function candidateLogEntry(
  candidateId: string,
  action: CandidateLogAction,
  message: string,
  type: CandidateLog["type"],
  actor: string | undefined,
  now: Date = new Date(),
): CandidateLog {
  return {
    at: now.toISOString(),
    time: formatClock(now),
    type,
    action,
    message,
    candidateId,
    actor,
  };
}

/** "09:04:31" — hand-formatted, because a locale mismatch breaks hydration. */
function formatClock(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * That candidate's entries and nothing else, oldest first.
 *
 * Matching is on the id alone. An earlier version also matched any log whose
 * *message* happened to contain the id, which let one record's events surface
 * on another's screen — the opposite of what a per-candidate history is for.
 */
export function logsForCandidate(logs: CandidateLog[], candidateId: string): CandidateLog[] {
  return logs
    .filter((entry) => entry.candidateId === candidateId)
    .slice()
    .sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
}

/** Rejects anything that is not a well-formed entry, including older shapes. */
function isCandidateLog(value: unknown): value is CandidateLog {
  if (!value || typeof value !== "object") return false;
  const entry = value as Record<string, unknown>;
  return (
    typeof entry.candidateId === "string" &&
    entry.candidateId.length > 0 &&
    typeof entry.message === "string" &&
    typeof entry.at === "string" &&
    !Number.isNaN(new Date(entry.at).getTime()) &&
    typeof entry.time === "string" &&
    TYPES.includes(entry.type as CandidateLog["type"]) &&
    ACTIONS.includes(entry.action as CandidateLogAction)
  );
}

function readStoredLogs(): CandidateLog[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isCandidateLog);
  } catch {
    // Malformed JSON from an older schema, or storage blocked entirely.
    return [];
  }
}

function writeStoredLogs(logs: CandidateLog[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(logs));
  } catch {
    // Quota exceeded or private browsing — the mirror is optional.
  }
}

// --------------------------------------------------------------------------- //
//  The store
// --------------------------------------------------------------------------- //

/**
 * The history lives outside React and is read through `useSyncExternalStore`.
 *
 * localStorage cannot be touched on the server or during hydration, and loading
 * it into state from an effect means the first paint shows an empty history and
 * the second shows the real one. An external store is the shape React provides
 * for exactly this: the server and the hydration pass both see `EMPTY`, and the
 * stored entries arrive in the same commit that makes the page interactive.
 */
let cache: CandidateLog[] | null = null;
const listeners = new Set<() => void>();

/** Stable identity for the server/hydration snapshot — a new [] each call would loop. */
const EMPTY: CandidateLog[] = [];

function current(): CandidateLog[] {
  if (cache === null) cache = readStoredLogs();
  return cache;
}

function commit(next: CandidateLog[]): void {
  // Trimmed here rather than at write time, so what is held in memory and what
  // is on disk can never disagree about which entries still exist.
  cache = next.length > MAX_STORED ? next.slice(-MAX_STORED) : next;
  writeStoredLogs(cache);
  for (const listener of listeners) listener();
}

export function subscribeLogs(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getLogsSnapshot(): CandidateLog[] {
  return current();
}

export function getLogsServerSnapshot(): CandidateLog[] {
  return EMPTY;
}

/** Records one event against one candidate. */
export function appendCandidateLog(
  candidateId: string,
  action: CandidateLogAction,
  message: string,
  type: CandidateLog["type"] = "info",
  actor?: string,
): void {
  commit([...current(), candidateLogEntry(candidateId, action, message, type, actor)]);
}

/**
 * Forgets a candidate's history. Called when the record itself is deleted:
 * there is no longer a row to open the log from, so keeping the entries would
 * only crowd out the candidates that still exist.
 */
export function dropCandidateLogs(candidateId: string): void {
  const remaining = current().filter((entry) => entry.candidateId !== candidateId);
  if (remaining.length !== current().length) commit(remaining);
}
