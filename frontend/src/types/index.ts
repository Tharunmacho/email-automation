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

/**
 * One screening question, as it was asked and as it was answered.
 *
 * The wording travels with the answer rather than being resolved from the
 * job's current question list: an admin rewording a question must not rewrite
 * what a candidate was asked six weeks ago.
 */
export interface JobAnswer {
  question_id?: string | null;
  question?: string | null;
  answer?: string | null;
  /** "text" or "choice" — what they were offered, not what they said. */
  kind?: string | null;
  asked_at?: string | null;
}

export interface CandidateProfile {
  is_resume?: boolean;
  confidence: number;

  full_name?: string | null;
  email?: string | null;
  phone?: string | null;
  /** The full international number, where one is known. */
  phone_e164?: string | null;
  location?: string | null;
  city?: string | null;
  /** Where the candidate lives. Never where they want to work. */
  country?: string | null;

  /**
   * Where the candidate wants to work — one actual country, never a region.
   *
   * Set for WhatsApp candidates; absent for email ones, whose résumés say where
   * they have been rather than where they are headed. Kept strictly apart from
   * `country` above, so a recruiter filtering on residence does not get
   * Malaysia back for someone living in Tamil Nadu.
   */
  destination_country?: string | null;
  /** What they want to do, in their own words. */
  job_preference?: string | null;
  /** The same, as a controlled value. This is what the CV policy reads. */
  job_category?: string | null;

  /** The `job_designations` row they picked — the join key. */
  job_id?: string | null;
  /**
   * That job's title, stored beside the id rather than looked up.
   *
   * A job retired or reworded months later still has to read as the job this
   * person applied for.
   */
  job_title?: string | null;
  /** The trade qualification behind the application — "ITI Electrician". */
  course_or_trade?: string | null;
  /**
   * A state, emirate or city inside the destination country.
   *
   * Sits below `destination_country` and never replaces it — the CV policy
   * reads the country and would not know what to do with "Kerala".
   */
  state_preference?: string | null;
  /**
   * When they can start, in their own words: "Immediately", "after 2 months".
   *
   * Free text on purpose. A date field would force a made-up date onto every
   * candidate who answered with a duration.
   */
  available_from?: string | null;
  /** What they said to the screening questions attached to that job. */
  job_answers?: JobAnswer[];

  skills?: string[];
  technical_skills?: string[];
  trade_skills?: string[];
  languages?: string[];

  /** Passport details, for overseas placement. Aadhaar and PAN are not stored. */
  passport_number?: string | null;
  passport_expiry?: string | null;

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
  /**
   * Experience as a band ("1_3", "5_10") when that is how it was given.
   *
   * The WhatsApp bot offers ranges rather than a number, and turning "3_5" into
   * 4.0 would put a figure on the record the candidate never said. So the band
   * is kept as a band and `total_experience_years` stays null.
   */
  total_experience_band?: string | null;

  summary?: string | null;
  resume_summary?: string | null;
  additional_info?: Record<string, unknown>;
  raw_ocr?: Record<string, any> | null;
}

/**
 * Storage internals are marked optional because a list row does not carry them:
 * `GET /candidates` projects the document down to what a row displays, and only
 * `GET /candidates/{id}` returns the whole thing.
 */
export interface StoredResume {
  original_filename?: string;
  mime_type?: string;
  size?: number;
  sha256?: string;
  storage_backend?: string;
  storage_key?: string;
  extraction_method?: string;
  ocr_used?: boolean;
}

export interface SourceEmail {
  message_id?: string;
  thread_id?: string;
  from_addr?: string;
  from_name?: string | null;
  subject?: string;
  received_date?: string | null;
}

/**
 * One candidate, as either a list row or a full record.
 *
 * A row from `GET /candidates` is a projection: identity, status, and the
 * fields the directory shows. Everything else — education, projects, the OCR
 * payload — is absent until the record is read with `getCandidate`, so treat
 * anything outside the list projection as possibly missing, and never write a
 * list row back through `updateCandidateProfile`.
 */
