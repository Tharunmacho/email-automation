/**
 * Typed client for the FastAPI resume-ingestion backend (app/api/routes.py).
 *
 * The production container uses Next.js standalone output. During local
 * development or Docker usage the frontend runs on :3000 and FastAPI on
 * :8000, so point NEXT_PUBLIC_API_BASE at the backend. A reverse-proxied
 * deployment can compile it as an empty string to keep API calls same-origin.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ??
  (typeof window !== "undefined" && (window.location.port === "3000" || window.location.hostname === "localhost")
    ? "http://127.0.0.1:8000"
    : "");

import type {
  WorkExperience,
  Education,
  Project,
  CandidateProfile,
  JobAnswer,
  StoredResume,
  SourceEmail,
  CandidateRecord,
  CandidateListResponse,
  AadhaarRecord,
  PassportRecord,
  IdentityDocuments,
  IdentityDocument,
  AnsweredQuestion,
  CandidateUploadResponse,
  PollAttachmentResult,
  PollMessageResult,
  PollSummary,
  AuthUser,
  SourcingClientRecord,
  JobOrderRecord,
  B2BEnquiryRecord,
  EvaluationStatus,
  StaffMember,
  StaffWorkloadRow,
  StaffWorkloadResponse,
  SlaAlert,
  RebalanceResult,
  RehomeResult,
  DeleteStaffResult,
  DemoAccount,
  NotificationRecord,
} from "@/types";

export type {
  WorkExperience,
  Education,
  Project,
  CandidateProfile,
  JobAnswer,
  StoredResume,
  SourceEmail,
  CandidateRecord,
  CandidateListResponse,
  AadhaarRecord,
  PassportRecord,
  IdentityDocuments,
  IdentityDocument,
  AnsweredQuestion,
  CandidateUploadResponse,
  PollAttachmentResult,
  PollMessageResult,
  PollSummary,
  AuthUser,
  SourcingClientRecord,
  JobOrderRecord,
  B2BEnquiryRecord,
  EvaluationStatus,
  StaffMember,
  StaffWorkloadRow,
  StaffWorkloadResponse,
  SlaAlert,
  RebalanceResult,
  RehomeResult,
  DeleteStaffResult,
  DemoAccount,
  NotificationRecord,
};

export { EVALUATION_STATUSES } from "@/types";

/**
 * The accounts the login screen offers as quick-fill buttons.
 *
 * Unauthenticated, because it is read before anyone has signed in. Returns an
 * empty list when demo mode is off, which is how the buttons disappear in a
 * real deployment rather than filling credentials that no longer work.
 */
export async function fetchDemoAccounts(): Promise<DemoAccount[]> {
  try {
    const response = await fetch(`${API_BASE}/auth/demo-accounts`, { cache: "no-store" });
    if (!response.ok) return [];
    const body = (await response.json()) as { enabled: boolean; accounts: DemoAccount[] };
    return body.enabled ? body.accounts : [];
  } catch {
    // An unreachable API is reported by the login attempt itself; the buttons
    // simply do not appear.
    return [];
  }
}

export const REVIEW_CONFIDENCE_THRESHOLD = 0.85;

// --------------------------------------------------------------------------- //
//  Auth
// --------------------------------------------------------------------------- //
const TOKEN_KEY = "ats_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    // sessionStorage first: a "don't remember me" login must win over a stale
    // localStorage token left by a previous session.
    return (
      window.sessionStorage.getItem(TOKEN_KEY) ??
      window.localStorage.getItem(TOKEN_KEY)
    );
  } catch {
    return null;
  }
}

/**
 * @param remember true  -> localStorage, survives closing the browser
 *                 false -> sessionStorage, cleared when the tab closes
 */
export function setToken(token: string | null, remember = true): void {
  if (typeof window === "undefined") return;
  try {
    // Always clear both, so a token never lingers in the store we aren't using.
    window.localStorage.removeItem(TOKEN_KEY);
    window.sessionStorage.removeItem(TOKEN_KEY);
    if (!token) return;
    (remember ? window.localStorage : window.sessionStorage).setItem(TOKEN_KEY, token);
  } catch {
    // Private browsing with storage disabled — the session just won't persist.
  }
}

