/**
 * Typed client for the FastAPI resume-ingestion backend (app/api/routes.py).
 *
 * The app is built with `output: "export"`, so it is served as static files —
 * usually by the same FastAPI process, in which case a relative origin works.
 * During `next dev` the frontend runs on :3000 and the API on :8000, so point
 * NEXT_PUBLIC_API_BASE at the backend (CORS is already wide open server-side).
 */

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

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

export interface PollSummary {
  fetched: number;
  processed: number;
  skipped: number;
  errors: number;
  ingested_candidates: number;
}

/** Candidates below this parser confidence are surfaced for manual review. */
export const REVIEW_CONFIDENCE_THRESHOLD = 0.85;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
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

export function triggerPoll(): Promise<PollSummary> {
  return request<PollSummary>("/ingest/poll", { method: "POST" });
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

export function resumeDownloadUrl(candidateId: string): string {
  return `${API_BASE}/candidates/${candidateId}/resume`;
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