export interface CandidateRecord {
  id: string;
  /** Human-facing CRM identifier; the internal database key remains `id`. */
  candidate_code?: string;
  /**
   * Where this candidate came from. Absent on records written before the
   * WhatsApp integration, which are all email — read it as `"email"` when
   * missing rather than treating it as unknown.
   */
  source?: "email" | "whatsapp";
  profile: CandidateProfile;
  /**
   * Optional, because a candidate may genuinely have no CV.
   *
   * The WhatsApp bot only asks for one where the CV policy requires it, so a
   * general worker bound for Malaysia arrives with none — and that is a
   * complete record, not a broken one. Every reader must handle its absence:
   * no download button, and no invented filename standing in for a file that
   * does not exist.
   */
  resume?: StoredResume | null;
  /** Absent for WhatsApp candidates — there is no email, and none is faked. */
  source_email?: SourceEmail | null;
  /** What the CV policy decided when this candidate registered. */
  cv_required?: boolean;
  cv_policy_version?: string | null;
  email_key?: string | null;
  phone_key?: string | null;
  resume_hash?: string | null;
  status: string;
  duplicate_of?: string | null;
  raw_ocr?: Record<string, any> | null;
  created_at: string;
  updated_at: string;

  /**
   * When the résumé arrived and when the parser finished with it.
   *
   * Absent on records written before these were stored, which is why every
   * reader falls back to `created_at` rather than treating a missing value as
   * the epoch — an old profile would otherwise read as infinitely overdue.
   */
  ingested_at?: string | null;
  processed_at?: string | null;

  /** Which staff member owns this profile, and since when. */
  assigned_staff_id?: string | null;
  assigned_staff_name?: string | null;
  assigned_at?: string | null;

  /** Stamped once, on the owner's first open. Null means the SLA is running. */
  viewed_at?: string | null;
  evaluation_status?: EvaluationStatus | null;
  evaluation_score?: number | null;
  evaluation_notes?: string | null;
  evaluated_at?: string | null;
}

/**
 * Where an identity document came from — which file, off which pages.
 *
 * A passport read out of page 54 of a 60-page application bundle is a different
 * claim from one that arrived as its own scan, and a documentation officer
 * checking a misread number needs to know which page to open.
 */
export interface IdentityDocumentSource {
  provider?: string;
  account_id?: string;
  message_id?: string;
  attachment_id?: string;
  filename?: string;
  sha256?: string;
  pages?: number[];
}

/**
 * The scan itself, where one is stored against the record.
 *
 * A document read out of an application bundle has no block of its own — the
 * pages in `source` are what it is, and the server cuts them out of the stored
 * bundle when somebody asks. A document that arrived as its own upload has
 * this. Either way the download is the same call; this only says what the file
 * will turn out to be called.
 *
 * No storage key. The server never sends one.
 */
export interface IdentityDocumentFile {
  filename?: string;
  mime_type?: string;
  size?: number;
  sha256?: string;
}

/**
 * One Aadhaar card as the OCR service read it.
 *
 * `aadhaar_number` and `vid` are served to administrators only — every other
 * caller gets `masked_aadhaar_number` and nothing else, so a recruiter's
 * browser never holds the full number. Treat both as possibly absent.
 */
export interface AadhaarRecord {
  _id?: string;
  document_type?: string;
  candidate_id?: string | null;
  name?: string | null;
  /** Administrators only. */
  aadhaar_number?: string | null;
  /** Always present — "XXXXXXXX9017". */
  masked_aadhaar_number?: string | null;
  /** The card's own checksum. False means the OCR misread a digit. */
  aadhaar_number_valid?: boolean | null;
  date_of_birth?: string | null;
  year_of_birth?: string | number | null;
  gender?: string | null;
  mobile_number?: string | null;
  address?: string | null;
  care_of?: string | null;
  pincode?: string | null;
  /** Administrators only. */
  vid?: string | null;
  enrollment_id?: string | null;
  document_side?: string | null;
  warnings?: string[];
  source?: IdentityDocumentSource;
  /**
   * Whether there is a scan behind this row that this caller may download.
   *
   * Answered by the server, not guessed at here: it turns on facts the browser
   * does not have — whether a bundle is still in storage, and whether an
   * Aadhaar may be served to whoever is signed in. A button that can only 404
   * tells a recruiter something untrue.
   */
  file_available?: boolean;
  file?: IdentityDocumentFile;
  created_at?: string;
  updated_at?: string;
}