/** Thrown when the API rejects the token, so callers can send the user back to login. */
export class UnauthorizedError extends Error {
  constructor(message = "Session expired") {
    super(message);
    this.name = "UnauthorizedError";
  }
}

export async function login(
  email: string,
  password: string,
  remember = true,
): Promise<{ token: string; user: AuthUser }> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    let detail = "Invalid email or password";
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      // keep the default
    }
    throw new Error(detail);
  }
  const data = await response.json();
  setToken(data.token, remember);
  return { token: data.token, user: data.user };
}

/** Validates a stored token on page load. */
export async function fetchMe(): Promise<AuthUser> {
  return (await request<{ user: AuthUser }>("/auth/me")).user;
}

export function logout(): void {
  setToken(null);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (response.status === 401) {
    // The token is gone or expired — drop it so the app returns to login
    // instead of retrying forever against a 401.
    setToken(null);
    throw new UnauthorizedError();
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      // Non-JSON error body — keep the status line.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

/**
 * A page of candidate list rows.
 *
 * These are projections, not whole records: the API leaves out the OCR payload
 * and anything else only a detail view reads. Use `getCandidate` before showing
 * or editing a full profile — saving a summary back would write away every
 * field it does not carry.
 */
export function listCandidates(limit = 200, skip = 0): Promise<CandidateListResponse> {
  return request<CandidateListResponse>(`/candidates?limit=${limit}&skip=${skip}`, {
    cache: "no-store",
  });
}

/** The complete record for one candidate — every field, OCR payload included. */
export function getCandidate(candidateId: string): Promise<CandidateRecord> {
  return request<CandidateRecord>(`/candidates/${candidateId}`, { cache: "no-store" });
}

/** Fetch the separately stored Aadhaar and passport records for one candidate. */
export function getCandidateIdentity(candidateId: string): Promise<IdentityDocuments> {
  return request<IdentityDocuments>(`/candidates/${candidateId}/identity`, {
    cache: "no-store",
  });
}

/** Backward-compatible identity lookup used by older profile views. */
export async function fetchIdentityDocuments(candidateId: string): Promise<IdentityDocuments> {
  try {
    return await getCandidateIdentity(candidateId);
  } catch {
    return { candidate_id: candidateId, aadhaar: [], passport: [] };
  }
}

/** Runs the poll inline; the request is held open for the whole batch. */
export function triggerPoll(): Promise<PollSummary> {
  return request<PollSummary>("/ingest/poll", { method: "POST" });
}

interface QueuedPoll {
  task_id: string;
  state: string;
  /**
   * Present only when there was no worker to queue on, in which case the API
   * ran the cycle inside the request and this is its summary — there is no
   * task to wait for. Without reading it, a client sees a 200 with no task id
   * and asks after `/ingest/tasks/undefined` until it times out.
   */
  result?: PollSummary;
}

interface PollTaskStatus {
  task_id: string;
  state: string;
  ready: boolean;
  result?: PollSummary;
  error?: string;
}

/** How long to wait for a queued cycle before giving up on it. */
const POLL_TIMEOUT_MS = 10 * 60 * 1000;
/**
 * Status checks back off as the run goes on. A cycle with two emails in it
 * finishes in seconds, so the first checks are quick; one grinding through a
 * mailbox of scans takes minutes, and asking after it every two seconds for
 * that whole time was hundreds of pointless requests against Uvicorn — each one
 * a Redis round trip to the result backend — while the worker was busy.
 */
const POLL_FIRST_INTERVAL_MS = 1000;
const POLL_MAX_INTERVAL_MS = 15000;
const POLL_BACKOFF_FACTOR = 1.5;
/** Consecutive status-check failures tolerated before calling the run lost. */
const POLL_MAX_CONSECUTIVE_ERRORS = 5;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Run a Gmail poll, off the request path when that is possible.
 *
 * With a Celery worker up, the cycle is queued and this polls for its result,
 * so no single HTTP request is held open through OCR and the LLM. With no
 * worker, it falls back to the inline endpoint — the app works either way, and
 * running a worker is an optimisation rather than a deployment requirement.
 */
export async function runPollCycle(): Promise<PollSummary> {
  let queued: QueuedPoll;
  try {
    queued = await request<QueuedPoll>("/ingest/poll/async", { method: "POST" });
  } catch (err) {
    // A dead session must still send the user to login, not silently retry
    // against an endpoint that will reject them the same way.
    if (err instanceof UnauthorizedError) throw err;
    return triggerPoll();
  }

  // No worker was running, so the API polled inline and this is the finished
  // batch. Nothing to wait for — and nothing to re-run: asking for another
  // cycle here would put every message through OCR a second time.
  if (queued.result) return queued.result;
  if (!queued.task_id) return triggerPoll();

  const deadline = Date.now() + POLL_TIMEOUT_MS;
  let consecutiveErrors = 0;
  let interval = POLL_FIRST_INTERVAL_MS;

  for (;;) {
    await sleep(interval);
    interval = Math.min(Math.round(interval * POLL_BACKOFF_FACTOR), POLL_MAX_INTERVAL_MS);

    let status: PollTaskStatus;
    try {
      status = await request<PollTaskStatus>(`/ingest/tasks/${queued.task_id}`, {
        cache: "no-store",
      });
      consecutiveErrors = 0;
    } catch (err) {
      if (err instanceof UnauthorizedError) throw err;
      // The batch is running on a worker, not in this request. A blip while
      // asking after it says nothing about whether it is still going, so keep
      // asking rather than reporting a failure that did not happen.
      consecutiveErrors += 1;
      if (consecutiveErrors >= POLL_MAX_CONSECUTIVE_ERRORS) {
        throw new Error(
          "Lost contact with the API while the poll was running. It may still " +
            "finish in the background — refresh to see what was ingested.",
        );
      }
      continue;
    }

    if (status.state === "FAILURE") {
      console.warn("Async worker task failed, falling back to inline poll:", status.error);
      return triggerPoll();
    }
    if (status.state === "SUCCESS" && status.result) {
      return status.result;
    }
    if (Date.now() > deadline) {
      throw new Error(
        "The poll is still running after 10 minutes. It will finish in the background — " +
          "refresh to see the candidates it ingested.",
      );
    }
  }
}

export function updateCandidateProfile(
  candidateId: string,
  profile: CandidateProfile,
): Promise<CandidateRecord> {
  return request<CandidateRecord>(`/candidates/${candidateId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
}

/** Upload documents and let VeriIS create the structured candidate profile. */
export function uploadCandidateDocuments(files: {
  resume: File;
  aadhaar?: File | null;
  passport?: File | null;
}): Promise<CandidateUploadResponse> {
  const body = new FormData();
  body.append("resume", files.resume);
  if (files.aadhaar) body.append("aadhaar", files.aadhaar);
  if (files.passport) body.append("passport", files.passport);
  return request<CandidateUploadResponse>("/candidates/upload", {
    method: "POST",
    body,
  });
}

export function verifyCandidate(candidateId: string): Promise<CandidateRecord> {
  return request<CandidateRecord>(`/candidates/${candidateId}/verify`, { method: "POST" });
}

export function deleteCandidateAPI(candidateId: string): Promise<{ status: string; message: string }> {
  return request<{ status: string; message: string }>(`/api/v1/candidates/${candidateId}`, {
    method: "DELETE",
  });
}

export function resumeDownloadUrl(candidateId: string): string {
  const token = getToken();
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${API_BASE}/candidates/${candidateId}/resume${query}`;
}

/**
 * The Aadhaar or passport scan behind one identity row.
 *
 * The record id is scoped to the candidate id server-side, so this is not a
 * second way to reach a document — holding a record id is not authorisation.
 * An Aadhaar is refused with a 403 to anyone who is not an administrator, for
 * the same reason its number is masked: the card is the number. Ask
 * `file_available` on the record before offering the link.
 */
export function identityFileUrl(
  candidateId: string,
  documentType: "aadhaar" | "passport",
  recordId: string,
): string {
  const token = getToken();
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return (
    `${API_BASE}/candidates/${candidateId}/identity/` +
    `${documentType}/${encodeURIComponent(recordId)}/file${query}`
  );
}

// ---- System / ingestion configuration ----
export interface IngestRules {
  provider: string;
  mailbox: {
    account: string;
    configured: boolean;
    inbox_folder: string;
    processed_folder: string;
    deleted_folder: string;
    gmail_query: string;
  };
  gates: {
    detector_min_score: number;
    inspect_all_documents: boolean;
    min_image_attachment_bytes: number;
    min_ingest_confidence: number;
  };
  attachments: { accepted_extensions: string[] };
  ignored_senders: string[];
  ocr: {
    provider: string;
    min_text_chars: number;
    dpi: number;
    chunk_pages: number;
    max_pages: number;
    give_up_pages: number;
    languages: string;
    provider_configured: boolean;
  };
  extraction: { model: string; configured: boolean };
  auto_reply: { enabled: boolean };
}

/** The rules the pipeline actually applies — read-only, no credentials in it. */
export function fetchIngestRules(): Promise<IngestRules> {
  return request<IngestRules>("/ingest/rules", { cache: "no-store" });
}

export function fetchWorkerStatus(): Promise<{ available: boolean }> {
  return request<{ available: boolean }>("/ingest/workers", { cache: "no-store" });
}

/** Unauthenticated liveness probe — used by Settings to report API reachability. */
export async function fetchHealth(): Promise<{ status: string; candidates: number }> {
  const response = await fetch(`${API_BASE}/health`, { cache: "no-store" });
  if (!response.ok) throw new Error(`API replied ${response.status}`);
  return (await response.json()) as { status: string; candidates: number };
}

// --------------------------------------------------------------------------- //
//  Staff administration (requires the Staff page permission)
// --------------------------------------------------------------------------- //
export function listStaff(includeInactive = true): Promise<{ count: number; items: StaffMember[] }> {
  return request<{ count: number; items: StaffMember[] }>(
    `/staff?include_inactive=${includeInactive}`,
    { cache: "no-store" },
  );
}

/** The workload matrix: one row per staff member, with evaluated/assigned progress. */
export function fetchStaffWorkload(): Promise<StaffWorkloadResponse> {
  return request<StaffWorkloadResponse>("/staff/workload", { cache: "no-store" });
}

/**
 * Add a staff account to the roster.
 *
 * Creates and nothing else — existing allocations are left exactly where they
 * are. The new account fills up through the normal least-loaded rule as
 * résumés arrive; moving the pile that is already there is `rebalanceCandidates`,
 * which the admin triggers deliberately.
 */
export function createStaff(payload: {
  email: string;
  password: string;
  name?: string;
  keywords?: string[];
  phone?: string;
}): Promise<{ staff: StaffMember }> {
  return request<{ staff: StaffMember }>("/staff", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateStaff(
  staffId: string,
  payload: {
    name?: string;
    keywords?: string[];
    active?: boolean;
    password?: string;
    phone?: string;
  },
): Promise<{ staff: StaffMember }> {
  return request<{ staff: StaffMember }>(`/staff/${staffId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/**
 * Delete a staff account and deal with the queue it was holding.
 *
 * Redistribution defaults on for a reason: the deleted account's candidates
 * otherwise point at a user that no longer exists, which makes them invisible
 * to every staff dashboard and accountable to nobody.
 *
 * The reply says what happened to each half — `reallocated` unviewed profiles
 * went straight to the least-loaded staff, `orphaned` reviewed ones kept their
 * verdict and are waiting on `rehomeOrphans`.
 */
export function deleteStaff(staffId: string, rebalance = true): Promise<DeleteStaffResult> {
  return request<DeleteStaffResult>(`/staff/${staffId}?rebalance=${rebalance}`, {
    method: "DELETE",
  });
}

// --------------------------------------------------------------------------- //
//  Allocation
// --------------------------------------------------------------------------- //
/** Move one profile to a named staff member. */
export function assignCandidate(candidateId: string, staffId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/candidates/${candidateId}/assign`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ staff_id: staffId }),
  });
}

/**
 * Let the balancer place one profile: it goes to whoever is holding the fewest.
 *
 * The same call ingestion makes, so a profile placed from the console lands
 * exactly where an automatically-allocated one would have. `reason` is
 * "workload", or "no_staff" when there is no active account to place it with.
 */
export function autoAssignCandidate(candidateId: string): Promise<{
  candidate_id: string;
  assigned_staff_id: string | null;
  assigned_staff_name: string | null;
  reason: string;
}> {
  return request(`/candidates/${candidateId}/auto-assign`, { method: "POST" });
}

/**
 * Level the whole collection across the active roster.
 *
 * Profiles that have already been viewed or evaluated stay where they are, so
 * this is safe to run on a working system — it moves untouched work only.
 */
export function rebalanceCandidates(): Promise<RebalanceResult> {
  return request<RebalanceResult>("/candidates/rebalance", { method: "POST" });
}

/**
 * Hand every profile stranded on a deleted account to somebody who exists.
 *
 * The one thing a rebalance cannot do. An orphan is orphaned precisely because
 * it had already been reviewed, and a rebalance is defined by leaving reviewed
 * profiles alone — so calling `rebalanceCandidates` on a pile of orphans moves
 * none of them and the console's warning never clears. This re-homes them with
 * their verdict, score, notes and first-open timestamp intact.
 */
export function rehomeOrphans(): Promise<RehomeResult> {
  return request<RehomeResult>("/candidates/rehome-orphans", { method: "POST" });
}

// --------------------------------------------------------------------------- //
//  Evaluation (staff workspace)
// --------------------------------------------------------------------------- //
/** Stamp first view. Idempotent — re-opening a profile does not move the clock. */
export function markCandidateViewed(
  candidateId: string,
): Promise<{ status: string; first_view: boolean }> {
  return request<{ status: string; first_view: boolean }>(`/candidates/${candidateId}/view`, {
    method: "POST",
  });
}

export function evaluateCandidate(
  candidateId: string,
  payload: { status: EvaluationStatus; score?: number | null; notes?: string | null },
): Promise<CandidateRecord> {
  return request<CandidateRecord>(`/candidates/${candidateId}/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/**
 * Server settings the interface has to agree with.
 *
 * Chiefly the SLA threshold, which drives every countdown in the staff queue —
 * assuming 10 hours in the browser would start lying the moment it is changed.
 */
export function fetchUiConfig(): Promise<{
  sla_threshold_hours: number;
  auto_assign_enabled: boolean;
}> {
  return request("/config", { cache: "no-store" });
}

// --------------------------------------------------------------------------- //
//  Notifications
// --------------------------------------------------------------------------- //
/**
 * The signed-in user's own feed.
 *
 * Scoped server-side by the token, so there is no user id to pass and no way to
 * ask for someone else's. The socket delivers the same events live; this is
 * what makes them survive being offline when one fired.
 */
export function fetchNotifications(
  limit = 30,
): Promise<{ items: NotificationRecord[]; unread: number }> {
  return request(`/notifications?limit=${limit}`, { cache: "no-store" });
}

/** Mark specific rows read, or the whole feed with `{ all: true }`. */
export function markNotificationsRead(
  payload: { ids?: string[]; all?: boolean },
): Promise<{ updated: number; unread: number }> {
  return request("/notifications/read", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: payload.ids ?? [], all: payload.all ?? false }),
  });
}

// --------------------------------------------------------------------------- //
//  SLA
// --------------------------------------------------------------------------- //
export function fetchSlaAlerts(
  status: "active" | "resolved" | "all" = "active",
): Promise<{ count: number; items: SlaAlert[]; threshold_hours: number }> {
  return request(`/sla/alerts?status=${status}`, { cache: "no-store" });
}

/** Live breach list, computed now rather than read from the audit log. */
export function fetchSlaBreaches(): Promise<{
  count: number;
  items: SlaAlert[];
  threshold_hours: number;
}> {
  return request("/sla/breaches", { cache: "no-store" });
}

/** Run the sweep immediately instead of waiting for the beat timer. */
export function runSlaScan(): Promise<{ in_breach: number; new_alerts: number; resolved: number }> {
  return request("/sla/scan", { method: "POST" });
}

// ---- Sourcing Clients DB API ----
export function listSourcingClientsAPI(): Promise<{ items: SourcingClientRecord[] }> {
  return request<{ items: SourcingClientRecord[] }>("/sourcing-clients");
}

export function createSourcingClientAPI(
  record: SourcingClientRecord,
): Promise<{ status: string; record: SourcingClientRecord }> {
  return request<{ status: string; record: SourcingClientRecord }>("/sourcing-clients", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(record),
  });
}

export function deleteSourcingClientAPI(clientId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/sourcing-clients/${clientId}`, { method: "DELETE" });
}

// ---- Job Orders DB API ----
export function listJobOrdersAPI(): Promise<{ items: JobOrderRecord[] }> {
  return request<{ items: JobOrderRecord[] }>("/job-orders");
}

export function createJobOrderAPI(
  record: JobOrderRecord,
): Promise<{ status: string; record: JobOrderRecord }> {
  return request<{ status: string; record: JobOrderRecord }>("/job-orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(record),
  });
}

export function updateJobOrderAPI(
  orderId: string,
  record: JobOrderRecord,
): Promise<{ status: string; record: JobOrderRecord }> {
  return request<{ status: string; record: JobOrderRecord }>(`/job-orders/${orderId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(record),
  });
}

export function deleteJobOrderAPI(orderId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/job-orders/${orderId}`, { method: "DELETE" });
}

// ---- B2B Enquiries -------------------------------------------------------- //
//
// Manpower requirements raised by agents. The WhatsApp bot writes them through
// its own service-key endpoint (`POST /b2b-enquiries`), which is not reachable
// from here and is not meant to be. Everything below uses the signed-in
// recruiter's session and requires the B2B Enquiries page grant.

export interface EnquiryListResponse {
  items: B2BEnquiryRecord[];
  /** Per-state totals over the whole collection, not over `items`. A filtered
   *  list must not make the other tabs read zero. */
  counts: Record<string, number>;
  statuses: string[];
}

export function listB2BEnquiriesAPI(status?: string): Promise<EnquiryListResponse> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<EnquiryListResponse>(`/b2b-enquiries${query}`, { cache: "no-store" });
}

