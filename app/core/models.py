"""Domain models shared across the pipeline.

Two families live here:
  * *Transport* models (EmailMessage, Attachment) — what we pull from Gmail.
  * *Persistence* models (CandidateProfile, StoredResume, CandidateRecord) —
    what we compute and write to MongoDB.

Keeping them explicit (rather than passing dicts around) makes every stage's
input/output contract obvious and gives future extensions — search, ranking,
scoring — a stable schema to build on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
#  Transport: what we read from Gmail
# --------------------------------------------------------------------------- #
class Attachment(BaseModel):
    filename: str
    mime_type: str
    size: int
    attachment_id: str          # Gmail attachment handle (fetched lazily)
    data: Optional[bytes] = None  # populated once downloaded


class EmailMessage(BaseModel):
    message_id: str
    thread_id: str
    from_addr: str
    from_name: Optional[str] = None
    to_addr: Optional[str] = None
    subject: str = ""
    date: Optional[str] = None
    snippet: str = ""
    body_text: str = ""
    attachments: List[Attachment] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
#  Extraction output
# --------------------------------------------------------------------------- #
class PageText(BaseModel):
    """One page of a document, plus what the page classifier made of it."""

    page_number: int            # 1-based
    text: str = ""
    kind: str = "unknown"       # resume | certificate | experience_letter | id_document | other | blank
    score: float = 0.0          # how résumé-like the page is
    ocr_used: bool = False


class ExtractedDocument(BaseModel):
    text: str
    method: str                 # "pdf_text" | "pdf_ocr" | "docx" | "doc" | "image_ocr" | "plain"
    page_count: Optional[int] = None
    ocr_used: bool = False
    char_count: int = 0

    # Where the résumé lives inside a multi-document bundle. `text` always holds
    # everything that was extracted — nothing is thrown away — while these say
    # which slice of it is the candidate's profile, so OCR and the LLM can be
    # pointed at that slice alone.
    pages: List[PageText] = Field(default_factory=list)
    resume_pages: List[int] = Field(default_factory=list)   # 1-based
    is_resume: Optional[bool] = None                        # None = not classified
    classification_confidence: Optional[float] = None
    classification_reason: str = ""

    @property
    def resume_text(self) -> str:
        """Only the pages carrying candidate profile data (all of it if unknown)."""
        if not self.pages or not self.resume_pages:
            return self.text
        wanted = set(self.resume_pages)
        parts = [p.text.strip() for p in self.pages if p.page_number in wanted and p.text.strip()]
        return "\n\n".join(parts) or self.text


# --------------------------------------------------------------------------- #
#  Structured candidate (the AI's job)
# --------------------------------------------------------------------------- #
class WorkExperience(BaseModel):
    model_config = ConfigDict(extra="allow")

    company: Optional[str] = None
    designation: Optional[str] = None
    title: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    location: Optional[str] = None
    country: Optional[str] = None
    is_overseas: Optional[bool] = None
    duration_human: Optional[str] = None
    duration_months: Optional[int] = None
    description: Optional[str] = None


class Education(BaseModel):
    model_config = ConfigDict(extra="allow")

    institution: Optional[str] = None
    # Indian and Gulf résumés name the awarding board separately from the school
    # ("SSLC, Govt. Higher Secondary School, Tamil Nadu State Board").
    board_or_university: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    passing_year: Optional[str] = None
    grade: Optional[str] = None


class Project(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    description: Optional[str] = None
    technologies: List[str] = Field(default_factory=list)
    url: Optional[str] = None


class TradeLicense(BaseModel):
    """A licence or trade certificate, with the number that makes it checkable."""

    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    number: Optional[str] = None
    issuer: Optional[str] = None
    issue_date: Optional[str] = None
    expiry_date: Optional[str] = None


class CandidateProfile(BaseModel):
    """The structured key/value profile the AI extracts from resume text."""
    model_config = ConfigDict(extra="allow")

    is_resume: bool = True
    confidence: float = 0.0     # 0..1 — the AI's confidence this is a real resume

    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    # Trade résumés routinely carry a home number and a mobile in the Gulf; the
    # single `phone` above is the primary one, this keeps the rest.
    phone_numbers: List[str] = Field(default_factory=list)
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None

    skills: List[str] = Field(default_factory=list)
    technical_skills: List[str] = Field(default_factory=list)
    # Machinery and trades ("EOT Crane", "TIG Welding", "CNC Lathe") — the part
    # of a blue-collar profile that a generic skills list flattens away.
    trade_skills: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)

    work_experience: List[WorkExperience] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    licenses: List[TradeLicense] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    achievements: List[str] = Field(default_factory=list)

    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    current_company: Optional[str] = None
    current_designation: Optional[str] = None
    total_experience_years: Optional[float] = None

    resume_summary: Optional[str] = None
    # Anything the AI found that doesn't fit the schema above.
    additional_info: Optional[Dict[str, Any]] = Field(default_factory=dict)
    # Full unredacted raw OCR extraction JSON payload
    raw_ocr: Optional[Dict[str, Any]] = None


# --------------------------------------------------------------------------- #
#  Persistence
# --------------------------------------------------------------------------- #
class StoredResume(BaseModel):
    """Pointer + fingerprint for the original resume file we kept."""

    original_filename: str
    mime_type: str
    size: int
    sha256: str                 # content hash — exact-duplicate detection
    storage_backend: str        # "local" | "s3" | "gcs"
    storage_key: str            # path/key within the backend
    extraction_method: str = ""
    ocr_used: bool = False


class SourceEmail(BaseModel):
    message_id: str
    thread_id: str
    from_addr: str
    from_name: Optional[str] = None
    subject: str = ""
    received_date: Optional[str] = None


# The verdicts a reviewer can record. "pending" is the state every allocation
# starts in and the one the SLA clock runs against. Mirrored in
# frontend/src/types/index.ts.
EVALUATION_STATUSES = (
    "pending",
    "shortlisted",
    "interviewing",
    "rejected",
    "on_hold",
    "hired",
)


class CandidateRecord(BaseModel):
    """The full MongoDB document for one ingested candidate."""

    id: str                     # stored as Mongo _id
    profile: CandidateProfile
    resume: StoredResume
    source_email: SourceEmail

    # Normalised dedup keys (also indexed in Mongo).
    email_key: Optional[str] = None
    phone_key: Optional[str] = None
    resume_hash: str = ""

    status: str = "ingested"    # ingested | duplicate | needs_review | error
    duplicate_of: Optional[str] = None
    auto_reply_sent: bool = False
    raw_ocr: Optional[Dict[str, Any]] = None

    # ---- allocation ------------------------------------------------------- #
    # Who owns this profile. An unassigned candidate is one nobody is
    # accountable for and the SLA sweep cannot report, so ingestion places every
    # record it stores (see app.assignment.balancer).
    assigned_staff_id: Optional[str] = None
    assigned_staff_name: Optional[str] = None
    assigned_at: Optional[datetime] = None

    # ---- evaluation ------------------------------------------------------- #
    # `viewed_at` is stamped once, on the owner's first open; while it is null
    # the SLA clock is running. Reassignment clears both this and the verdict.
    viewed_at: Optional[datetime] = None
    evaluation_status: str = "pending"
    evaluation_score: Optional[int] = None      # 1..5 stars
    evaluation_notes: Optional[str] = None
    evaluated_at: Optional[datetime] = None
    evaluated_by: Optional[str] = None          # staff id that recorded it

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def to_mongo(self) -> Dict[str, Any]:
        doc = self.model_dump(mode="python", exclude_none=True)
        doc["_id"] = doc.pop("id")
        return doc

    @classmethod
    def from_mongo(cls, doc: Dict[str, Any]) -> "CandidateRecord":
        doc = dict(doc)
        doc["id"] = doc.pop("_id")
        return cls.model_validate(doc)