/** One passport as the OCR service read it, MRZ first. */
export interface PassportRecord {
  _id?: string;
  document_type?: string;
  candidate_id?: string | null;
  passport_number?: string | null;
  surname?: string | null;
  given_names?: string | null;
  nationality?: string | null;
  issuing_country?: string | null;
  date_of_birth?: string | null;
  sex?: string | null;
  expiry_date?: string | null;
  date_of_issue?: string | null;
  personal_number?: string | null;
  /**
   * The MRZ's own integrity test. False means a character was misread — worth
   * showing, never worth silently trusting.
   */
  check_digits_valid?: boolean | null;
  mrz_source?: string | null;
  /** Administrators only. */
  raw_mrz?: string | null;
  confidence?: number | null;
  /** Read off the printed page rather than the MRZ — carries place of issue. */
  printed_fields?: Record<string, unknown> | null;
  warnings?: string[];
  source?: IdentityDocumentSource;
  /**
   * Whether there is a scan behind this row that this caller may download.
   *
   * Answered by the server, not guessed at here: it turns on facts the browser
   * does not have — whether a bundle is still in storage, and whether an
   * Aadhaar may be served to whoever is signed in. A button that can only 404
   * tells a recruiter something untrue.
   */
  file_available?: boolean;
  file?: IdentityDocumentFile;
  created_at?: string;
  updated_at?: string;
}

/** Everything `GET /candidates/{id}/identity` found for one candidate. */
export interface IdentityDocuments {
  candidate_id: string;
  aadhaar: AadhaarRecord[];
  passport: PassportRecord[];
}

/** Mirrors EVALUATION_STATUSES in app/core/models.py. */
export type EvaluationStatus =
  | "pending"
  | "shortlisted"
  | "interviewing"
  | "rejected"
  | "on_hold"
  | "hired";

export const EVALUATION_STATUSES: EvaluationStatus[] = [
  "pending",
  "shortlisted",
  "interviewing",
  "rejected",
  "on_hold",
  "hired",
];

/** One quick-fill button on the login screen. */
export interface DemoAccount {
  role: string;
  label: string;
  description: string;
  email: string;
  password: string;
}

export interface StaffMember {
  id: string;
  /** Human-facing roster identifier; separate from the account/database key. */
  staff_code?: string;
  email: string;
  name: string;
  role: string;
  /** Expertise terms the balancer matches against a candidate's skills. */
  keywords: string[];
  /** Free text, and optional — an account created before the field existed
      has none, and no format is imposed on the ones that do. */
  phone?: string;
  active: boolean;
}

/** One row of the admin's workload matrix. */
export interface StaffWorkloadRow extends StaffMember {
  assigned: number;
  evaluated: number;
  unviewed: number;
  pending: number;
  /** Percentage, precomputed server-side so the bar cannot divide by zero. */
  progress: number;
}

export interface StaffWorkloadResponse {
  /** Active accounts only — the ones work is routed to. */
  items: StaffWorkloadRow[];
  /**
   * Every account id on the roster, deactivated ones included.
   *
   * `items` omits deactivated accounts, so an owner id missing from it means
   * one of two very different things: the account is deactivated (fine, they
   * keep their queue) or it is gone (orphaned, nobody can see the profile).
   * This is what tells them apart, and it is the same list the server counts
   * `totals.orphaned` against, so the banner and the directory always agree.
   */
  roster_ids?: string[];
  totals: {
    staff: number;
    assigned: number;
    evaluated: number;
    unassigned: number;
    /** Profiles still pointing at a deleted account — only a rebalance clears these. */
    orphaned: number;
  };
}