/** Log an enquiry that came in by phone or email rather than through the bot. */
export function createB2BEnquiryAPI(
  record: Partial<B2BEnquiryRecord>,
): Promise<{ status: string; enquiry: B2BEnquiryRecord }> {
  return request<{ status: string; enquiry: B2BEnquiryRecord }>("/b2b-enquiries/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(record),
  });
}

/**
 * Partial update. Only the fields sent are touched, so moving an enquiry to
 * `reviewing` cannot blank the requirement text by omitting it.
 *
 * `converted` is refused by the API: it means a job order exists, and the only
 * way to make that true is `convertB2BEnquiryAPI`, which writes both at once.
 */
export function updateB2BEnquiryAPI(
  enquiryId: string,
  changes: Partial<B2BEnquiryRecord>,
): Promise<{ status: string; enquiry: B2BEnquiryRecord }> {
  return request<{ status: string; enquiry: B2BEnquiryRecord }>(`/b2b-enquiries/${enquiryId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

export interface ConvertEnquiryPayload {
  title: string;
  client: string;
  headcount: number;
  salary?: string;
  skills?: string[];
  description?: string;
  due_date?: string;
  industry?: string;
  designation?: string;
}

/** Raise the job order this enquiry asked for, and stamp the enquiry with it. */
export function convertB2BEnquiryAPI(
  enquiryId: string,
  payload: ConvertEnquiryPayload,
): Promise<{ status: string; job_order: JobOrderRecord; enquiry: B2BEnquiryRecord }> {
  return request(`/b2b-enquiries/${enquiryId}/convert`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteB2BEnquiryAPI(enquiryId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/b2b-enquiries/${enquiryId}`, { method: "DELETE" });
}



