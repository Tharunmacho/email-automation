/**
 * Typed client for the FastAPI resume-ingestion backend (app/api/routes.py).
 *
 * The app is built with `output: "export"`, so it is served as static files —
 * usually by the same FastAPI process, in which case a relative origin works.
 * During `next dev` the frontend runs on :3000 and the API on :8000, so point
 * NEXT_PUBLIC_API_BASE at the backend (CORS is already wide open server-side).
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ??
  (typeof window !== "undefined" && (window.location.port === "3000" || window.location.hostname === "localhost")
    ? "http://127.0.0.1:8000"
    : "");

export interface WorkExperience {
  company?: string | null;
  designation?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  location?: string | null;
  description?: string | null;
}

export interface Education {
  institution?: string | null;
  degree?: string | null;
  field_of_study?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  grade?: string | null;
}

export interface Project {
  name?: string | null;
  description?: string | null;
  technologies?: string[];
  url?: string | null;
}

export interface CandidateProfile {
  is_resume: boolean;
  confidence: number;

  full_name?: string | null;
  email?: string | null;
  phone?: string | null;
  location?: string | null;

  skills?: string[];
  technical_skills?: string[];
  languages?: string[];

  work_experience?: WorkExperience[];
  education?: Education[];
  certifications?: string[];
  projects?: Project[];
  achievements?: string[];

  linkedin_url?: string | null;
  github_url?: string | null;
  portfolio_url?: string | null;

  current_company?: string | null;
  current_designation?: string | null;
  total_experience_years?: number | null;

  resume_summary?: string | null;
  additional_info?: Record<string, unknown>;
}

export interface StoredResume {
  original_filename: string;
  mime_type: string;
  size: number;
  sha256: string;
  storage_backend: string;
  storage_key: string;
  extraction_method?: string;
  ocr_used?: boolean;
}

export interface SourceEmail {
  message_id: string;
  thread_id: string;
  from_addr: string;
  from_name?: string | null;
  subject?: string;
  received_date?: string | null;
}

export interface CandidateRecord {
  id: string;
  profile: CandidateProfile;
  resume: StoredResume;
  source_email: SourceEmail;
  email_key?: string | null;
  phone_key?: string | null;
  resume_hash?: string;
  status: string;
  duplicate_of?: string | null;
  created_at: string;
  updated_at: string;
}

export interface CandidateListResponse {
  total: number;
  count: number;
  items: CandidateRecord[];
}

export interface PollAttachmentResult {
  filename: string;
  status: string;
  candidate_id?: string | null;
  detail?: string | null;
}

export interface PollMessageResult {
  message_id: string;
  status: string;
  reason?: string | null;
  attachments: PollAttachmentResult[];
}

export interface PollSummary {
  fetched: number;
  processed: number;
  skipped: number;
  errors: number;
  ingested_candidates: number;
  results?: PollMessageResult[];
  /** Set when the cycle declined to run because another one held the lock. */
  skipped_reason?: string;
}

/** Candidates below this parser confidence are surfaced for manual review. */
export const REVIEW_CONFIDENCE_THRESHOLD = 0.85;

// --------------------------------------------------------------------------- //
//  Auth
// --------------------------------------------------------------------------- //
const TOKEN_KEY = "ats_token";

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  role: string;
}

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

export function listCandidates(limit = 200, skip = 0): Promise<CandidateListResponse> {
  return request<CandidateListResponse>(`/candidates?limit=${limit}&skip=${skip}`, {
    cache: "no-store",
  });
}

/** Runs the poll inline; the request is held open for the whole batch. */
export function triggerPoll(): Promise<PollSummary> {
  return request<PollSummary>("/ingest/poll", { method: "POST" });
}

interface QueuedPoll {
  task_id: string;
  state: string;
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
const POLL_INTERVAL_MS = 2000;
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

  const deadline = Date.now() + POLL_TIMEOUT_MS;
  let consecutiveErrors = 0;

  for (;;) {
    await sleep(POLL_INTERVAL_MS);

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
      throw new Error(status.error || "The ingestion worker failed to run the poll.");
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

export function verifyCandidate(candidateId: string): Promise<CandidateRecord> {
  return request<CandidateRecord>(`/candidates/${candidateId}/verify`, { method: "POST" });
}

export function deleteCandidateAPI(candidateId: string): Promise<{ status: string; message: string }> {
  return request<{ status: string; message: string }>(`/api/v1/candidates/${candidateId}`, {
    method: "DELETE",
  });
}

export function resumeDownloadUrl(candidateId: string): string {
  return `${API_BASE}/candidates/${candidateId}/resume`;
}

// ---- Sourcing Clients DB API ----
export function listSourcingClientsAPI(): Promise<{ items: any[] }> {
  return request<{ items: any[] }>("/sourcing-clients");
}

export function createSourcingClientAPI(record: any): Promise<{ status: string; record: any }> {
  return request<{ status: string; record: any }>("/sourcing-clients", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(record),
  });
}

export function deleteSourcingClientAPI(clientId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/sourcing-clients/${clientId}`, { method: "DELETE" });
}

// ---- Job Orders DB API ----
export function listJobOrdersAPI(): Promise<{ items: any[] }> {
  return request<{ items: any[] }>("/job-orders");
}

export function createJobOrderAPI(record: any): Promise<{ status: string; record: any }> {
  return request<{ status: string; record: any }>("/job-orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(record),
  });
}

export function updateJobOrderAPI(orderId: string, record: any): Promise<{ status: string; record: any }> {
  return request<{ status: string; record: any }>(`/job-orders/${orderId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(record),
  });
}

export function deleteJobOrderAPI(orderId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/job-orders/${orderId}`, { method: "DELETE" });
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
