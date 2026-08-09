/**
 * Core type definitions for the Resume Automation & Sourcing Platform frontend.
 */

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
  raw_ocr?: Record<string, any> | null;
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
  raw_ocr?: Record<string, any> | null;
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
  /** Emails belonging to a candidate the user deleted — deliberately not re-ingested. */
  suppressed?: number;
  errors: number;
  ingested_candidates: number;
  results?: PollMessageResult[];
  skipped_reason?: string;
}

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  role: string;
}

export interface SourcingClientRecord {
  id: string;
  name: string;
  client_type: string;
  email?: string | null;
  phone?: string | null;
  location?: string | null;
  status?: string;
  notes?: string | null;
  created_at?: string;
}

export interface JobOrderRecord {
  id: string;
  title: string;
  client_name?: string | null;
  client_id?: string | null;
  skills_required?: string[];
  experience_required_years?: number | null;
  location?: string | null;
  status?: string;
  salary_range?: string | null;
  description?: string | null;
  created_at?: string;
}