// ---- Data Management: job designations, countries, job questions ---------- //
//
// The jobs the agency recruits for and the countries it sends people to, as
// rows an admin edits. Two consumers read them: the CV policy, which resolves
// `destination_country + job_id` against the rules stored on a job, and the
// WhatsApp bot, which draws its questions from the same table. Adding a job
// here is what puts it in front of candidates.

export interface JobDesignation {
  /** Stable, derived from the title once, and never changed afterwards — the
   *  CV rules and every candidate on file point at it. */
  id: string;
  title: string;
  active: boolean;
  /** Whether the bot offers it. WhatsApp shows nine plus "Other". */
  bot_visible: boolean;
  bot_order: number;
  /** The rule when no country says otherwise. */
  cv_required_default: boolean;
  /** `{ malaysia: false }` — the exceptions, keyed on the lowercased country. */
  cv_overrides: Record<string, boolean>;
  created_at?: string;
  updated_at?: string;
  created_by?: string | null;
}

export interface CountryRow {
  id: string;
  name: string;
  active: boolean;
  bot_visible: boolean;
  bot_order: number;
}

export interface JobQuestion {
  id: string;
  job_id: string;
  text: string;
  kind: "text" | "choice";
  choices: string[];
  required: boolean;
  order: number;
  active: boolean;
}