/**
 * One row of the bell's feed.
 *
 * Stored server-side rather than derived from the socket, so it is still there
 * after a logout, a restart, or an allocation that happened while nobody was
 * looking — which is most of them.
 */
export interface NotificationRecord {
  id: string;
  type: "candidate_assigned" | "candidate_ingested" | "sla_alert" | string;
  title: string;
  message: string;
  candidate_id?: string | null;
  candidate_name?: string | null;
  created_at?: string;
  read: boolean;
}

export interface SlaAlert {
  id?: string;
  candidate_id: string;
  candidate_name?: string;
  full_name?: string;
  assigned_staff_id?: string | null;
  assigned_staff_name?: string | null;
  assigned_at?: string | null;
  hours_overdue: number;
  /** Which half of the SLA failed. */
  reason?: "unviewed" | "unevaluated";
  status?: "active" | "resolved";
  created_at?: string;
}

export interface RebalanceResult {
  status: string;
  moved: number;
  unchanged: number;
  locked: number;
  total?: number;
  staff_counts: Record<string, { name: string; assigned: number }>;
}

/**
 * What re-homing stranded profiles did.
 *
 * Distinct from a rebalance because it is the only thing that can clear an
 * orphan: every orphan carries a verdict, and a rebalance is defined by
 * refusing to move profiles that do.
 */
export interface RehomeResult {
  status: string;
  rehomed: number;
  remaining: number;
  detail?: string;
}

/** What deleting a staff account did with the queue it was holding. */
export interface DeleteStaffResult {
  status: string;
  id: string;
  /** Unviewed profiles handed straight to the least-loaded remaining staff. */
  reallocated: number;
  /** Reviewed ones left pointing at the deleted account, awaiting a re-home. */
  orphaned: number;
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
  /** Present for staff accounts. */
  staff_code?: string | null;
  email: string;
  name: string;
  role: string;
  /** Personal mobile number recorded on this account, when configured. */
  phone?: string;
  /**
   * The pages this account may reach: its role's floor plus whatever an admin
   * granted it. Computed by the API so the browser never has to reimplement the
   * role rules to draw a menu.
   *
   * Optional because a session issued before permissions existed has no such
   * list, and the rail falls back to the per-item roles in that case.
   */
  pages?: string[];
  /** Only the granted half, for the permission screen's checkboxes. */
  page_grants?: string[];
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

/**
 * A manpower requirement raised by an agent, as `GET /b2b-enquiries` returns it.
 *
 * Snake-case throughout because that is what the API sends; nothing is mapped
 * on the way in. The fields are optional-by-omission rather than by design —
 * the backend writes every one of them on every insert, and they are marked
 * optional here only so a record stored by an earlier build still type-checks.
 */
export interface B2BEnquiryRecord {
  id: string;
  source: "whatsapp" | "manual" | string;
  status: "new" | "reviewing" | "converted" | "closed" | string;

  /** Who raised it. */
  party_type: "agent" | "association" | "client" | string;
  company_name?: string;
  contact_name: string;
  phone?: string;
  phone_e164?: string;
  email?: string;
  country?: string;
  city?: string;

  /** What they asked for. `requirement` is their own words and is the field a
   *  recruiter reads first; everything below it is what the bot managed to
   *  pull out of the conversation. */
  requirement?: string;
  job_title?: string;
  job_id?: string;
  /** Absent rather than 0 when they did not give a number — see the backend's
   *  `_coerce_headcount`, which refuses to invent one. */
  headcount?: number | null;
  destination_country?: string;
  salary_budget?: string;
  experience_required?: string;
  skills?: string[];
  needed_by?: string;
  notes?: string;

  /** The Sourcing Hub record this sender was matched to, when they are already
   *  on file. Advisory — a display label, never an account link. */
  sourcing_client_id?: string | null;
  sourcing_client_name?: string;
  wa_user_id?: string;

  /** What the agency did about it. */
  converted_job_order_id?: string | null;
  handled_by?: string;
  handled_at?: string | null;
  received_at?: string;
  updated_at?: string;
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