/** One row of "what does this job actually resolve to, per destination". */
export interface CvMatrixRow {
  country: string;
  cv_required: boolean;
  /** Which rule answered — the override, or the job's default. */
  reason: string;
  is_override: boolean;
}

export function listJobDesignationsAPI(): Promise<{ items: JobDesignation[] }> {
  return request<{ items: JobDesignation[] }>("/job-designations");
}

export function saveJobDesignationAPI(
  job: Partial<JobDesignation> & { title: string },
): Promise<{ status: string; item: JobDesignation }> {
  return request<{ status: string; item: JobDesignation }>("/job-designations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(job),
  });
}

export function retireJobDesignationAPI(jobId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/job-designations/${jobId}`, { method: "DELETE" });
}

export function jobCvMatrixAPI(
  jobId: string,
): Promise<{ job: JobDesignation; matrix: CvMatrixRow[] }> {
  return request<{ job: JobDesignation; matrix: CvMatrixRow[] }>(
    `/job-designations/${jobId}/cv-matrix`,
  );
}

export function listCountriesAPI(): Promise<{ items: CountryRow[] }> {
  return request<{ items: CountryRow[] }>("/countries");
}

export function saveCountryAPI(
  country: Partial<CountryRow> & { name: string },
): Promise<{ status: string; item: CountryRow }> {
  return request<{ status: string; item: CountryRow }>("/countries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(country),
  });
}

export function retireCountryAPI(countryId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/countries/${countryId}`, { method: "DELETE" });
}

export function listJobQuestionsAPI(jobId?: string): Promise<{ items: JobQuestion[] }> {
  const query = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
  return request<{ items: JobQuestion[] }>(`/job-questions${query}`);
}

export function saveJobQuestionAPI(
  question: Partial<JobQuestion> & { job_id: string; text: string },
): Promise<{ status: string; item: JobQuestion }> {
  return request<{ status: string; item: JobQuestion }>("/job-questions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(question),
  });
}

export function deleteJobQuestionAPI(questionId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/job-questions/${questionId}`, { method: "DELETE" });
}

// ---- User management ------------------------------------------------------ //
//
// Accounts, and which pages each one reaches. A checked grant exposes the page
// and its API; an unchecked page is absent. Candidate record isolation remains
// separate, so staff still see only profiles allocated to them.

export interface ManagedUser {
  id: string;
  /** Present for staff accounts; admins do not belong to the staff roster. */
  staff_code?: string | null;
  email: string;
  name: string;
  role: string;
  active: boolean;
  keywords: string[];
  /** Mobile number, free text. Empty when nobody has recorded one. */
  phone?: string;
  created_at: string | null;
  /** The extra pages an admin ticked. */
  page_grants: string[];
  /** What the rail actually shows: the role's floor plus the grants. */
  pages: string[];
}

export function listUsersAPI(): Promise<{ items: ManagedUser[]; pages: string[] }> {
  return request<{ items: ManagedUser[]; pages: string[] }>("/users");
}

export function createUserAPI(payload: {
  email: string;
  password: string;
  name?: string;
  role?: string;
  page_grants?: string[];
  keywords?: string[];
  phone?: string;
}): Promise<{ status: string; user: ManagedUser }> {
  return request<{ status: string; user: ManagedUser }>("/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateUserAPI(
  userId: string,
  patch: {
    name?: string;
    role?: string;
    active?: boolean;
    password?: string;
    page_grants?: string[];
    keywords?: string[];
    phone?: string;
  },
): Promise<{ status: string; user: ManagedUser }> {
  return request<{ status: string; user: ManagedUser }>(`/users/${userId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}


/** Card/badge theme derived from status + parser confidence. */
export function candidateTheme(candidate: CandidateRecord): {
  cardClass: string;
  badgeClass: string;
} {
  if (candidate.status === "verified") {
    return { cardClass: "verified-theme", badgeClass: "badge-verified" };
  }
  if ((candidate.profile?.confidence ?? 1) < REVIEW_CONFIDENCE_THRESHOLD) {
    return { cardClass: "pending-theme", badgeClass: "badge-review" };
  }
  return { cardClass: "active-theme", badgeClass: "badge-active" };
}
